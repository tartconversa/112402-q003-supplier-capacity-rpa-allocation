"""RPA 驱动测试：幂等、检查点暂停、禁止重复提交与只查询恢复。"""
from capflow.collectors import CollectionService, FakeBrowser
from capflow.collectors.rpa import RPAScript, Step
from capflow.errors import LoginExpired, PageStructureChanged, SubmissionAmbiguous
from capflow.models import CollectedRow, RunState, SubmissionState

from tests._base import CapflowTestCase


def build_script(quantity: int = 24) -> RPAScript:
    return RPAScript(
        script_ref="v2-portal-confirm",
        portal_url="https://portal.example-v2.test/capacity/confirm",
        rows=[CollectedRow(window="2026-W46", reported_as="cell-b",
                           report_kind="process", quantity=quantity, revision=1)],
        steps=[
            Step("打开确认页", "open", "/capacity/confirm", expect="capacity-form"),
            Step("填报数量", "fill", "#qty-cell-b", value=quantity, expect="capacity-form"),
            Step("提交确认", "submit", "#submit", expect="submit-result", is_submit=True),
            Step("查询结果", "query", "/status", expect="submission-status", is_query=True),
        ],
    )


class RPADriverTest(CapflowTestCase):
    def test_正常流程一次提交并确认回执(self):
        """正常场景只提交一次，落库确认状态与 HTML 回执。"""
        service = CollectionService(self.repo)
        run = service.collect_rpa("V-2", "2026-W46", build_script(),
                                  FakeBrowser("V-2", "normal"))
        self.assertEqual(run.state, RunState.COMPLETED.value)
        subs = self.repo.list_rpa_submissions(run.run_id)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["state"], SubmissionState.CONFIRMED.value)
        self.assertTrue(subs[0]["receipt_id"])

    def test_登录过期在首步暂停并保留检查点(self):
        """登录过期必须暂停，且不能写入任何提交记录。"""
        service = CollectionService(self.repo)
        with self.assertRaises(LoginExpired):
            service.collect_rpa("V-2", "2026-W46", build_script(),
                                FakeBrowser("V-2", "login"))
        run = self.repo.list_runs("V-2")[0]
        self.assertEqual(run.state, RunState.PAUSED.value)
        self.assertEqual(run.checkpoint.step_index, 0)
        self.assertEqual(self.repo.list_rpa_submissions(run.run_id)[0]["state"],
                         SubmissionState.REGISTERED.value)

    def test_页面结构变化在故障步骤暂停(self):
        """预期标识不匹配时暂停在具体步骤，证据留存。"""
        service = CollectionService(self.repo)
        with self.assertRaises(PageStructureChanged):
            service.collect_rpa("V-2", "2026-W46", build_script(),
                                FakeBrowser("V-2", "structure"))
        run = self.repo.list_runs("V-2")[0]
        self.assertEqual(run.state, RunState.PAUSED.value)
        self.assertEqual(run.checkpoint.step_name, "打开确认页")

    def test_提交结果不明确后只查询恢复且不重复提交(self):
        """首次提交不明确 -> 暂停；恢复只执行查询，绝不二次提交。"""
        service = CollectionService(self.repo)
        script = build_script()
        with self.assertRaises(SubmissionAmbiguous):
            service.collect_rpa("V-2", "2026-W46", script,
                                FakeBrowser("V-2", "ambiguous_then_confirmed"))
        paused = self.repo.list_runs("V-2")[0]
        self.assertEqual(paused.checkpoint.submission_state,
                         SubmissionState.SUBMITTED_PENDING.value)

        resumed = service.resume_rpa(
            paused.run_id, script,
            FakeBrowser("V-2", "ambiguous_then_confirmed", already_submitted=True),
        )
        self.assertEqual(resumed.state, RunState.COMPLETED.value)
        sub = self.repo.list_rpa_submissions(paused.run_id)[0]
        self.assertEqual(sub["state"], SubmissionState.CONFIRMED.value)
        # 证据中只有一次 submit，恢复动作是 query。
        self.assertEqual([e for e in sub["evidence"] if "submit" in e],
                         ["submit-accepted"])
        self.assertIn("query-confirmed", sub["evidence"])

    def test_仍不明确时继续暂停且不重提(self):
        """门户仍无确认时继续保持暂停，幂等键不变。"""
        service = CollectionService(self.repo)
        script = build_script()
        with self.assertRaises(SubmissionAmbiguous):
            service.collect_rpa("V-2", "2026-W46", script,
                                FakeBrowser("V-2", "ambiguous"))
        paused = self.repo.list_runs("V-2")[0]
        key = paused.checkpoint.idempotency_key
        with self.assertRaises(SubmissionAmbiguous):
            service.resume_rpa(paused.run_id, script,
                               FakeBrowser("V-2", "ambiguous", already_submitted=True))
        again = self.repo.list_runs("V-2")[0]
        self.assertEqual(again.state, RunState.PAUSED.value)
        self.assertEqual(again.checkpoint.idempotency_key, key)

    def test_相同表单重复触发按幂等跳过(self):
        """同供应商同窗口同内容的表单只允许一次确认提交。"""
        service = CollectionService(self.repo)
        service.collect_rpa("V-2", "2026-W46", build_script(),
                            FakeBrowser("V-2", "normal"))
        second = service.collect_rpa("V-2", "2026-W46", build_script(),
                                     FakeBrowser("V-2", "normal"))
        self.assertEqual(second.state, RunState.COMPLETED.value)
        # 数据库中仍只有一条提交记录。
        self.assertEqual(len(self.repo.list_rpa_submissions()), 1)
        skip_events = [e for e in self.repo.list_audit()
                       if e.action == "rpa.skip_duplicate"]
        self.assertEqual(len(skip_events), 1)

    def test_表单数字变化产生不同幂等键(self):
        """数量更正属于新表单，签名与幂等键都应变化。"""
        driver = CollectionService(self.repo).rpa
        run = CollectionService(self.repo)._new_run("V-2", "2026-W46", "test")
        driver._current_supplier = run.supplier
        key1 = driver.idempotency_key(build_script(24))
        key2 = driver.idempotency_key(build_script(30))
        self.assertNotEqual(key1, key2)

    def test_不同供应商相同表单不会互相去重(self):
        """供应商是幂等维度：V-4 与 V-2 同 URL 同内容也必须各自独立提交。"""
        service = CollectionService(self.repo)
        service.collect_rpa("V-2", "2026-W46", build_script(),
                            FakeBrowser("V-2", "normal"))
        # V-4 登录过期应真实暂停，而不是命中 V-2 的幂等记录被跳过。
        with self.assertRaises(LoginExpired):
            service.collect_rpa("V-4", "2026-W46", build_script(),
                                FakeBrowser("V-4", "login"))
        run_v4 = next(r for r in self.repo.list_runs("V-4"))
        self.assertEqual(run_v4.state, RunState.PAUSED.value)
        keys = {s["idempotency_key"] for s in self.repo.list_rpa_submissions()}
        self.assertEqual(len(keys), 2)
