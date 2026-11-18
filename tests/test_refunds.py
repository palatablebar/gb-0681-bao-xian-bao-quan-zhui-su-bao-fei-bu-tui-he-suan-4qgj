"""退款指令规则与收款依据测试。"""
import csv, io, json
from decimal import Decimal
import pytest
from app.workflow import load, run
from app.modules.m5_calculate.refunds import apply as build_refunds, resolve_basis
from app.modules.m7_archive.export_out import bundle
from app.errors import DomainError
from app import store


def _by_policy(state, pid):
    return next(i for i in state['refund_instructions'] if i['policy_id'] == pid)


def test_refund_instructions_only_for_refund_cases(raw):
    s = run(raw, {})
    # P013/P014 是真实退款件；其余 SUPPLEMENT/EVEN 不出指令
    assert {i['policy_id'] for i in s['refund_instructions']} == {'P013', 'P014'}
    p014 = _by_policy(s, 'P014')
    assert Decimal(p014['amount']) == Decimal('920.00') and p014['status'] == 'READY'


def test_default_basis_is_original_payer(raw):
    s = run(raw, {})
    assert s['refund_basis'] == 'ORIGINAL_PAYER'
    assert s['manifest']['refund_basis'] == 'ORIGINAL_PAYER'
    assert all(i['payee_basis'] == 'ORIGINAL_PAYER' for i in s['refund_instructions'])


def test_closed_original_account_blocks_instead_of_rerouting(raw):
    # 核心风控：原付款账户销户只能挂起，绝不能静默改退最新账户
    s = run(raw, {})
    p013 = _by_policy(s, 'P013')
    assert p013['status'] == 'BLOCKED_CLOSED'
    assert p013['payee_account'] == 'ACC-P013'          # 仍是原付款账户
    assert p013['latest_account']['account'] == 'ACC-P013-NEW'  # 最新账户可见但不被采用
    assert '销户' in p013['reason']
    assert s['archive']['ready'] is False and s['archive']['refund_blocked'] == 1


def test_latest_account_basis_flags_non_payer_risk(raw):
    # 对照口径：改退最新账户时显式标注收款人可能非实际付款人
    s = run(load('clean.csv'), {'payee_basis': 'LATEST_ACCOUNT'})
    p013 = _by_policy(s, 'P013')
    assert p013['payee_account'] == 'ACC-P013-NEW'
    assert '非实际付款人' in p013['reason']


def test_invalid_basis_rejected(raw):
    with pytest.raises(DomainError) as e:
        run(raw, {'payee_basis': 'BOGUS'})
    assert e.value.code == 'PAYEE_BASIS'


def test_instruction_id_stable_across_runs(raw):
    a = run(raw, {}); b = run(raw, {})
    assert a['run_id'] == b['run_id']
    assert _by_policy(a, 'P014')['instruction_id'] == _by_policy(b, 'P014')['instruction_id']


def test_workbench_and_archive_share_same_basis(raw):
    s = run(raw, {})
    rows = list(csv.reader(io.StringIO(bundle(s).lstrip('\ufeff'))))
    exported = [json.loads(r[5]) for r in rows if r[0] == '退款指令']
    assert len(exported) == len(s['refund_instructions'])
    assert {e['payee_basis'] for e in exported} == {s['refund_basis']}
    # 阶段预览 run_id 为 preview；正式批次则为哈希，指令号始终与当前 run_id 同源
    rid = s['run_id']
    assert all(e['instruction_id'].startswith('RF-%s-' % rid) for e in exported)
    assert {e['instruction_id'] for e in exported} == {
        i['instruction_id'] for i in s['refund_instructions']}


def test_retry_is_idempotent_and_keeps_single_instruction(state, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    rid = state['run_id']; store.save(rid, state)
    instr = _by_policy(state, 'P014')
    first = store.register_refund(rid, instr)
    store.register_refund(rid, instr)
    third = store.register_refund(rid, instr)
    assert third['attempts'] == 3 and third['instruction_no'] == first['instruction_no']
    assert store.refund_attempts(rid, 'P014')['attempts'] == 3


def test_failed_generation_preserves_original_payment_rows(state, tmp_path, monkeypatch):
    # 生成失败：登记 FAILED 留痕，但原始缴费流水一行不删、一字不改
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    rid = state['run_id']; store.save(rid, state)
    instr = _by_policy(state, 'P014')
    before = [r for r in state['rows'] if r['policy_id'] == 'P014']
    with pytest.raises(DomainError) as e:
        store.register_refund(rid, instr, simulate_failure=True)
    assert e.value.code == 'REFUND_FAILED' and e.value.status == 502
    att = store.refund_attempts(rid, 'P014')
    assert att['status'] == 'FAILED' and att['last_error'] == 'GATEWAY_ERROR'
    after = store.get(rid)
    after_rows = [r for r in after['rows'] if r['policy_id'] == 'P014']
    assert after_rows == before  # 追溯链完整
    # 失败后成功重试：同一行恢复，不新增指令
    recovered = store.register_refund(rid, instr)
    assert recovered['status'] == 'READY' and recovered['attempts'] == att['attempts'] + 1


def test_no_instruction_for_even_or_supplement(raw):
    s = run(raw, {})
    refund_pids = {i['policy_id'] for i in s['refund_instructions']}
    for c in s['calculations']:
        if c['code'] in ('EVEN', 'SUPPLEMENT'):
            assert c['id'] not in refund_pids


def test_legacy_sample_has_no_refund_and_stays_ready():
    s = run(load('legacy.csv'), {})
    assert s['refund_instructions'] == [] and s['archive']['ready'] is True
