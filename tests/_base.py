"""测试共用夹具。"""
from pathlib import Path
import tempfile
import unittest

from capflow.models import Order, RoutingEntry
from capflow.repository import Repository

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
EXPORTS = SAMPLES / "exports"
RPA_SCENARIOS = SAMPLES / "rpa"


class CapflowTestCase(unittest.TestCase):
    """每个测试使用独立临时数据库并预置基础路由与订单。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.repo = Repository(self.db_path)
        for entry in [
            RoutingEntry("model-x", "cell-a", 1, "2026-01-01"),
            RoutingEntry("model-y", "cell-a", 1, "2026-01-01"),
            RoutingEntry("model-z", "cell-b", 1, "2026-01-01"),
        ]:
            self.repo.upsert_routing(entry)
        for order in [
            Order("O1", "model-x", "2026-W45", 70, "2026-11-06", "confirmed", 1),
            Order("O2", "model-y", "2026-W45", 45, "2026-11-07", "planned", 2),
            Order("O3", "model-z", "2026-W46", 20, "2026-11-13", "planned", 1),
        ]:
            self.repo.upsert_order(order)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()
