TITLE='保险保全追溯与保费补退核算'
SHORT='endorsement'
RULE_VERSION='2026.11.18-v2'
MODULES=[('m1_ingest', '保单采信'), ('m2_mapping', '责任映射'), ('m3_versions', '保全选版'), ('m4_segments', '承保切片'), ('m5_calculate', '保费重算'), ('m6_reconcile', '补退对账'), ('m7_archive', '结案归档')]
REQUIRED=['id', 'policy_id', 'version', 'kind', 'effective', 'start', 'end', 'annual_premium', 'paid', 'cover']
NUMERIC=['version', 'annual_premium', 'paid']
DATES=['effective', 'start', 'end']
TIMES=[]
# 可选账户列：缺失时按哨兵处理，不影响采信；账户状态与账户号随记录保形
ACCOUNT_FIELDS=['payee_account', 'account_status']
# 退款收款依据。ORIGINAL_PAYER：只退实际付款人，账户销户则阻断挂起，严禁静默改退最新账户。
# LATEST_ACCOUNT 仅作对照口径保留，默认不得启用，避免把款项付给未实际付款的新权利人。
PAYEE_BASIS_ORIGINAL='ORIGINAL_PAYER'
PAYEE_BASIS_LATEST='LATEST_ACCOUNT'
PAYEE_BASIS_DEFAULT=PAYEE_BASIS_ORIGINAL
PAYEE_BASIS_CHOICES=(PAYEE_BASIS_ORIGINAL, PAYEE_BASIS_LATEST)
ACCOUNT_CLOSED='closed'
