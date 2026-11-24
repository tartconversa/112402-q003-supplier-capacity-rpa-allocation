"""分配引擎测试：共享产能单次扣减、承诺保护、停机重查与依据追溯。"""
from capflow.allocation import AllocationEngine
from capflow.collectors import CollectionService, JSONExportConnector
from capflow.models import (
    ActionKind,
    CollectedRow,
    ItemStatus,
    Order,
    RecordStatus,
)
from capflow.operations import OpsService
from capflow.planning import PlanningService

from tests._base import CapflowTestCase, EXPORTS


def collect_confirmed(repo, path: str, supplier: str = "V-1", window: str = "2026-W45"):
    service = CollectionService(repo)
    service.collect_export(supplier, window,
                           JSONExportConnector(supplier, str(EXPORTS / path)))
    ops = OpsService(repo)
    for rec in repo.list_capacity_records(window=window,
                                          status=RecordStatus.COLLECTED.value):
        ops.confirm_record(rec.record_id)
    return ops


class AllocationEngineTest(CapflowTestCase):
    def test_共享工序产能只被扣减一次(self):
        """model-x 与 model-y 共用 cell-a，总分配不得超过共享池总量。"""
        collect_confirmed(self.repo, "v1_w45_rev1.json")  # 120 件
        plan = AllocationEngine(self.repo).allocate_window(
            "2026-W45", OpsService(self.repo).effective_supply("2026-W45")).plan
        total = sum(it.allocated for it in plan.items)
        self.assertEqual(total, 115)  # 70 + 45，未超过 120
        statuses = {it.order_id: it.status for it in plan.items}
        self.assertEqual(statuses["O1"], ItemStatus.ALLOCATED.value)
        self.assertEqual(statuses["O2"], ItemStatus.ALLOCATED.value)

    def test_未确认产能不得当作可分配余量(self):
        """只采集不确认时，所有订单都不能分配，且不产生虚假承诺。"""
        CollectionService(self.repo).collect_export(
            "V-1", "2026-W45",
            JSONExportConnector("V-1", str(EXPORTS / "v1_w45_rev1.json")))
        plan = AllocationEngine(self.repo).allocate_window(
            "2026-W45", OpsService(self.repo).effective_supply("2026-W45")).plan
        self.assertTrue(all(it.allocated == 0 for it in plan.items))
        self.assertEqual({it.status for it in plan.items},
                         {ItemStatus.HELD.value, ItemStatus.SHORTFALL.value})

    def test_已确认订单优先且缺口生成升级事项(self):
        """confirmed 订单先占用产能；不可兑现时保留 HELD 并升级，不静默改单。"""
        collect_confirmed(self.repo, "v1_w45_rev2.json")  # 50 件
        plan = AllocationEngine(self.repo).allocate_window(
            "2026-W45", OpsService(self.repo).effective_supply("2026-W45")).plan
        items = {it.order_id: it for it in plan.items}
        self.assertEqual(items["O1"].status, ItemStatus.HELD.value)
        self.assertEqual(items["O1"].allocated, 50)
        self.assertEqual(items["O2"].allocated, 0)
        self.assertEqual(items["O2"].status, ItemStatus.SHORTFALL.value)
        kinds = {a.order_id: a.kind for a in plan.actions}
        self.assertEqual(kinds["O1"], ActionKind.ESCALATE.value)
        self.assertEqual(kinds["O2"], ActionKind.RESCHEDULE.value)

    def test_计划订单按优先级和交期排序(self):
        """同级 planned 订单中 priority 小的先获得有限产能（用 W46/cell-b 隔离）。"""
        # 提高基础订单 O3 的优先级数字，使新增的 O9(priority=1) 排在最前。
        self.repo.upsert_order(
            Order("O3", "model-z", "2026-W46", 8, "2026-11-13", "planned", 5))
        self.repo.upsert_order(
            Order("O8", "model-z", "2026-W46", 30, "2026-11-20", "planned", 5))
        self.repo.upsert_order(
            Order("O9", "model-z", "2026-W46", 16, "2026-11-14", "planned", 1))
        # cell-b 仅 24 件：高优先级 O9 先拿 16，剩余 8 件归下一优先级的 O3。
        collect_confirmed(self.repo, "v2_w46.json", supplier="V-2", window="2026-W46")
        plan = AllocationEngine(self.repo).allocate_window(
            "2026-W46", OpsService(self.repo).effective_supply("2026-W46")).plan
        items = {it.order_id: it for it in plan.items}
        self.assertEqual(items["O9"].allocated, 16)
        self.assertEqual(items["O3"].allocated, 8)
        self.assertEqual(items["O8"].allocated, 0)

    def test_停机影响多个型号时同轮重查全部未承诺订单(self):
        """停机后重跑：两型号同时受影响，已承诺订单保留记录并追加升级事项。"""
        ops = collect_confirmed(self.repo, "v1_w45_rev1.json")  # 120 件
        engine = AllocationEngine(self.repo)
        first = engine.allocate_window(
            "2026-W45", ops.effective_supply("2026-W45"), "停机前").plan
        self.assertEqual(
            {it.order_id: it.allocated for it in first.items},
            {"O1": 70, "O2": 45},
        )
        # 停机 60 件后仅剩 60：已确认的 O1 承诺受损（HELD），planned 的 O2 被压缩。
        ops.register_downtime("cell-a", "2026-W45", 60,
                              "主轴维护，cell-a 全线受影响")
        second = engine.allocate_window(
            "2026-W45", ops.effective_supply("2026-W45"), "停机后").plan
        items = {it.order_id: it for it in second.items}
        self.assertEqual(items["O1"].status, ItemStatus.HELD.value)
        self.assertEqual(items["O1"].allocated, 60)
        self.assertEqual(items["O2"].allocated, 0)
        self.assertEqual(items["O2"].status, ItemStatus.SHORTFALL.value)
        escalations = [a for a in second.actions
                       if a.order_id == "O1" and a.kind == ActionKind.ESCALATE.value]
        self.assertTrue(escalations)

    def test_分配依据可追溯到版本和原始回执(self):
        """每条分配明细必须带 record_id/revision/receipt_id。"""
        ops = collect_confirmed(self.repo, "v1_w45_rev1.json")
        plan = AllocationEngine(self.repo).allocate_window(
            "2026-W45", ops.effective_supply("2026-W45")).plan
        for item in plan.items:
            if item.allocated:
                self.assertTrue(item.basis)
                for basis in item.basis:
                    self.assertTrue(basis.record_id.startswith("CAP-"))
                    self.assertTrue(basis.receipt_id.startswith("RCPT-"))
                    self.assertEqual(basis.revision, 1)

    def test_方案对比逐单呈现变更与下一步(self):
        """门户更正后两版方案对比应标出受影响订单、依据版本与新增事项。"""
        ops = collect_confirmed(self.repo, "v1_w45_rev1.json")
        engine = AllocationEngine(self.repo)
        first = engine.allocate_window(
            "2026-W45", ops.effective_supply("2026-W45"), "rev1 方案").plan
        # 门户覆盖为 rev2（50 件），运营确认新版本。
        CollectionService(self.repo).collect_export(
            "V-1", "2026-W45",
            JSONExportConnector("V-1", str(EXPORTS / "v1_w45_rev2.json")))
        for rec in self.repo.list_capacity_records(
                window="2026-W45", status=RecordStatus.COLLECTED.value):
            if rec.revision == 2:
                ops.confirm_record(rec.record_id)
        second = engine.allocate_window(
            "2026-W45", ops.effective_supply("2026-W45"), "rev2 方案").plan

        comparison = PlanningService(self.repo).compare(first, second)
        diffs = {d.order_id: d for d in comparison.diffs}
        self.assertTrue(diffs["O1"].changed)
        self.assertEqual(diffs["O1"].delta, -20)
        self.assertEqual(diffs["O1"].basis_revision_after, [2])
        self.assertTrue(comparison.new_actions)

    def test_缺口闭合在对比中显式标出(self):
        """新回报补足产能后，原 shortfall 订单应出现在 closed_gap_orders。"""
        ops = collect_confirmed(self.repo, "v1_w45_rev2.json")  # 50 件
        engine = AllocationEngine(self.repo)
        first = engine.allocate_window(
            "2026-W45", ops.effective_supply("2026-W45")).plan
        CollectionService(self.repo).collect_export(
            "V-1", "2026-W45",
            JSONExportConnector("V-1", str(EXPORTS / "v1_w45_rev3.json")))  # 120, rev3
        for rec in self.repo.list_capacity_records(
                window="2026-W45", status=RecordStatus.COLLECTED.value):
            if rec.revision == 3:
                ops.confirm_record(rec.record_id)
        second = engine.allocate_window(
            "2026-W45", ops.effective_supply("2026-W45")).plan
        cmp_ = PlanningService(self.repo).compare(first, second)
        self.assertIn("O2", cmp_.closed_gap_orders)

    def test_产品专用余量默认不被其他型号占用(self):
        """未获共享裁决时，model-x 的专用余量不能分给 model-y。"""
        from capflow.collectors import CollectionBatch
        service = CollectionService(self.repo)
        run = service._new_run("V-9", "2026-W45", "test")
        service._persist_batch(run, CollectionBatch(
            supplier="V-9",
            rows=[CollectedRow("2026-W45", "model-x", "product", 70, 1)],
            receipts=[],
        ))
        ops = OpsService(self.repo)
        ops.confirm_record(self.repo.list_capacity_records()[0].record_id)
        plan = AllocationEngine(self.repo).allocate_window(
            "2026-W45", ops.effective_supply("2026-W45")).plan
        items = {it.order_id: it for it in plan.items}
        self.assertEqual(items["O1"].allocated, 70)       # 本型号可用
        self.assertEqual(items["O2"].allocated, 0)        # 他型号不可用
