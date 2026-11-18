from app.modules.m5_calculate.rules import apply as calculate
from app.modules.m5_calculate.refunds import apply as build_refunds

def execute(state):
    state=calculate(state)
    # 收款依据来自批次参数（M1 已固化在 state['parameters']），M5 生成、M7 沿用同一依据
    state=build_refunds(state,state.get('parameters',{}),state.get('run_id'))
    return state
