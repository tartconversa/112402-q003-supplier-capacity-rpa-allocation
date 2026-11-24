"""可审计的 RPA 网页提交驱动。

保证三条硬规则：

1. **幂等**：同一供应商/时间窗/表单内容生成同一幂等键，作为数据库唯一键；
2. **不重复提交**：恢复时若提交已发生（含结果不明确），只允许查询，禁止再次提交；
3. **可恢复**：页面结构变化、登录过期、结果不明确都会在故障步骤落检查点并暂停，
   证据（截图/HTML 摘要）随提交记录留存。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..errors import LoginExpired, PageStructureChanged, SubmissionAmbiguous
from ..models import (
    Checkpoint,
    CollectedRow,
    CollectionBatch,
    ReceiptDraft,
    RunState,
    SubmissionState,
    stable_key,
    utcnow_iso,
)


@dataclass
class Step:
    """RPA 脚本中的一个原子操作。"""

    name: str
    action: str          # open / fill / click / submit / query / screenshot
    target: str = ""     # 页面元素标识或 URL
    value: Any = None
    expect: str = ""     # 预期页面标识，不匹配即判定页面结构变化
    is_submit: bool = False
    is_query: bool = False


@dataclass
class RPAScript:
    """某个供应商门户的提交流程脚本与待提交数据。"""

    script_ref: str
    portal_url: str
    rows: list[CollectedRow]
    steps: list[Step]
    notes: list[str] = field(default_factory=list)

    def form_signature(self) -> str:
        """表单内容的稳定签名：数字或版本变化即得到不同签名。"""
        payload = [f"{r.reported_as}:{r.report_kind}:{r.quantity}:{r.revision}" for r in self.rows]
        return stable_key([self.script_ref, *payload])


class BrowserSession(Protocol):
    """浏览器驱动协议；生产环境可用 Playwright/Selenium 实现。"""

    def login(self) -> None: ...
    def do(self, step: Step) -> dict[str, Any]:
        """执行一步，返回 {'ok': bool, 'evidence': str, 'receipt': str|None}。"""


class FakeBrowser:
    """测试与演练用的脚本化浏览器。

    scenario 支持：

    - ``normal``      正常提交并返回明确回执；
    - ``login``       打开页面即登录过期；
    - ``structure``   预期标识不匹配（页面结构变化）；
    - ``ambiguous``   提交后无明确确认标识；
    - ``ambiguous_then_confirmed`` 首次不明确，恢复查询时确认成功。
    """

    def __init__(self, supplier: str, scenario: str = "normal", already_submitted: bool = False):
        self.supplier = supplier
        self.scenario = scenario
        # 恢复场景下浏览器是全新实例，但门户服务端仍保留已提交记录。
        self._submitted = already_submitted
        self._receipt = ""
        self.evidence: list[str] = []

    def login(self) -> None:
        if self.scenario == "login":
            raise LoginExpired(self.supplier, "portal/login")

    def do(self, step: Step) -> dict[str, Any]:
        self.evidence.append(f"{step.action}:{step.target or '-'}")
        if self.scenario == "structure" and step.expect:
            raise PageStructureChanged(self.supplier, f"标识 {step.expect}", step.target)

        if step.is_submit:
            # 门户侧同样按幂等键去重：重复提交直接拒绝。
            if self._submitted:
                raise PageStructureChanged(
                    self.supplier, "重复提交被门户拒绝（幂等键已存在）", step.target
                )
            self._submitted = True
            if self.scenario in ("ambiguous", "ambiguous_then_confirmed"):
                raise SubmissionAmbiguous(self.supplier, "<pending>")
            self._receipt = (
                f"RPA-RCPT-{self.supplier}-"
                f"{stable_key([step.name, utcnow_iso()])[:8]}"
            )
            return {
                "ok": True,
                "evidence": "submit-accepted",
                "receipt": self._receipt,
            }

        if step.is_query:
            if self._receipt:
                # 提交时已拿到明确回执，查询只做核验，返回同一回执。
                return {
                    "ok": True,
                    "evidence": "query-confirmed",
                    "receipt": self._receipt,
                }
            if self.scenario == "ambiguous_then_confirmed" and self._submitted:
                self._receipt = f"RPA-RCPT-{self.supplier}-CONFIRMED"
                return {
                    "ok": True,
                    "evidence": "query-confirmed",
                    "receipt": self._receipt,
                }
            if self._submitted:
                # 门户仍未给出明确回执，继续保持不明确，绝不重提。
                raise SubmissionAmbiguous(self.supplier, "<pending>")
            raise PageStructureChanged(self.supplier, "查询到无提交记录", step.target)

        return {"ok": True, "evidence": f"done:{step.name}", "receipt": None}


class RPADriver:
    """按脚本驱动网页提交，并通过仓库维护幂等状态与检查点。"""

    def __init__(self, repository):
        self.repo = repository

    def __init__(self, repository):
        self.repo = repository
        self._current_supplier = ""

    def idempotency_key(self, script: RPAScript) -> str:
        # 供应商是幂等维度之一：不同供应商即使门户 URL 与表单内容相同，
        # 也是彼此独立的提交，禁止跨供应商去重。
        return stable_key(["rpa", script.portal_url, script.form_signature(),
                           self._current_supplier])

    def run(self, run, script: RPAScript, browser: BrowserSession,
            actor: str = "rpa-bot") -> CollectionBatch:
        """执行（或恢复）一次网页提交。

        成功返回结构化采集批次；可恢复故障抛出对应 CollectionError，
        此时运行已置为 paused 并写入检查点。
        """
        self._current_supplier = run.supplier
        key = self.idempotency_key(script)
        existing = self.repo.get_rpa_submission(key)
        start_index = 0
        if existing:
            start_index = existing["step_index"]
            state = existing["state"]
            if state == SubmissionState.CONFIRMED.value:
                # 同一表单已确认，直接复用，绝不重复提交。
                self.repo.audit(
                    actor, "rpa.skip_duplicate", "rpa_submission", key,
                    {"reason": "同一表单已有确认回执", "receipt_id": existing["receipt_id"]},
                )
                return self._batch_from_script(run.supplier, script, [])
            if state == SubmissionState.SUBMITTED_PENDING.value:
                return self._resolve_pending(run, script, browser, key, actor)
        else:
            self.repo.save_rpa_submission(self._sub_dict(
                key, run, script, SubmissionState.REGISTERED, 0, [], None))

        try:
            browser.login()
        except LoginExpired:
            self._pause(run, script, key, 0, script.steps[0] if script.steps else None,
                        SubmissionState.REGISTERED, [], actor)
            raise

        evidence: list[str] = []
        receipt_id = None
        for index in range(start_index, len(script.steps)):
            step = script.steps[index]
            try:
                result = browser.do(step)
            except LoginExpired:
                self._pause(run, script, key, index, step,
                            self._current_state(evidence), evidence, actor)
                raise
            except PageStructureChanged:
                self._pause(run, script, key, index, step,
                            self._current_state(evidence), evidence, actor)
                raise
            except SubmissionAmbiguous as exc:
                exc.idempotency_key = key
                # 提交动作已在门户侧发生，证据先留存再暂停。
                evidence.append("submit-accepted")
                self._pause(
                    run, script, key, index, step,
                    SubmissionState.SUBMITTED_PENDING, evidence, actor,
                    instruction=exc.instruction,
                )
                raise

            evidence.append(result["evidence"])
            if step.is_submit:
                self.repo.save_rpa_submission(self._sub_dict(
                    key, run, script, SubmissionState.IN_PROGRESS, index + 1, evidence, None))
            if result.get("receipt"):
                receipt_id = self._store_receipt(run, script, key, result, evidence, actor)
                self.repo.save_rpa_submission(self._sub_dict(
                    key, run, script, SubmissionState.CONFIRMED, index + 1,
                    evidence, receipt_id))
            else:
                self.repo.save_rpa_submission(self._sub_dict(
                    key, run, script, SubmissionState.IN_PROGRESS, index + 1, evidence, receipt_id))

        if receipt_id is None:
            # 脚本跑完却没有明确回执，按结果不明确处理。
            exc = SubmissionAmbiguous(run.supplier, key)
            self._pause(run, script, key, len(script.steps), None,
                        SubmissionState.SUBMITTED_PENDING, evidence, actor,
                        instruction=exc.instruction)
            raise exc

        self.repo.update_run_state(run.run_id, RunState.RUNNING)
        self.repo.audit(actor, "rpa.confirmed", "rpa_submission", key,
                        {"receipt_id": receipt_id, "steps": len(script.steps)})
        return self._batch_from_script(run.supplier, script, evidence)

    def _resolve_pending(self, run, script: RPAScript, browser: BrowserSession,
                         key: str, actor: str) -> CollectionBatch:
        """提交结果不明确后的恢复：只执行查询步骤，绝不重新提交。"""
        query_steps = [(i, s) for i, s in enumerate(script.steps) if s.is_query]
        if not query_steps:
            exc = SubmissionAmbiguous(run.supplier, key)
            self._pause(run, script, key, len(script.steps), None,
                        SubmissionState.SUBMITTED_PENDING,
                        self.repo.get_rpa_submission(key)["evidence"], actor,
                        instruction=exc.instruction)
            raise exc
        try:
            browser.login()
            index, step = query_steps[-1]
            result = browser.do(step)
        except LoginExpired:
            self._pause(run, script, key, index, step,
                        SubmissionState.SUBMITTED_PENDING,
                        self.repo.get_rpa_submission(key)["evidence"], actor)
            raise
        except SubmissionAmbiguous as exc:
            exc.idempotency_key = key
            self._pause(run, script, key, index, step,
                        SubmissionState.SUBMITTED_PENDING,
                        self.repo.get_rpa_submission(key)["evidence"], actor,
                        instruction=exc.instruction)
            raise

        evidence = self.repo.get_rpa_submission(key)["evidence"] + [result["evidence"]]
        if not result.get("receipt"):
            exc = SubmissionAmbiguous(run.supplier, key)
            self._pause(run, script, key, index, step,
                        SubmissionState.SUBMITTED_PENDING, evidence, actor,
                        instruction=exc.instruction)
            raise exc
        receipt_id = self._store_receipt(run, script, key, result, evidence, actor)
        self.repo.save_rpa_submission(self._sub_dict(
            key, run, script, SubmissionState.CONFIRMED, index + 1, evidence, receipt_id))
        self.repo.audit(actor, "rpa.confirmed_after_query", "rpa_submission", key,
                        {"receipt_id": receipt_id})
        return self._batch_from_script(run.supplier, script, evidence)

    # ------------------------------------------------------------------ 辅助

    def _store_receipt(self, run, script: RPAScript, key: str,
                       result: dict[str, Any], evidence: list[str], actor: str) -> str:
        from ..models import RawReceipt
        receipt_id = result["receipt"]
        content = (
            f"portal={script.portal_url}\nkey={key}\n"
            f"evidence={evidence!r}\nconfirmation={receipt_id}\n"
        )
        self.repo.save_receipt(RawReceipt(
            receipt_id=receipt_id,
            supplier=run.supplier,
            media="rpa-html",
            source_ref=script.portal_url,
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            received_at=utcnow_iso(),
        ))
        return receipt_id

    def _pause(self, run, script: RPAScript, key: str, step_index: int,
               step: Step | None, state: SubmissionState, evidence: list[str],
               actor: str, instruction: str = "") -> None:
        checkpoint = Checkpoint(
            run_id=run.run_id,
            supplier=run.supplier,
            window=run.window,
            step_index=step_index,
            step_name=step.name if step else "(脚本末尾)",
            submission_state=state.value,
            idempotency_key=key,
            instruction=instruction or "修复后从检查点恢复；已提交步骤不会重放。",
            saved_at=utcnow_iso(),
        )
        self.repo.save_rpa_submission(self._sub_dict(
            key, run, script, state, step_index, evidence, None))
        self.repo.update_run_state(run.run_id, RunState.PAUSED,
                                   error=checkpoint.instruction, checkpoint=checkpoint)
        self.repo.audit(actor, "rpa.paused", "run", run.run_id,
                        {"step": checkpoint.step_name, "key": key, "state": state.value})

    @staticmethod
    def _current_state(evidence: list[str]) -> SubmissionState:
        return SubmissionState.IN_PROGRESS if evidence else SubmissionState.REGISTERED

    @staticmethod
    def _sub_dict(key: str, run, script: RPAScript, state: SubmissionState,
                  step_index: int, evidence: list[str], receipt_id: str | None) -> dict[str, Any]:
        return {
            "idempotency_key": key,
            "run_id": run.run_id,
            "supplier": run.supplier,
            "window": run.window,
            "script_ref": script.script_ref,
            "form_signature": script.form_signature(),
            "state": state.value,
            "step_index": step_index,
            "evidence": evidence,
            "receipt_id": receipt_id,
            "updated_at": utcnow_iso(),
        }

    @staticmethod
    def _batch_from_script(supplier: str, script: RPAScript,
                           evidence: list[str]) -> CollectionBatch:
        receipts = [
            ReceiptDraft(media="rpa-evidence", source_ref=script.portal_url,
                         content="\n".join(evidence) if evidence else "复用既有确认回执")
        ]
        return CollectionBatch(supplier=supplier, rows=list(script.rows), receipts=receipts)
