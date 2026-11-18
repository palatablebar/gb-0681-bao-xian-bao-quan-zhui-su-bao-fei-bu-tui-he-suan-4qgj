from app.modules.m7_archive.export_out import checksum

def execute(state):
    instructions=state.get('refund_instructions',[])
    # 阻断（如原付款账户销户）的指令不得视为可结案，需人工核验后放行
    blocked=sum(1 for r in instructions if r.get('status')=='BLOCKED_CLOSED')
    state['archive']={'sha256':checksum(state),'evidence_count':len(state.get('calculations',[]))+len(state['quarantine'])+len(instructions),
        'refund_count':len(instructions),'refund_blocked':blocked,
        'refund_basis':state.get('refund_basis',''),
        'ready':state.get('reconcile',{}).get('balanced',False) and blocked==0}
    return state
