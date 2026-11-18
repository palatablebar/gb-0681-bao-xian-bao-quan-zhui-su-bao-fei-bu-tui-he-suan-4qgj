# 保费补退定义

每份保单有且仅有一条issue基线。change按effective、责任、version选版；同生效日同版本矛盾拒绝。cancel必须引用同保单的change，取消整份变更，不按录入顺序覆盖。区间为[start,end)。年度保费按所在自然年365或366日拆分，跨年强制切段。先求每段精确保费，再最大余额法分配到分，使段合计等于保单总额。paid为已收金额，总应收减已收为补退额，正数补缴、负数退费。

## 通用采信约定

CSV按UTF-8 BOM、UTF-8、GB18030回退，保留逻辑记录结束行号；引号内换行由CSV解析。重复表头单独计数，列数不齐隔离。编码与ID按字符串保形。缺失哨兵为 `-`、`N/A`、`NULL`、`未知` 与空串；不得作为零。数值先NFKC归一、去千分位，再用Decimal。日期采用ISO；时间必须带偏移并转UTC。空文件、全空行、仅表头分别返回EMPTY_FILE、BLANK_FILE、HEADER_ONLY。

重复键为(id,version)，完全相同行去重；同键内容冲突则整组隔离，不取最后一行。行数对账：输入=采纳+隔离+空行+重复表头+完全重复。运行幂等键由输入字节、参数与规则版本计算。

## 收款账户与退款指令

`payee_account`、`account_status` 为可选列，缺失或哨兵（`-`、空串等）不影响采信，仅在退款环节按“账户缺失”处理；字符串保形，不做零值。

收款依据（`payee_basis`）是批次级口径，默认且推荐 `ORIGINAL_PAYER`：退款只回实际出资的原付款账户（同保单最早一笔 `paid>0` 流水登记的账户）。原付款账户 `account_status=closed` 时，指令置 `BLOCKED_CLOSED` 挂起转人工核验，**严禁静默改退最新账户**——改退 `LATEST_ACCOUNT` 可能把款项付给未实际付款的新权利人，该口径仅作对照保留，启用时指令显式标注风险。非退款件（SUPPLEMENT/EVEN）不出指令。

依据在 M5 随 `manifest` 固化，M7 结案下载读取同一 `refund_basis` 与 `refund_instructions`，工作台与归档同源不重算。指令号 `RF-<run_id>-<policy_id>` 由批次与保单稳定派生。同一（批次、保单）仅一条指令，重试只累加 `attempts` 不新增；生成失败登记 `FAILED` 留痕但不删除、不改写任何原始缴费流水，保证追溯链完整。存在 `BLOCKED_CLOSED` 指令的批次 `archive.ready=false`，不得结案。
