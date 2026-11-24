# capflow：供应商产能回报采集与订单承诺调整

设备厂每周向关键供应商确认未来数周可供数量，再用于分配客户订单。本项目覆盖
**采集 → 运营复核 → 分配 → 方案对比** 的完整闭环，全部使用 Python 3 标准库实现。

## 业务规则与设计约束

| 场景 | 系统行为 |
| --- | --- |
| 供应商通过授权导出接口回报 | 传输层可注入；严格校验门户契约，原始回执（含 SHA-256）原样留库 |
| 供应商需要网页提交确认 | 可审计 RPA：幂等键 + 状态机 + 逐步证据，**同表单绝不重复提交** |
| 页面结构变化 / 登录过期 / 提交结果不明确 | **暂停该供应商流程**并落检查点（步骤、提交状态、幂等键、恢复说明），其他供应商不受影响 |
| 提交结果不明确后的恢复 | 只允许凭幂等键执行查询步骤，禁止再次填写提交 |
| 门户覆盖旧表 | 新版本作为新 revision 全量保留；有效产能只取最新版，旧版留库可审计 |
| 运营判断共用设备 / 停机 / 替代班次 | 以运营复核记录为唯一裁决来源；无效班次不增产能，停机按资源扣减 |
| 未确认的产能 | 状态非 `confirmed` 的记录一律排除在有效供应之外，**不能当作可分配余量** |
| 共享工序 | 一个资源维护单一产能池，所有订单从同一池扣减，**整条产线产能只使用一次** |
| 已向客户确认的交期 | confirmed 订单优先满足并保留承诺；新回报使其不可兑现时生成改期/升级事项，**不静默挪用他单产能** |
| 临时停机影响多个型号 | 登记停机后重跑分配，所有未承诺订单在同一轮重新核对 |
| 门户更正或人工复核 | 方案整体快照留存，可逐订单对比前后状态、依据版本与下一步 |

## 目录结构

```
capflow/
  models.py        领域模型与状态枚举（采集、复核、分配、事项）
  errors.py        可恢复采集错误（登录过期/结构变化/结果不明确）
  repository.py    SQLite 持久化：回执、版本、审计事件、RPA 状态机、计划快照
  collectors/
    export.py      授权导出接口连接器（Transport 协议可替换为 HTTP 实现）
    rpa.py         RPA 驱动（RPAScript/BrowserSession 协议，FakeBrowser 演练）
    service.py     采集编排：运行隔离、检查点、记录归一化与回执落库
  operations.py    运营复核录入与有效供应计算
  allocation.py    分配引擎（单次扣减、承诺保护、升级事项、版本化依据）
  planning.py      方案快照对比（逐订单 diff、新增事项、缺口闭合）
  reference.py     CSV/JSON 样例加载器
  cli.py           命令行入口
  demo.py          端到端演练（临时库，不改业务数据）
samples/
  capacity.csv orders.csv routing.csv      原始演练数据
  exports/                                  门户 JSON 导出（含故障与多版本样例）
  rpa/                                      RPA 脚本样例
tests/              unittest 测试（中文 docstring，标准库）
```

## 快速开始

```bash
# 运行全部测试（30 个）
python3 -m unittest discover -s tests -v

# 端到端演练：采集→暂停/恢复→复核→分配→停机重排→方案对比
python3 -m capflow.demo
```

### CLI 工作流

```bash
DB="--db data/capflow.db"
python3 -m capflow.cli $DB load-samples
# 导出接口采集（结构变化时自动暂停，runs 可查看检查点）
python3 -m capflow.cli $DB collect-export --supplier V-1 --window 2026-W45 \
    --file samples/exports/v1_w45_rev2.json
# RPA 网页确认（scenario-name 可注入 login/structure/ambiguous 等故障）
python3 -m capflow.cli $DB collect-rpa --supplier V-2 --window 2026-W46 \
    --scenario samples/rpa/v2_w46_confirm.json --scenario-name ambiguous
# 从检查点恢复（仅查询，不重复提交）
python3 -m capflow.cli $DB resume-rpa --run-id RUN-xxxx --supplier V-2 \
    --window 2026-W46 --scenario samples/rpa/v2_w46_confirm.json
# 运营动作
python3 -m capflow.cli $DB confirm-all --window 2026-W45
python3 -m capflow.cli $DB review --kind downtime --resource cell-a \
    --window 2026-W45 --quantity 20 --reason "主轴维护" --valid
python3 -m capflow.cli $DB review --kind shift --resource cell-a \
    --window 2026-W45 --quantity 10 --reason "未批准夜班"   # 不加 --valid 即无效
# 有效供应、分配、方案对比、审计与回执
python3 -m capflow.cli $DB supply --window 2026-W45
python3 -m capflow.cli $DB allocate --window 2026-W45
python3 -m capflow.cli $DB compare --window 2026-W45
python3 -m capflow.cli $DB audit
python3 -m capflow.cli $DB receipts
```

## 关键状态机

- 采集运行：`running → completed`，或 `running → paused`（携带检查点，可恢复）。
- RPA 提交：`registered → in_progress → submitted_pending → confirmed`；
  `submitted_pending` 之后只允许查询转换，任何恢复路径都不会再触发 submit。
- 产能记录：`collected → confirmed / rejected`；路由无法解析时为 `exception`。
- 订单明细：`allocated / shortfall / held`，其中 `held` 表示已承诺但新回报下无法全额兑现。
- 处理事项：`reschedule / escalate / reconcile`，默认 `open`，处理后可关闭。

## 生产化接入点

- `collectors/export.py` 的 `Transport` 协议：实现带鉴权的 HTTP 客户端替换 `FileTransport`。
- `collectors/rpa.py` 的 `BrowserSession` 协议：用 Playwright/Selenium 实现 `login()` 与
  `do(step)`，保留逐步截图/HTML 证据与预期标识校验即可复用全部幂等与检查点逻辑。
- 数据库默认 SQLite 单文件；`Repository` 是唯一持久化入口，可整体替换为其他后端。
- 所有写操作均通过 `Repository.audit()` 留痕，审计表只追加。

## 样例数据说明

所有标识与数值均为合成数据。CSV 为 UTF-8、首行列名、空单元格表示来源未提供值；
`samples/exports/` 中的 JSON 模拟门户导出，`v1_broken.json` 用于触发结构变化暂停，
`v1_w45_rev1/2/3.json` 演示门户连续更正时的版本留存与最新版生效规则。
