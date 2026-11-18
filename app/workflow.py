import time,importlib,json,base64
from pathlib import Path
from app.config import MODULES
from app.errors import DomainError
from app.manifest import identity,manifest

def load(sample):
    if sample not in ('dirty.csv','clean.csv','legacy.csv'):raise DomainError('SAMPLE','样例名称无效')
    return (Path(__file__).resolve().parents[1]/'sample_data'/sample).read_bytes()

def run(content,parameters=None,stage=7):
    if len(content)>2_000_000:raise DomainError('SIZE','文件超过2MB',413)
    params=parameters or {}
    if not isinstance(params,dict):raise DomainError('PARAMETERS','参数必须是对象')
    start=time.monotonic();state=None
    # 批次ID由输入字节、参数与规则版本决定，提前算出并注入 state，
    # 使 M5 退款指令号能与最终批次同源（否则会退化成 RF-preview-*，破坏追溯与幂等）
    run_id=identity(content,params)
    for n,entry in enumerate(MODULES,1):
        if n>stage:break
        fn=importlib.import_module('app.modules.'+entry[0]+'.service').execute
        state=fn(content,params) if n==1 else fn(state)
        if n==1:state['run_id']=run_id
    # Decimal values are serialized as strings before any storage or response.
    state=json.loads(json.dumps(state,default=str,ensure_ascii=False))
    state['run_id']=run_id;state['manifest']=manifest(content,params,time.monotonic()-start)
    # 收款依据以 M5 实际解析结果为准固化进运行清单，M7 下载与工作台共用同一依据
    if state.get('refund_basis'):state['manifest']['refund_basis']=state['refund_basis']
    state['source_b64']=base64.b64encode(content).decode('ascii')
    if stage>=7:
        from app.modules.m7_archive.export_out import checksum
        state['archive']['sha256']=checksum(state)
    return state
