"""端到端演练：在临时数据库上走完整业务链路。

运行：

    python3 -m capflow.demo

演练覆盖：

1. 导出接口采集（含门户覆盖旧表的新版本）；
2. 页面结构变化导致单供应商暂停，且不影响其他供应商；
3. RPA 网页确认：提交结果不明确 -> 暂停 -> 仅查询恢复（不重复提交）；
4. 运营确认/驳回、停机、无效班次——未确认产能不进入余量；
5. 共享工序产能只使用一次；已确认订单不可兑现时保留承诺并升级；
6. 停机影响多型号后重查未承诺订单，新旧方案逐单对比。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from capflow.allocation import AllocationEngine
from capflow.cli import show_plan, show_records, show_runs, show_supply
from capflow.collectors import (
    CollectionService,
    FakeBrowser,
    JSONExportConnector,
    RPAScript,
    Step,
)
from capflow.errors import PageStructureChanged, SubmissionAmbiguous
from capflow.models import CollectedRow, Order, RecordStatus, RoutingEntry
from capflow.operations import OpsService
from capflow.planning import PlanningService
from capflow.repository import Repository

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def build_demo_rpa_script() -> RPAScript:
    return RPAScript(
        script_ref="v2-portal-confirm",
        portal_url="https://portal.example-v2.test/capacity/confirm",
        rows=[CollectedRow(window="2026-W46", reported_as="cell-b",
                           report_kind="process", quantity=24, revision=1)],
        steps=[
            Step("打开确认页", "open", "/capacity/confirm", expect="capacity-form"),
            Step("填报数量", "fill", "#qty-cell-b", value=24, expect="capacity-form"),
            Step("提交确认", "submit", "#submit", expect="submit-result", is_submit=True),
            Step("查询结果", "query", "/status", expect="submission-status", is_query=True),
        ],
    )


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Repository(Path(tmp) / "demo.db")
        service = CollectionService(repo)
        ops = OpsService(repo, actor="ops-lead")
        planner = AllocationEngine(repo, actor="planner-wang")

        # ------------------------------------------------ 基础资料
        for entry in [
            RoutingEntry("model-x", "cell-a", 1, "2026-01-01"),
            RoutingEntry("model-y", "cell-a", 1, "2026-01-01"),
            RoutingEntry("model-z", "cell-b", 1, "2026-01-01"),
        ]:
            repo.upsert_routing(entry)
        for order in [
            Order("O1", "model-x", "2026-W45", 70, "2026-11-06", "confirmed", 1),
            Order("O2", "model-y", "2026-W45", 45, "2026-11-07", "planned", 2),
            Order("O3", "model-z", "2026-W46", 20, "2026-11-13", "planned", 1),
        ]:
            repo.upsert_order(order)

        # ------------------------------------------------ 1. 正常采集 + 版本覆盖
        print("\n########## 第 1 步：V-1 导出接口采集（rev1 后被 rev2 覆盖）")
        service.collect_export(
            "V-1", "2026-W45",
            JSONExportConnector("V-1", str(SAMPLES / "exports/v1_w45_rev1.json")),
        )
        service.collect_export(
            "V-1", "2026-W45",
            JSONExportConnector("V-1", str(SAMPLES / "exports/v1_w45_rev2.json")),
        )

        # ------------------------------------------------ 2. 结构变化：该供应商暂停，其他供应商不受影响
        print("\n########## 第 2 步：V-3 导出契约变化 -> 暂停并保留位置")
        try:
            service.collect_export(
                "V-3", "2026-W45",
                JSONExportConnector("V-3", str(SAMPLES / "exports/v1_broken.json")),
            )
        except PageStructureChanged as exc:
            print(f"按预期暂停：{exc}")
            print(f"恢复说明：{exc.instruction}")

        # ------------------------------------------------ 3. RPA：结果不明确 -> 暂停 -> 查询恢复
        print("\n########## 第 3 步：V-2 网页确认，提交结果不明确后恢复")
        script = build_demo_rpa_script()
        try:
            service.collect_rpa(
                "V-2", "2026-W46", script,
                FakeBrowser("V-2", scenario="ambiguous_then_confirmed"),
            )
        except SubmissionAmbiguous as exc:
            print(f"按预期暂停：{exc}")
        paused_run = next(r for r in repo.list_runs()
                          if r.state == "paused" and r.supplier == "V-2")
        print(f"检查点已保存：步骤 {paused_run.checkpoint.step_index} "
              f"({paused_run.checkpoint.step_name})，键={paused_run.checkpoint.idempotency_key}")
        # 恢复时再次 run：已提交的步骤不会重放，只做查询。
        service.resume_rpa(
            paused_run.run_id, script,
            FakeBrowser("V-2", scenario="ambiguous_then_confirmed", already_submitted=True),
        )
        # 再跑一次相同表单：幂等跳过，绝不产生第二次确认。
        again = service.collect_rpa(
            "V-2", "2026-W46", script,
            FakeBrowser("V-2", scenario="normal"),
        )
        print(f"重复表单采集运行 {again.run_id} 已按幂等键跳过提交（审计可查 rpa.skip_duplicate）")
        show_runs(repo)

        # ------------------------------------------------ 4. 运营复核
        print("\n########## 第 4 步：运营复核（未确认产能不进入余量）")
        records = repo.list_capacity_records(window="2026-W45")
        show_records(repo, "2026-W45")
        for rec in records:
            if rec.status == RecordStatus.COLLECTED.value and rec.revision == 2:
                ops.confirm_record(rec.record_id, "与供应商电话复核，rev2 为准")
        print("rev1 旧版本保留留库但未确认，因此不参与有效供应计算")
        # W46 的 RPA 回报确认
        for rec in repo.list_capacity_records(window="2026-W46",
                                              status=RecordStatus.COLLECTED.value):
            ops.confirm_record(rec.record_id)
        # 一个被判无效的替代班次：不增加产能
        ops.register_shift("cell-a", "2026-W45", 30,
                           "临时夜班未经设备主管批准", valid=False)

        # ------------------------------------------------ 5. 第一次分配
        print("\n########## 第 5 步：W45 首次分配（共享 cell-a 只扣一次）")
        supply_before = ops.effective_supply("2026-W45")
        show_supply(repo, "2026-W45")
        first = planner.allocate_window(
            "2026-W45", supply_before, "首次方案：cell-a 50 件，O1 已确认优先"
        ).plan
        show_plan(first)

        # ------------------------------------------------ 6. 临时停机影响多型号 -> 重查 + 对比
        print("\n########## 第 6 步：登记 cell-a 停机 20 件，重跑并对比方案")
        ops.register_downtime("cell-a", "2026-W45", 20,
                              "主轴维护，cell-a 上 model-x/model-y 同时受影响")
        supply_after = ops.effective_supply("2026-W45")
        second = planner.allocate_window(
            "2026-W45", supply_after, "停机后重排：不挪用他单产能，承诺受损即升级"
        ).plan
        show_plan(second)

        comparison = PlanningService(repo).compare(first, second)
        print("\n--- 逐订单对比 ---")
        for d in comparison.affected():
            print(f"{d.order_id}: {d.before_status}/{d.before_allocated} -> "
                  f"{d.after_status}/{d.after_allocated} (Δ{d.delta:+d})；{d.next_step}")
        print(f"新增处理事项：{[(a.order_id, a.kind) for a in comparison.new_actions]}")

        print("\n########## 第 7 步：W46 分配（V-2 RPA 确认的产能可正常使用）")
        w46 = planner.allocate_window("2026-W46", ops.effective_supply("2026-W46")).plan
        show_plan(w46)

        print("\n########## 原始回执与审计抽样")
        for rcp in repo.list_receipts():
            print(f"回执 {rcp.receipt_id} [{rcp.media}] hash={rcp.content_hash[:12]}…")
        events = repo.list_audit(limit=6)
        for event in events:
            print(f"{event.action:28s} {event.entity_type}:{event.entity_id[:24]}")

        repo.close()
        print("\n演练结束。")


if __name__ == "__main__":
    main()
