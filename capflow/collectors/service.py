"""采集编排服务。

职责：

- 为每个供应商建立独立的采集运行（run），单个供应商故障不影响其他供应商；
- 连接器成功后，原始回执原样落库（含哈希），结构化数字归一化为产能记录；
- 产品/工序标识经工艺路由归一到共享资源，无法解析的记录标记为异常，
  绝不静默猜测；
- 门户覆盖旧表时新版本作为新 revision 全量保留，旧版本不删除；
- 可恢复故障统一暂停并保留检查点，调用方按 run_id 恢复。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..errors import CollectionError
from ..models import (
    CapacityRecord,
    Checkpoint,
    CollectionBatch,
    CollectionRun,
    RawReceipt,
    RecordStatus,
    RunState,
    stable_key,
    utcnow_iso,
)
from .rpa import RPADriver, RPAScript, BrowserSession


@dataclass
class CollectionOutcome:
    """一轮采集的汇总，便于 CLI/测试呈现。"""

    completed: list[str]
    paused: list[tuple[str, str]]   # (供应商, 恢复说明)


class CollectionService:
    def __init__(self, repository, actor: str = "collector"):
        self.repo = repository
        self.actor = actor
        self.rpa = RPADriver(repository)

    # ---------------------------------------------------------- 导出接口采集

    def collect_export(self, supplier: str, window: str, connector) -> CollectionRun:
        """通过授权导出接口采集一个供应商一个时间窗的回报。"""
        run = self._new_run(supplier, window, connector.kind)
        try:
            batch = connector.collect()
        except CollectionError as exc:
            # 连接器内部已给出明确错误；运行置暂停（导出类无步骤级检查点）。
            self._pause_simple(run, connector.source_ref)
            self.repo.audit(self.actor, "collect.export_paused", "run", run.run_id,
                            {"supplier": supplier, "error": str(exc)})
            raise
        self._persist_batch(run, batch)
        self.repo.update_run_state(run.run_id, RunState.COMPLETED, finished=True)
        self.repo.audit(self.actor, "collect.export_completed", "run", run.run_id,
                        {"supplier": supplier, "window": window, "rows": len(batch.rows)})
        return self.repo.get_run(run.run_id)

    # -------------------------------------------------------------- RPA 采集

    def collect_rpa(self, supplier: str, window: str, script: RPAScript,
                    browser: BrowserSession) -> CollectionRun:
        """通过可审计 RPA 完成需要网页提交确认的采集。"""
        run = self._new_run(supplier, window, f"rpa:{script.script_ref}")
        try:
            batch = self.rpa.run(run, script, browser, actor=self.actor)
        except CollectionError:
            # RPADriver 已写入步骤级检查点并将 run 置为 paused。
            raise
        self._persist_batch(run, batch)
        self.repo.update_run_state(run.run_id, RunState.COMPLETED, finished=True)
        self.repo.audit(self.actor, "collect.rpa_completed", "run", run.run_id,
                        {"supplier": supplier, "window": window, "rows": len(batch.rows)})
        return self.repo.get_run(run.run_id)

    def resume_rpa(self, run_id: str, script: RPAScript,
                   browser: BrowserSession) -> CollectionRun:
        """从检查点恢复一个暂停的 RPA 流程。"""
        run = self.repo.get_run(run_id)
        if run is None:
            raise KeyError(f"采集运行不存在：{run_id}")
        if run.state != RunState.PAUSED.value:
            raise ValueError(f"运行 {run_id} 状态为 {run.state}，无需恢复")
        self.repo.update_run_state(run.run_id, RunState.RUNNING)
        batch = self.rpa.run(run, script, browser, actor=self.actor)
        self._persist_batch(run, batch)
        self.repo.update_run_state(run.run_id, RunState.COMPLETED, finished=True)
        self.repo.audit(self.actor, "collect.rpa_resumed", "run", run.run_id,
                        {"supplier": run.supplier, "window": run.window})
        return self.repo.get_run(run.run_id)

    # -------------------------------------------------------------- 内部实现

    def _new_run(self, supplier: str, window: str, connector: str) -> CollectionRun:
        run_id = "RUN-" + stable_key([supplier, window, connector, utcnow_iso()])
        run = CollectionRun(
            run_id=run_id, supplier=supplier, window=window, connector=connector,
            state=RunState.RUNNING.value, started_at=utcnow_iso(),
        )
        self.repo.start_run(run)
        self.repo.audit(self.actor, "collect.run_started", "run", run_id,
                        {"supplier": supplier, "window": window, "connector": connector})
        return run

    def _pause_simple(self, run: CollectionRun, source_ref: str) -> None:
        checkpoint = Checkpoint(
            run_id=run.run_id, supplier=run.supplier, window=run.window,
            step_index=0, step_name="fetch",
            submission_state="registered",
            idempotency_key="",
            instruction="修复授权或字段映射后重新触发该供应商采集。",
            saved_at=utcnow_iso(),
        )
        self.repo.update_run_state(run.run_id, RunState.PAUSED,
                                   error="采集暂停", checkpoint=checkpoint)

    def _persist_batch(self, run: CollectionRun, batch: CollectionBatch) -> None:
        if not batch.receipts:
            # 没有外部回执时也要留存一份内部采集凭证，保证记录可追溯。
            from ..models import ReceiptDraft
            payload = "\n".join(
                f"{r.reported_as}|{r.report_kind}|{r.quantity}|rev{r.revision}"
                for r in batch.rows
            )
            batch.receipts.append(ReceiptDraft(
                media="internal",
                source_ref=run.connector,
                content=f"run={run.run_id}\nsupplier={batch.supplier}\n{payload}",
            ))
        last_receipt = ""
        for draft in batch.receipts:
            digest = hashlib.sha256(draft.content.encode("utf-8")).hexdigest()
            receipt_id = "RCPT-" + digest[:16]
            self.repo.save_receipt(RawReceipt(
                receipt_id=receipt_id,
                supplier=batch.supplier,
                media=draft.media,
                source_ref=draft.source_ref,
                content=draft.content,
                content_hash=digest,
                received_at=utcnow_iso(),
            ))
            last_receipt = receipt_id

        for row in batch.rows:
            resource = self.repo.resource_for_product(row.reported_as) \
                if row.report_kind == "product" else row.reported_as
            status = RecordStatus.COLLECTED.value
            note = ""
            if resource is None:
                # 路由无法解析：不猜测、不参与分配，转人工复核。
                status = RecordStatus.EXCEPTION.value
                note = "工艺路由中找不到该产品对应的共享资源"
            record_id = "CAP-" + stable_key([
                batch.supplier, row.window, row.reported_as, str(row.revision),
            ])
            self.repo.save_capacity_record(CapacityRecord(
                record_id=record_id,
                supplier=batch.supplier,
                window=row.window,
                reported_as=row.reported_as,
                report_kind=row.report_kind,
                resource=resource,
                quantity=row.quantity,
                revision=row.revision,
                status=status,
                note=note,
                receipt_id=last_receipt,
                run_id=run.run_id,
                collected_at=utcnow_iso(),
            ))
            self.repo.audit(self.actor, "collect.record_stored", "capacity_record", record_id,
                            {"supplier": batch.supplier, "window": row.window,
                             "reported_as": row.reported_as, "revision": row.revision,
                             "quantity": row.quantity, "status": status})
