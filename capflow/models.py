"""领域模型：采集、复核、分配各环节共用的数据结构。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow_iso() -> str:
    """返回带时区的 ISO8601 时间戳。"""
    return datetime.now(timezone.utc).isoformat()


def stable_key(parts: list[str]) -> str:
    """根据稳定字段生成幂等键（同输入永远得到同键）。"""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


class RunState(str, Enum):
    """单个供应商一次采集流程的状态。"""

    RUNNING = "running"
    PAUSED = "paused"          # 出现可恢复故障，等待人工或后续恢复
    COMPLETED = "completed"
    FAILED = "failed"


class RecordStatus(str, Enum):
    """产能回报记录的复核状态。"""

    COLLECTED = "collected"    # 已采集，尚未经运营确认
    CONFIRMED = "confirmed"    # 运营确认，可进入分配
    REJECTED = "rejected"      # 运营驳回
    EXCEPTION = "exception"    # 无法解析（如路由缺失），待人工处理


class SubmissionState(str, Enum):
    """RPA 网页提交的幂等状态机。"""

    REGISTERED = "registered"
    IN_PROGRESS = "in_progress"
    SUBMITTED_PENDING = "submitted_pending"  # 已提交但回执不明确
    CONFIRMED = "confirmed"                  # 门户已给出明确回执
    FAILED = "failed"


class ReviewKind(str, Enum):
    """运营复核事项类型。"""

    DOWNTIME = "downtime"            # 维护停机
    SHIFT = "shift"                  # 替代班次
    SHARING = "sharing"              # 型号是否共用设备/余量是否可共享
    RECORD = "record"                # 单条产能记录的确认/驳回


class ItemStatus(str, Enum):
    """分配明细状态。"""

    ALLOCATED = "allocated"
    SHORTFALL = "shortfall"          # 计划订单产能不足
    HELD = "held"                    # 已对客户承诺，保留承诺但产能无法兑现


class ActionKind(str, Enum):
    """违约或异常时生成的处理事项。"""

    RESCHEDULE = "reschedule"        # 改期
    ESCALATE = "escalate"            # 升级
    REALLOCATE = "reallocate"        # 重新核对/重排
    RECONCILE = "reconcile"          # 供应数据对账


class ActionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass
class Checkpoint:
    """流程暂停时的可恢复位置。"""

    run_id: str
    supplier: str
    window: str
    step_index: int
    step_name: str
    submission_state: str
    idempotency_key: str
    instruction: str
    saved_at: str


@dataclass
class RawReceipt:
    """供应商门户/接口的原始回执，原样留存。"""

    receipt_id: str
    supplier: str
    media: str           # export-json / rpa-html / rpa-evidence 等
    source_ref: str      # URL 或页面标识
    content: str
    content_hash: str
    received_at: str


@dataclass
class CapacityRecord:
    """一条产能回报（按产品型号或共享工序填报）。"""

    record_id: str
    supplier: str
    window: str
    reported_as: str          # 门户上的原始填报标识
    report_kind: str          # product / process
    resource: str | None      # 经工艺路由归一化后的共享资源；无法解析为 None
    quantity: int
    revision: int
    status: str
    note: str
    receipt_id: str
    run_id: str
    collected_at: str


@dataclass
class CollectionRun:
    """一次供应商采集流程。"""

    run_id: str
    supplier: str
    window: str
    connector: str
    state: str
    error: str = ""
    checkpoint: Checkpoint | None = None
    started_at: str = ""
    finished_at: str = ""


@dataclass
class AuditEvent:
    """审计事件。"""

    event_id: int
    ts: str
    actor: str
    action: str
    entity_type: str
    entity_id: str
    detail: dict[str, Any]


@dataclass
class OpsReviewEntry:
    """运营人员的判断记录（停机、班次、共享、记录结论）。"""

    review_id: str
    kind: str
    resource: str
    window: str
    product: str
    quantity: int
    valid: bool
    reason: str
    reviewer: str
    created_at: str


@dataclass
class Order:
    """客户订单。"""

    order_id: str
    product: str
    window: str
    quantity: int
    due: str
    state: str           # confirmed / planned
    priority: int = 0    # 数字越小优先级越高


@dataclass
class RoutingEntry:
    """产品经过共享设备的工艺路由。"""

    product: str
    resource: str
    units_per_lot: int
    valid_from: str


@dataclass
class CollectedRow:
    """连接器解析出的单条产能数字。"""

    window: str
    reported_as: str
    report_kind: str      # product / process
    quantity: int
    revision: int


@dataclass
class ReceiptDraft:
    """待落库的原始回执内容。"""

    media: str
    source_ref: str
    content: str


@dataclass
class CollectionBatch:
    """一次成功采集的结果：原始回执加结构化行。"""

    supplier: str
    rows: list[CollectedRow]
    receipts: list[ReceiptDraft] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class BasisEntry:
    """分配消耗依据：指向具体版本的产能记录与回执。"""

    record_id: str
    supplier: str
    revision: int
    receipt_id: str
    quantity: int


@dataclass
class PlanItem:
    """单个订单的分配结果。"""

    order_id: str
    product: str
    resource: str
    window: str
    demanded: int
    allocated: int
    status: str
    basis: list[BasisEntry] = field(default_factory=list)
    next_step: str = ""


@dataclass
class ActionItem:
    """改期/升级等处理事项。"""

    action_id: str
    order_id: str
    kind: str
    reason: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass
class Plan:
    """一个时间窗口的完整分配方案。"""

    plan_id: str
    window: str
    created_at: str
    note: str
    items: list[PlanItem] = field(default_factory=list)
    actions: list[ActionItem] = field(default_factory=list)


@dataclass
class SupplyBucket:
    """单个共享资源在一个窗口的有效产能视图。"""

    resource: str
    shared: int = 0                                  # 共享池数量
    earmark: dict[str, int] = field(default_factory=dict)  # 产品专用数量
    basis: list[BasisEntry] = field(default_factory=list)
    downtime_total: int = 0
    shift_total: int = 0
    notes: list[str] = field(default_factory=list)


def to_dict(obj: Any) -> Any:
    """把数据类转为可 JSON 序列化的普通结构。"""
    return asdict(obj)
