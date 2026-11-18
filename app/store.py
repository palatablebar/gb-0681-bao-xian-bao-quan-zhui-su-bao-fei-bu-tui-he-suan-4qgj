import sqlite3,json,os
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    # 退款指令登记表：(批次,保单) 唯一，重试只更新同一行，绝不新增指令
    db.execute('''CREATE TABLE IF NOT EXISTS refund_instructions (
        run_id TEXT NOT NULL, policy_id TEXT NOT NULL, instruction_no TEXT NOT NULL,
        status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '', body TEXT NOT NULL,
        PRIMARY KEY (run_id, policy_id))''')
    return db

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision']);return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def register_refund(run_id, instruction, simulate_failure=False):
    """登记一次退款（重试）。

    幂等键为 (run_id, policy_id)：首次插入，后续重试只累加 attempts 并更新同一行，
    同一退款重试不会产生第二条指令。simulate_failure=True 模拟网关生成失败：
    仅登记错误并抛出，绝不删除/改写 runs 中的原始缴费流水，追溯链保持完整。
    """
    policy_id=instruction['policy_id'];no=instruction['instruction_id']
    db=connection()
    try:
        existing=db.execute('SELECT attempts FROM refund_instructions WHERE run_id=? AND policy_id=?',(run_id,policy_id)).fetchone()
        attempts=(existing['attempts'] if existing else 0)+1
        if simulate_failure:
            # 失败路径：先提交 FAILED 留痕再抛错。注意不能依赖 with 事务——
            # 连接上下文管理器在异常时会回滚，那样失败痕迹会丢失。
            # 全程不触碰 runs 中的原始缴费流水，仅在本表留痕，保证失败可追溯。
            db.execute('''INSERT INTO refund_instructions(run_id,policy_id,instruction_no,status,attempts,last_error,body)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,policy_id) DO UPDATE SET
                status=excluded.status,attempts=excluded.attempts,
                last_error=excluded.last_error,body=excluded.body''',
                (run_id,policy_id,no,'FAILED',attempts,'GATEWAY_ERROR',json.dumps(instruction,ensure_ascii=False)))
            db.commit()
            raise DomainError('REFUND_FAILED','退款指令生成失败，已保留原缴费流水与失败记录',502)
        db.execute('''INSERT INTO refund_instructions(run_id,policy_id,instruction_no,status,attempts,last_error,body)
            VALUES(?,?,?,?,?,'',?) ON CONFLICT(run_id,policy_id) DO UPDATE SET
            instruction_no=excluded.instruction_no,status=excluded.status,attempts=excluded.attempts,
            last_error='',body=excluded.body''',
            (run_id,policy_id,no,instruction.get('status','READY'),attempts,json.dumps(instruction,ensure_ascii=False)))
        row=db.execute('SELECT * FROM refund_instructions WHERE run_id=? AND policy_id=?',(run_id,policy_id)).fetchone()
        db.commit()
        return dict(row)
    finally:
        db.close()

def refund_attempts(run_id, policy_id):
    with connection() as db:
        r=db.execute('SELECT * FROM refund_instructions WHERE run_id=? AND policy_id=?',(run_id,policy_id)).fetchone()
    return dict(r) if r else None
