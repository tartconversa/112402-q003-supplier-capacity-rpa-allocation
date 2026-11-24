"""SQLite 持久化层。

设计要点：

- 原始回执独立留存并记录 SHA-256 哈希，后续表只通过 ``receipt_id`` 引用；
- 同一回报标识的多个版本（revision）全部保留，读取有效产能时只取最新版；
- 审计事件只追加不更新；
- RPA 提交以幂等键为唯一约束，杜绝重复提交；
- 计划快照（方案、明细、依据、事项）整体 JSON 落库，支持新旧方案对比。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import (
    ActionItem,
    ActionStatus,
    AuditEvent,
    BasisEntry,
    CapacityRecord,
    Checkpoint,
    CollectionRun,
    Order,
    OpsReviewEntry,
    Plan,
    PlanItem,
    RawReceipt,
    RoutingEntry,
    RunState,
    utcnow_iso,
)

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id   TEXT PRIMARY KEY,
    supplier     TEXT NOT NULL,
    media        TEXT NOT NULL,
    source_ref   TEXT NOT NULL,
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    supplier    TEXT NOT NULL,
    window      TEXT NOT NULL,
    connector   TEXT NOT NULL,
    state       TEXT NOT NULL,
    error       TEXT NOT NULL DEFAULT '',
    checkpoint  TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS capacity_records (
    record_id    TEXT PRIMARY KEY,
    supplier     TEXT NOT NULL,
    window       TEXT NOT NULL,
    reported_as  TEXT NOT NULL,
    report_kind  TEXT NOT NULL,
    resource     TEXT,
    quantity     INTEGER NOT NULL,
    revision     INTEGER NOT NULL,
    status       TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    receipt_id   TEXT NOT NULL REFERENCES receipts(receipt_id),
    run_id       TEXT NOT NULL REFERENCES runs(run_id),
    collected_at TEXT NOT NULL,
    UNIQUE(supplier, window, reported_as, revision)
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    detail      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rpa_submissions (
    idempotency_key TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    supplier        TEXT NOT NULL,
    window          TEXT NOT NULL,
    script_ref      TEXT NOT NULL,
    form_signature  TEXT NOT NULL,
    state           TEXT NOT NULL,
    step_index      INTEGER NOT NULL,
    evidence        TEXT NOT NULL,
    receipt_id      TEXT REFERENCES receipts(receipt_id),
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ops_reviews (
    review_id  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    resource   TEXT NOT NULL DEFAULT '',
    window     TEXT NOT NULL DEFAULT '',
    product    TEXT NOT NULL DEFAULT '',
    quantity   INTEGER NOT NULL DEFAULT 0,
    valid      INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    reviewer   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    product  TEXT NOT NULL,
    window   TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    due      TEXT NOT NULL,
    state    TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS routing (
    product       TEXT NOT NULL,
    resource      TEXT NOT NULL,
    units_per_lot INTEGER NOT NULL,
    valid_from    TEXT NOT NULL,
    PRIMARY KEY(product, resource, valid_from)
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id    TEXT PRIMARY KEY,
    window     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    note       TEXT NOT NULL,
    snapshot   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS action_items (
    action_id  TEXT PRIMARY KEY,
    plan_id    TEXT REFERENCES plans(plan_id),
    order_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    reason     TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Repository:
    """全部持久化操作的唯一入口。"""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ 审计

    def audit(self, actor: str, action: str, entity_type: str, entity_id: str,
              detail: dict[str, Any] | None = None) -> AuditEvent:
        """追加一条审计事件（永不更新、永不删除）。"""
        ts = utcnow_iso()
        cur = self.conn.execute(
            "INSERT INTO audit_events(ts, actor, action, entity_type, entity_id, detail)"
            " VALUES (?,?,?,?,?,?)",
            (ts, actor, action, entity_type, entity_id, json.dumps(detail or {}, ensure_ascii=False)),
        )
        self.conn.commit()
        return AuditEvent(cur.lastrowid, ts, actor, action, entity_type, entity_id, detail or {})

    def list_audit(self, entity_type: str = "", entity_id: str = "", limit: int = 200) -> list[AuditEvent]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: list[Any] = []
        if entity_type:
            sql += " AND entity_type=?"
            params.append(entity_type)
        if entity_id:
            sql += " AND entity_id=?"
            params.append(entity_id)
        sql += " ORDER BY event_id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            AuditEvent(
                r["event_id"], r["ts"], r["actor"], r["action"],
                r["entity_type"], r["entity_id"], json.loads(r["detail"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ 回执

    def save_receipt(self, receipt: RawReceipt) -> RawReceipt:
        self.conn.execute(
            "INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?)",
            (receipt.receipt_id, receipt.supplier, receipt.media, receipt.source_ref,
             receipt.content, receipt.content_hash, receipt.received_at),
        )
        self.conn.commit()
        return receipt

    def get_receipt(self, receipt_id: str) -> RawReceipt | None:
        r = self.conn.execute("SELECT * FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone()
        return self._row_to_receipt(r) if r else None

    def list_receipts(self, supplier: str = "") -> list[RawReceipt]:
        if supplier:
            rows = self.conn.execute(
                "SELECT * FROM receipts WHERE supplier=? ORDER BY received_at", (supplier,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM receipts ORDER BY received_at").fetchall()
        return [self._row_to_receipt(r) for r in rows]

    @staticmethod
    def _row_to_receipt(r: sqlite3.Row) -> RawReceipt:
        return RawReceipt(
            r["receipt_id"], r["supplier"], r["media"], r["source_ref"],
            r["content"], r["content_hash"], r["received_at"],
        )

    # ------------------------------------------------------------------ 运行

    def start_run(self, run: CollectionRun) -> CollectionRun:
        self.conn.execute(
            "INSERT INTO runs(run_id, supplier, window, connector, state, error, checkpoint,"
            " started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (run.run_id, run.supplier, run.window, run.connector, run.state, run.error,
             self._dump(run.checkpoint), run.started_at, run.finished_at or None),
        )
        self.conn.commit()
        return run

    def update_run_state(self, run_id: str, state: RunState, error: str = "",
                         checkpoint: Checkpoint | None = None, finished: bool = False) -> None:
        self.conn.execute(
            "UPDATE runs SET state=?, error=?, checkpoint=?, finished_at=? WHERE run_id=?",
            (state.value, error, self._dump(checkpoint),
             utcnow_iso() if finished else None, run_id),
        )
        self.conn.commit()

    def get_run(self, run_id: str) -> CollectionRun | None:
        r = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return self._row_to_run(r) if r else None

    def list_runs(self, supplier: str = "") -> list[CollectionRun]:
        if supplier:
            rows = self.conn.execute(
                "SELECT * FROM runs WHERE supplier=? ORDER BY started_at", (supplier,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM runs ORDER BY started_at").fetchall()
        return [self._row_to_run(r) for r in rows]

    @staticmethod
    def _dump(obj: Any) -> str | None:
        return json.dumps(obj, ensure_ascii=False, default=lambda o: o.__dict__) if obj else None

    def _row_to_run(self, r: sqlite3.Row) -> CollectionRun:
        cp = json.loads(r["checkpoint"]) if r["checkpoint"] else None
        checkpoint = Checkpoint(**cp) if cp else None
        return CollectionRun(
            r["run_id"], r["supplier"], r["window"], r["connector"], r["state"],
            r["error"], checkpoint, r["started_at"], r["finished_at"] or "",
        )

    # -------------------------------------------------------------- 产能记录

    def save_capacity_record(self, rec: CapacityRecord) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO capacity_records(record_id, supplier, window, reported_as,"
            " report_kind, resource, quantity, revision, status, note, receipt_id, run_id, collected_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.record_id, rec.supplier, rec.window, rec.reported_as, rec.report_kind,
             rec.resource, rec.quantity, rec.revision, rec.status, rec.note,
             rec.receipt_id, rec.run_id, rec.collected_at),
        )
        self.conn.commit()

    def get_capacity_record(self, record_id: str) -> CapacityRecord | None:
        r = self.conn.execute("SELECT * FROM capacity_records WHERE record_id=?", (record_id,)).fetchone()
        return self._row_to_record(r) if r else None

    def list_capacity_records(self, supplier: str = "", window: str = "",
                              status: str = "") -> list[CapacityRecord]:
        sql = "SELECT * FROM capacity_records WHERE 1=1"
        params: list[Any] = []
        for col, val in (("supplier", supplier), ("window", window), ("status", status)):
            if val:
                sql += f" AND {col}=?"
                params.append(val)
        sql += " ORDER BY window, supplier, reported_as, revision"
        return [self._row_to_record(r) for r in self.conn.execute(sql, params).fetchall()]

    def set_record_status(self, record_id: str, status: str, note: str = "") -> None:
        self.conn.execute(
            "UPDATE capacity_records SET status=?, note=COALESCE(NULLIF(?, ''), note) WHERE record_id=?",
            (status, note, record_id),
        )
        self.conn.commit()

    @staticmethod
    def _row_to_record(r: sqlite3.Row) -> CapacityRecord:
        return CapacityRecord(
            r["record_id"], r["supplier"], r["window"], r["reported_as"], r["report_kind"],
            r["resource"], r["quantity"], r["revision"], r["status"], r["note"],
            r["receipt_id"], r["run_id"], r["collected_at"],
        )

    # ------------------------------------------------------------- RPA 提交

    def save_rpa_submission(self, sub: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO rpa_submissions(idempotency_key, run_id, supplier, window,"
            " script_ref, form_signature, state, step_index, evidence, receipt_id, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sub["idempotency_key"], sub["run_id"], sub["supplier"], sub["window"],
             sub["script_ref"], sub["form_signature"], sub["state"], sub["step_index"],
             json.dumps(sub["evidence"], ensure_ascii=False), sub.get("receipt_id"),
             sub["updated_at"]),
        )
        self.conn.commit()

    def get_rpa_submission(self, idempotency_key: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT * FROM rpa_submissions WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["evidence"] = json.loads(r["evidence"])
        return d

    def list_rpa_submissions(self, run_id: str = "") -> list[dict[str, Any]]:
        if run_id:
            rows = self.conn.execute(
                "SELECT * FROM rpa_submissions WHERE run_id=? ORDER BY updated_at", (run_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM rpa_submissions ORDER BY updated_at").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(r["evidence"])
            out.append(d)
        return out

    # ------------------------------------------------------------- 运营复核

    def save_review(self, entry: OpsReviewEntry) -> None:
        self.conn.execute(
            "INSERT INTO ops_reviews(review_id, kind, resource, window, product, quantity,"
            " valid, reason, reviewer, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (entry.review_id, entry.kind, entry.resource, entry.window, entry.product,
             entry.quantity, int(entry.valid), entry.reason, entry.reviewer, entry.created_at),
        )
        self.conn.commit()

    def list_reviews(self, kind: str = "", resource: str = "", window: str = "") -> list[OpsReviewEntry]:
        sql = "SELECT * FROM ops_reviews WHERE 1=1"
        params: list[Any] = []
        for col, val in (("kind", kind), ("resource", resource), ("window", window)):
            if val:
                sql += f" AND {col}=?"
                params.append(val)
        sql += " ORDER BY created_at"
        rows = self.conn.execute(sql, params).fetchall()
        return [
            OpsReviewEntry(
                r["review_id"], r["kind"], r["resource"], r["window"], r["product"],
                r["quantity"], bool(r["valid"]), r["reason"], r["reviewer"], r["created_at"],
            )
            for r in rows
        ]

    # ----------------------------------------------------------- 订单与路由

    def upsert_order(self, order: Order) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO orders(order_id, product, window, quantity, due, state, priority)"
            " VALUES (?,?,?,?,?,?,?)",
            (order.order_id, order.product, order.window, order.quantity, order.due,
             order.state, order.priority),
        )
        self.conn.commit()

    def list_orders(self, window: str = "", state: str = "") -> list[Order]:
        sql = "SELECT * FROM orders WHERE 1=1"
        params: list[Any] = []
        if window:
            sql += " AND window=?"
            params.append(window)
        if state:
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY due, priority, order_id"
        return [
            Order(r["order_id"], r["product"], r["window"], r["quantity"], r["due"],
                  r["state"], r["priority"])
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def upsert_routing(self, entry: RoutingEntry) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO routing(product, resource, units_per_lot, valid_from)"
            " VALUES (?,?,?,?)",
            (entry.product, entry.resource, entry.units_per_lot, entry.valid_from),
        )
        self.conn.commit()

    def list_routing(self) -> list[RoutingEntry]:
        return [
            RoutingEntry(r["product"], r["resource"], r["units_per_lot"], r["valid_from"])
            for r in self.conn.execute(
                "SELECT * FROM routing ORDER BY product, valid_from").fetchall()
        ]

    def resource_for_product(self, product: str) -> str | None:
        """取产品当前生效路由的共享资源（valid_from 取最新一条）。"""
        r = self.conn.execute(
            "SELECT resource FROM routing WHERE product=? ORDER BY valid_from DESC LIMIT 1",
            (product,),
        ).fetchone()
        return r["resource"] if r else None

    def units_per_lot(self, product: str, resource: str) -> int:
        r = self.conn.execute(
            "SELECT units_per_lot FROM routing WHERE product=? AND resource=?"
            " ORDER BY valid_from DESC LIMIT 1",
            (product, resource),
        ).fetchone()
        return r["units_per_lot"] if r else 1

    # ------------------------------------------------------------- 计划快照

    def save_plan(self, plan: Plan) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO plans(plan_id, window, created_at, note, snapshot)"
            " VALUES (?,?,?,?,?)",
            (plan.plan_id, plan.window, plan.created_at, plan.note,
             json.dumps(_plan_to_dict(plan), ensure_ascii=False)),
        )
        for action in plan.actions:
            self.conn.execute(
                "INSERT OR REPLACE INTO action_items(action_id, plan_id, order_id, kind, reason,"
                " status, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (action.action_id, plan.plan_id, action.order_id, action.kind, action.reason,
                 action.status, json.dumps(action.detail, ensure_ascii=False), action.created_at),
            )
        self.conn.commit()

    def list_plans(self, window: str = "") -> list[Plan]:
        if window:
            rows = self.conn.execute(
                "SELECT * FROM plans WHERE window=? ORDER BY created_at", (window,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM plans ORDER BY created_at").fetchall()
        return [_plan_from_dict(json.loads(r["snapshot"])) for r in rows]

    def get_plan(self, plan_id: str) -> Plan | None:
        r = self.conn.execute("SELECT snapshot FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return _plan_from_dict(json.loads(r["snapshot"])) if r else None

    def list_actions(self, status: str = "") -> list[ActionItem]:
        sql = "SELECT * FROM action_items WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at"
        return [
            ActionItem(
                r["action_id"], r["order_id"], r["kind"], r["reason"], r["status"],
                json.loads(r["detail"]), r["created_at"],
            )
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def close_action(self, action_id: str) -> None:
        self.conn.execute(
            "UPDATE action_items SET status=? WHERE action_id=?",
            (ActionStatus.CLOSED.value, action_id),
        )
        self.conn.commit()


def _plan_to_dict(plan: Plan) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "window": plan.window,
        "created_at": plan.created_at,
        "note": plan.note,
        "items": [
            {
                "order_id": it.order_id,
                "product": it.product,
                "resource": it.resource,
                "window": it.window,
                "demanded": it.demanded,
                "allocated": it.allocated,
                "status": it.status,
                "next_step": it.next_step,
                "basis": [b.__dict__ for b in it.basis],
            }
            for it in plan.items
        ],
        "actions": [a.__dict__ for a in plan.actions],
    }


def _plan_from_dict(d: dict[str, Any]) -> Plan:
    items = [
        PlanItem(
            it["order_id"], it["product"], it["resource"], it["window"],
            it["demanded"], it["allocated"], it["status"],
            [BasisEntry(**b) for b in it["basis"]],
            it.get("next_step", ""),
        )
        for it in d["items"]
    ]
    actions = [ActionItem(**a) for a in d["actions"]]
    return Plan(d["plan_id"], d["window"], d["created_at"], d["note"], items, actions)
