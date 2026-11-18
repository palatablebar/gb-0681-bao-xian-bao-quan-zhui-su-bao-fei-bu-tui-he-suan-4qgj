"""退款指令规则。

收款依据（payee_basis）是批次级口径，在 M5 生成并随 manifest 固化，
M7 结案下载沿用同一依据，工作台与归档不得各取一套。

- ORIGINAL_PAYER（默认且唯一推荐）：退款只回实际出资的原付款账户。
  原账户销户时不静默改退最新账户，而是生成 BLOCKED_CLOSED 指令挂起，转人工核验，
  既防冒领，也避免款项被银行退回后无人认领。
- LATEST_ACCOUNT：仅作对照口径保留。改退最新账户可能把钱付给未实际付款的新权利人，
  因此默认不启用；启用时指令显式标注 basis 与风险。

同一（批次、保单）只允许一条指令：重试按指令号幂等更新，绝不新增；
本模块只读 state 中的原始缴费流水，任何失败都不会删除或改写 rows，保证追溯链完整。
"""
from decimal import Decimal

from app.config import (
    ACCOUNT_CLOSED,
    PAYEE_BASIS_CHOICES,
    PAYEE_BASIS_DEFAULT,
    PAYEE_BASIS_LATEST,
    PAYEE_BASIS_ORIGINAL,
)
from app.errors import DomainError

# 指令状态
STATUS_READY = 'READY'              # 原付款账户可用，可发起退款
STATUS_BLOCKED = 'BLOCKED_CLOSED'   # 原付款账户销户，挂起待人工核验，不得自动改退
STATUS_SKIPPED = 'SKIPPED'          # 非退款件（补缴/持平），不出指令

# 缺失账户列或哨兵值时的占位
MISSING_ACCOUNT = '-'


def resolve_basis(params):
    """从批次参数解析收款依据，缺省 ORIGINAL_PAYER，非法取值直接拒绝。"""
    basis = (params or {}).get('payee_basis', PAYEE_BASIS_DEFAULT)
    if basis not in PAYEE_BASIS_CHOICES:
        raise DomainError('PAYEE_BASIS', '退款收款依据无效：%s' % basis)
    return basis


def _account(row):
    account = str(row.get('payee_account', '') or '').strip()
    status = str(row.get('account_status', '') or '').strip().lower()
    return account, status


def _paid_rows(rows, policy_id):
    """该保单下真正发生资金收付的原始流水（paid>0），按来源行号稳定排序。

    只读取、不过滤删除原始行；返回值仅用于定位付款人与金额，追溯仍以 rows 为准。
    """
    paid = []
    for r in rows:
        if r.get('policy_id') != policy_id:
            continue
        try:
            amount = Decimal(str(r.get('paid', 0)))
        except Exception:
            amount = Decimal(0)
        if amount > 0:
            paid.append(r)
    return sorted(paid, key=lambda r: r.get('_line', 0))


def _original_payer(rows, policy_id):
    """原付款人：最早一笔实际缴费流水上的账户（实际出资方）。"""
    paid = _paid_rows(rows, policy_id)
    if not paid:
        return None
    account, status = _account(paid[0])
    return {
        'row_id': paid[0].get('id', ''),
        'line': paid[0].get('_line', 0),
        'account': account or MISSING_ACCOUNT,
        'status': status or MISSING_ACCOUNT,
    }


def _latest_account(rows, policy_id):
    """最新登记账户：同保单按来源行号取最后一条带账户号的记录。"""
    candidate = None
    for r in sorted(rows, key=lambda r: r.get('_line', 0)):
        if r.get('policy_id') != policy_id:
            continue
        account, status = _account(r)
        if account:
            candidate = {
                'row_id': r.get('id', ''),
                'line': r.get('_line', 0),
                'account': account,
                'status': status or MISSING_ACCOUNT,
            }
    return candidate


def build_instruction(run_id, calc, rows, basis):
    """为一条计算结论构造退款指令；非退款件返回 None。"""
    difference = Decimal(str(calc['difference']))
    if difference >= 0:
        return None  # 补缴(SUPPLEMENT)或持平(EVEN)无需退款指令

    policy_id = calc['id']
    refund_amount = (-difference).quantize(Decimal('.01'))
    payer = _original_payer(rows, policy_id)
    latest = _latest_account(rows, policy_id)

    instruction = {
        # 指令号由批次+保单稳定派生：重试得到同一编号，天然幂等，不会重复出指令
        'instruction_id': 'RF-%s-%s' % (run_id, policy_id),
        'policy_id': policy_id,
        'line': calc.get('line'),
        'amount': str(refund_amount),
        'payee_basis': basis,
        'original_payer': payer,
        'latest_account': latest,
        'payee_account': MISSING_ACCOUNT,
        'payee_status': MISSING_ACCOUNT,
        'source_row_id': payer['row_id'] if payer else '',
        'source_line': payer['line'] if payer else 0,
        'status': STATUS_BLOCKED,
        'reason': '',
    }

    if basis == PAYEE_BASIS_ORIGINAL:
        if not payer or payer['account'] == MISSING_ACCOUNT:
            instruction.update(status=STATUS_BLOCKED,
                               reason='原付款账户缺失，需人工核验实际出资人')
        elif payer['status'] == ACCOUNT_CLOSED:
            # 关键风控点：销户只挂起，绝不自动改退最新账户，防止冒领与退回断链
            instruction.update(
                payee_account=payer['account'], payee_status=payer['status'],
                source_row_id=payer['row_id'], source_line=payer['line'],
                status=STATUS_BLOCKED,
                reason='原付款账户已销户，禁止改退最新账户，挂起人工核验')
        else:
            instruction.update(
                payee_account=payer['account'], payee_status=payer['status'],
                source_row_id=payer['row_id'], source_line=payer['line'],
                status=STATUS_READY, reason='退还原付款人账户')
    else:  # LATEST_ACCOUNT 对照口径
        if not latest:
            instruction.update(status=STATUS_BLOCKED, reason='无可用登记账户')
        else:
            drifted = bool(payer and latest['account'] != payer['account'])
            instruction.update(
                payee_account=latest['account'], payee_status=latest['status'],
                source_row_id=latest['row_id'], source_line=latest['line'],
                status=STATUS_READY,
                reason='改退最新账户，收款人可能非实际付款人' if drifted else '最新账户即原付款账户')
    return instruction


def apply(state, params=None, run_id=None):
    """在 M5 计算结论上生成退款指令，并把收款依据固化到 state。"""
    basis = resolve_basis(params)
    rid = run_id or state.get('run_id') or 'preview'
    rows = state.get('rows', [])
    instructions = []
    for calc in state.get('calculations', []):
        instruction = build_instruction(rid, calc, rows, basis)
        if instruction is not None:
            instructions.append(instruction)
    state['refund_instructions'] = instructions
    state['refund_basis'] = basis
    return state
