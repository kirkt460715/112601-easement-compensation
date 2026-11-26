# 公益林限制叠加下的分户补偿与争议提存

限制区间使用同一合同周期的相对日，资金以分计。权益变更材料按报入时间和生效日分别记载。支付回执是外部到账事实。

本地资料台提供合成台账、分批报到的事实以及合作方的独立回执。台账为可供查询的登记记录，回执注明的是合作方所知的实际情况。

## 结构

- `evidence_desk.py`：**资料台**（只读数据源）。提供合成台账、分页到达消息、合作方回执查询。
- `settlement/`：**补偿处理后端**。
  - `domain.py`：纯规则——闭区间天数、份额分数、协议叠加/共担拆分、最大余数法取整。
  - `ledger.py`：事件台账——只存事实，应付决定、提存、争议案件、预算占用全部按需重算。
  - `desk_client.py`：资料台客户端，自动翻页，对首次 503 的合作方查询按策略重试。
  - `service.py`：HTTP 服务，区分住户、村级经办、财政三类角色。
- `settlement_desk.py`：后端 CLI 入口。

## 联调入口

使用 Python 3.11.2 标准库。资料台与后端分别监听本地地址。

```sh
python3 -m unittest -v
python3 evidence_desk.py --port 8765
python3 settlement_desk.py --port 8770 --desk-port 8765 --bootstrap
```

不带 `--desk-port` 时后端仅使用内存台账，可直接通过 HTTP 受理事实。`--seed 台账.json` 可载入初始登记材料。

### 资料台（原有）

`GET /register` 读取 `data/register.json`。 `GET /messages?after=0&size=30` 从 `data/messages.json` 返回一页到达记录。消息含 `sequence`、`received_at`、`source` 和 `facts`。用返回的 `cursor` 读取下一页，游标顺序是接收顺序。

`GET /partner?action=操作&desk=合作方` 查询 `data/partner-queries.json` 中对应的 `fact`。配置 `unavailable_first` 的查询第一次返回 503，此次查询未能取得回执，同一查询后续可取得已记录的事实。未知查询返回 404。可以用 `--data` 指向另一组同名文件。

### 补偿后端

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | `/state` | 全部 | 当前权益、协议、申请、逐行应付决定与争议案件 |
| GET | `/household/view?subject=hh-1` | 住户 | 本人权益区间、已入账、已提交待查、可付未发、与本人相关的提存/争议金额 |
| POST | `/facts` | 住户/经办 | 受理一条事实，`source` 取 `register`/`budget`/`treasury`/`escrow`/`household` |
| POST | `/claims` | 住户/经办 | 受理补偿申请（非 register 来源标记待证） |
| POST | `/agreements` | 住户/经办 | 受理协议材料（非 register 来源不参与自动兑付） |
| GET | `/operator/rights` | 经办 | 全部权益区间（含待证主张） |
| GET | `/operator/decisions` | 经办 | 逐行可付决定与原因 |
| GET | `/operator/cases` | 经办 | 待处理争议案件清单 |
| POST | `/operator/payments` | 经办 | 发起支付，提交即占用预算，回执前不算支出 |
| POST | `/operator/receipts` | 经办 | 直接登记外部回执 |
| GET | `/operator/reconcile?kind=transfer&transfer_id=out-4` | 经办 | 向资料台取财政回执并登记（自动重试 503） |
| GET | `/operator/reconcile?kind=escrow` | 经办 | 向资料台取提存账户记录并对账 |
| POST | `/operator/bootstrap` | 经办 | 从资料台载入台账并按序重放全部消息 |
| GET | `/treasury/view?batch=批次号` | 财政 | 批次收支、各预算支出/占用/剩余、提存记录、保留未决的资金 |

## 业务规则落点

- **多份额、先明先付、待证提存**：权益按“槽位”的替换链重建（`ledger.rights_intervals`）。已确认份额生成 `holder` 行照常可付；待证份额生成 `escrow` 行进提存。提存缺口只形成 `escrow_reconcile` 案件，绝不从无争议住户款中扣差额。
- **两份协议的核对**：按限制类别、有效期与 `stack` 匹配（`domain.split_claim`）。`shared` 协议同日重叠只承担一次，按 `allocation` 注明的比例拆分；`independent` 协议可叠加、各自全额。缺少分摊比例直接报错，不猜测。
- **金额取整**：份额与比例用 `Fraction` 精确计算，落账时用最大余数法分成整数分，各份之和恒等于毛额。
- **预算封存**：`budget` 事实改变预算状态。封存时依 `interim_funding` 判断本预算可否先付自身已确定份额、或由另一预算按明确约定垫付；都不允许则挂 `held` 并在 `claims_reserved` 中保留，未决资金不被视作已花出，也不重新许给另一块地。
- **晚到确权证明的三种款项**：未发出的款随新权益人自动改判（`unissued_reassigned`）；已提交未回执的进入追查（`trace_pending`，不重复支付）；已确认入账的不自动冲账，超出当前权益的部分生成 `recover_return`，新权益人未补足部分生成 `topup`，由经办查原款另办退回/追补。
- **回执不设定权益**：`confirm_transfer` 只更新支付状态；外部回执无论注明谁收款，都不会创建或修改权益份额。
- **身份更正**：确权替换事实按 `valid_from` 截断原权益人区间，只改变今后受款；历史到账保留在财政批次记录中。
- **三角色查询**：住户只见本人相关数据；经办取得全部争议案件；财政沿同一支付批次核对支出、占用与剩余预算。
