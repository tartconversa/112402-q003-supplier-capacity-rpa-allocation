"""导出接口连接器测试：契约校验、原始回执与故障暂停。"""
from capflow.collectors import CollectionService, JSONExportConnector
from capflow.errors import PageStructureChanged
from capflow.models import RecordStatus, RunState

from tests._base import CapflowTestCase, EXPORTS


class ExportConnectorTest(CapflowTestCase):
    def test_正常导出落库为已采集记录并保留回执(self):
        """合法 JSON 导出应生成产能记录、原始回执和已完成运行。"""
        service = CollectionService(self.repo)
        run = service.collect_export(
            "V-1", "2026-W45",
            JSONExportConnector("V-1", str(EXPORTS / "v1_w45_rev1.json")),
        )
        self.assertEqual(run.state, RunState.COMPLETED.value)
        records = self.repo.list_capacity_records(window="2026-W45")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].quantity, 120)
        self.assertEqual(records[0].status, RecordStatus.COLLECTED.value)
        receipts = self.repo.list_receipts("V-1")
        self.assertEqual(len(receipts), 1)
        self.assertTrue(receipts[0].content_hash)

    def test_门户覆盖旧表时两个版本都保留(self):
        """rev1 与 rev2 必须同时留库，唯一约束按 (供应商,窗口,标识,版本)。"""
        service = CollectionService(self.repo)
        service.collect_export("V-1", "2026-W45",
                               JSONExportConnector("V-1", str(EXPORTS / "v1_w45_rev1.json")))
        service.collect_export("V-1", "2026-W45",
                               JSONExportConnector("V-1", str(EXPORTS / "v1_w45_rev2.json")))
        records = self.repo.list_capacity_records(window="2026-W45")
        self.assertEqual(sorted(r.revision for r in records), [1, 2])
        self.assertEqual([r.quantity for r in records if r.revision == 2], [50])

    def test_字段缺失判定页面结构变化并暂停运行(self):
        """导出契约字段缺失必须暂停该供应商，而不是抛裸异常中断整轮。"""
        service = CollectionService(self.repo)
        with self.assertRaises(PageStructureChanged):
            service.collect_export(
                "V-3", "2026-W45",
                JSONExportConnector("V-3", str(EXPORTS / "v1_broken.json")),
            )
        run = self.repo.list_runs("V-3")[0]
        self.assertEqual(run.state, RunState.PAUSED.value)
        self.assertIsNotNone(run.checkpoint)
        self.assertEqual(run.checkpoint.step_name, "fetch")

    def test_一个供应商暂停不影响其他供应商采集(self):
        """故障隔离：V-3 暂停后，V-1 的采集仍可独立完成。"""
        service = CollectionService(self.repo)
        with self.assertRaises(PageStructureChanged):
            service.collect_export(
                "V-3", "2026-W45",
                JSONExportConnector("V-3", str(EXPORTS / "v1_broken.json")),
            )
        run = service.collect_export(
            "V-1", "2026-W45",
            JSONExportConnector("V-1", str(EXPORTS / "v1_w45_rev2.json")),
        )
        self.assertEqual(run.state, RunState.COMPLETED.value)
