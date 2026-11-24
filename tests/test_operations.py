"""运营复核测试：未确认隔离、版本选择、停机、班次与共享裁决。"""
from capflow.collectors import CollectionService, JSONExportConnector
from capflow.operations import OpsService

from tests._base import CapflowTestCase, EXPORTS


class OpsServiceTest(CapflowTestCase):
    def _collect_two_revisions(self):
        service = CollectionService(self.repo)
        service.collect_export("V-1", "2026-W45",
                               JSONExportConnector("V-1", str(EXPORTS / "v1_w45_rev1.json")))
        service.collect_export("V-1", "2026-W45",
                               JSONExportConnector("V-1", str(EXPORTS / "v1_w45_rev2.json")))
        return self.repo.list_capacity_records(window="2026-W45")

    def test_未确认记录不进入有效供应(self):
        """collected/rejected/exception 状态的记录一律排除在可分配余量之外。"""
        self._collect_two_revisions()
        supply = OpsService(self.repo).effective_supply("2026-W45")
        self.assertEqual(supply.buckets, {})
        # 有效供应只评估最新版本（rev2）；rev1 已被门户覆盖，仍留库但不再生效。
        self.assertEqual(len(supply.excluded), 1)
        self.assertEqual(self.repo.list_capacity_records(window="2026-W45").__len__(), 2)

    def test_只取最新版本且必须确认(self):
        """门户覆盖旧表后，只有确认过的 rev2 生效，rev1 留库但不计数。"""
        records = self._collect_two_revisions()
        ops = OpsService(self.repo)
        rev2 = next(r for r in records if r.revision == 2)
        ops.confirm_record(rev2.record_id)
        supply = ops.effective_supply("2026-W45")
        bucket = supply.buckets["cell-a"]
        self.assertEqual(bucket.shared, 50)
        self.assertEqual(len(bucket.basis), 1)
        self.assertEqual(bucket.basis[0].revision, 2)

    def test_驳回记录不参与分配(self):
        """运营驳回的回报即使是最新版本也必须排除。"""
        records = self._collect_two_revisions()
        ops = OpsService(self.repo)
        rev2 = next(r for r in records if r.revision == 2)
        ops.reject_record(rev2.record_id, "供应商填报错误")
        supply = ops.effective_supply("2026-W45")
        self.assertNotIn("cell-a", supply.buckets)
        self.assertTrue(any("rejected" in reason for _, reason in supply.excluded))

    def test_停机扣减共享产能并保留依据(self):
        """有效停机从共享池扣减，且影响数量、原因进入桶备注。"""
        records = self._collect_two_revisions()
        ops = OpsService(self.repo)
        ops.confirm_record(next(r for r in records if r.revision == 2).record_id)
        ops.register_downtime("cell-a", "2026-W45", 20, "主轴维护")
        bucket = ops.effective_supply("2026-W45").buckets["cell-a"]
        self.assertEqual(bucket.shared, 30)
        self.assertEqual(bucket.downtime_total, 20)
        self.assertTrue(any("主轴维护" in n for n in bucket.notes))

    def test_无效替代班次不增加产能(self):
        """运营判定无效的班次不得计入有效供应。"""
        records = self._collect_two_revisions()
        ops = OpsService(self.repo)
        ops.confirm_record(next(r for r in records if r.revision == 2).record_id)
        ops.register_shift("cell-a", "2026-W45", 30, "夜班未批准", valid=False)
        bucket = ops.effective_supply("2026-W45").buckets["cell-a"]
        self.assertEqual(bucket.shared, 50)
        self.assertEqual(bucket.shift_total, 0)

    def test_有效班次增加产能(self):
        """批准的替代班次按数量增加有效供应。"""
        records = self._collect_two_revisions()
        ops = OpsService(self.repo)
        ops.confirm_record(next(r for r in records if r.revision == 2).record_id)
        ops.register_shift("cell-a", "2026-W45", 10, "周末加班已批准", valid=True)
        bucket = ops.effective_supply("2026-W45").buckets["cell-a"]
        self.assertEqual(bucket.shared, 60)
        self.assertEqual(bucket.shift_total, 10)

    def test_共享裁决控制产品余量能否跨型号使用(self):
        """产品级余量默认专用；运营确认共享后才进入共享池。"""
        service = CollectionService(self.repo)
        # 直接构造一条产品型号回报。
        from capflow.models import CollectedRow
        from capflow.collectors import CollectionBatch
        run = service._new_run("V-9", "2026-W45", "test")
        batch = CollectionBatch(
            supplier="V-9",
            rows=[CollectedRow("2026-W45", "model-x", "product", 40, 1)],
            receipts=[],
        )
        service._persist_batch(run, batch)
        ops = OpsService(self.repo)
        rec = self.repo.list_capacity_records()[0]
        ops.confirm_record(rec.record_id)
        bucket = ops.effective_supply("2026-W45").buckets["cell-a"]
        self.assertEqual(bucket.earmark, {"model-x": 40})
        self.assertEqual(bucket.shared, 0)
        ops.set_sharing("cell-a", "2026-W45", "model-x", True, "两型号同线且换型可行")
        bucket2 = ops.effective_supply("2026-W45").buckets["cell-a"]
        self.assertEqual(bucket2.shared, 40)
        self.assertEqual(bucket2.earmark, {})
