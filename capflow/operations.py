"""运营复核与有效产能计算。

运营人员是以下事实的唯一裁决来源：

- 两个型号是否真的共用同一设备（共享判断）；
- 维护停机影响哪些资源、时间窗、型号及影响数量；
- 替代班次是否有效（无效班次不增加产能）；
- 单条产能回报是否确认。

**未确认的产能不会出现在有效供应视图里，因此绝不可能被当作可分配余量。**
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    BasisEntry,
    CapacityRecord,
    OpsReviewEntry,
    RecordStatus,
    ReviewKind,
    SupplyBucket,
    stable_key,
    utcnow_iso,
)


@dataclass
class EffectiveSupply:
    """一个时间窗内可用于分配的有效产能。"""

    window: str
    buckets: dict[str, SupplyBucket] = field(default_factory=dict)
    excluded: list[tuple[str, str]] = field(default_factory=list)  # (record_id, 原因)


class OpsService:
    """记录运营判断并计算有效供应。"""

    def __init__(self, repository, actor: str = "ops"):
        self.repo = repository
        self.actor = actor

    # -------------------------------------------------------------- 复核录入

    def confirm_record(self, record_id: str, note: str = "") -> OpsReviewEntry:
        """确认一条产能回报，确认后才可参与分配。"""
        rec = self._require_record(record_id)
        if rec.status == RecordStatus.EXCEPTION.value:
            raise ValueError(f"记录 {record_id} 存在未解析异常，须先处理路由问题")
        self.repo.set_record_status(record_id, RecordStatus.CONFIRMED.value, note)
        return self._add_review(
            ReviewKind.RECORD, rec.resource or "", rec.window, rec.reported_as,
            rec.quantity, True, note or "运营确认产能回报",
        )

    def reject_record(self, record_id: str, reason: str) -> OpsReviewEntry:
        """驳回一条产能回报（如填报错误），驳回后不参与分配。"""
        rec = self._require_record(record_id)
        self.repo.set_record_status(record_id, RecordStatus.REJECTED.value, reason)
        return self._add_review(
            ReviewKind.RECORD, rec.resource or "", rec.window, rec.reported_as,
            rec.quantity, False, reason,
        )

    def register_downtime(self, resource: str, window: str, quantity: int,
                          reason: str, product: str = "", valid: bool = True) -> OpsReviewEntry:
        """登记维护停机；quantity 为该窗口受影响的产能件数。"""
        return self._add_review(
            ReviewKind.DOWNTIME, resource, window, product, quantity, valid, reason)

    def register_shift(self, resource: str, window: str, quantity: int,
                       reason: str, valid: bool = True, product: str = "") -> OpsReviewEntry:
        """登记替代班次；valid=False 表示运营判定该班次无效，不增加产能。"""
        return self._add_review(
            ReviewKind.SHIFT, resource, window, product, quantity, valid, reason)

    def set_sharing(self, resource: str, window: str, product: str, shareable: bool,
                    reason: str) -> OpsReviewEntry:
        """裁决某型号的产品级余量是否可共享给同资源的其他型号。"""
        return self._add_review(
            ReviewKind.SHARING, resource, window, product, 0, shareable, reason)

    # ---------------------------------------------------------- 有效产能计算

    def effective_supply(self, window: str) -> EffectiveSupply:
        """计算指定时间窗的有效供应。

        只纳入状态为 confirmed 的记录；同一 (供应商, 填报标识) 取最大 revision，
        门户覆盖旧表后旧版本仍留库但不再生效。
        """
        supply = EffectiveSupply(window=window)
        records = self.repo.list_capacity_records(window=window)
        latest = self._latest_revisions(records)
        reviews = self.repo.list_reviews(window=window)

        for rec in latest:
            if rec.status != RecordStatus.CONFIRMED.value:
                supply.excluded.append((rec.record_id, f"状态={rec.status}，未经运营确认"))
                continue
            bucket = supply.buckets.setdefault(rec.resource, SupplyBucket(resource=rec.resource))
            basis = BasisEntry(
                record_id=rec.record_id, supplier=rec.supplier,
                revision=rec.revision, receipt_id=rec.receipt_id, quantity=rec.quantity,
            )
            bucket.basis.append(basis)
            if rec.report_kind == "process":
                # 共享工序回报进入共享池，分配时整条产线产能只扣这一次。
                bucket.shared += rec.quantity
            else:
                # 产品型号回报先作为该产品的专用余量。
                bucket.earmark[rec.reported_as] = bucket.earmark.get(rec.reported_as, 0) + rec.quantity

        for entry in reviews:
            if not entry.resource or entry.resource not in supply.buckets:
                continue
            bucket = supply.buckets[entry.resource]
            if entry.kind == ReviewKind.DOWNTIME.value and entry.valid:
                amount = self._apply_review_amount(bucket, entry)
                bucket.downtime_total += amount
                bucket.notes.append(f"停机 {entry.reason}（{amount} 件）")
            elif entry.kind == ReviewKind.SHIFT.value:
                if entry.valid:
                    amount = entry.quantity
                    bucket.shift_total += amount
                    bucket.notes.append(f"有效替代班次 +{amount} 件：{entry.reason}")
                    self._add_to_target(bucket, entry, amount)
                else:
                    bucket.notes.append(f"替代班次判定无效，不计入：{entry.reason}")
            elif entry.kind == ReviewKind.SHARING.value and entry.product:
                if entry.valid:
                    surplus = bucket.earmark.pop(entry.product, 0)
                    if surplus:
                        bucket.shared += surplus
                        bucket.notes.append(
                            f"运营裁决 {entry.product} 余量 {surplus} 件可共享：{entry.reason}")
                else:
                    bucket.notes.append(
                        f"运营裁决 {entry.product} 余量不可共享，仅限本型号：{entry.reason}")
        return supply

    # ------------------------------------------------------------------ 辅助

    def _apply_review_amount(self, bucket: SupplyBucket, entry: OpsReviewEntry) -> int:
        amount = entry.quantity
        if entry.product and entry.product in bucket.earmark:
            used = min(amount, bucket.earmark[entry.product])
            bucket.earmark[entry.product] -= used
            amount -= used
            if bucket.earmark[entry.product] == 0:
                del bucket.earmark[entry.product]
        if amount > 0:
            used_shared = min(amount, bucket.shared)
            bucket.shared -= used_shared
            amount -= used_shared
        return entry.quantity - amount

    @staticmethod
    def _add_to_target(bucket: SupplyBucket, entry: OpsReviewEntry, amount: int) -> None:
        if entry.product:
            bucket.earmark[entry.product] = bucket.earmark.get(entry.product, 0) + amount
        else:
            bucket.shared += amount

    @staticmethod
    def _latest_revisions(records: list[CapacityRecord]) -> list[CapacityRecord]:
        chosen: dict[tuple[str, str], CapacityRecord] = {}
        for rec in records:
            key = (rec.supplier, rec.reported_as)
            cur = chosen.get(key)
            if cur is None or rec.revision > cur.revision:
                chosen[key] = rec
        return list(chosen.values())

    def _add_review(self, kind: ReviewKind, resource: str, window: str, product: str,
                    quantity: int, valid: bool, reason: str) -> OpsReviewEntry:
        review_id = "REV-" + stable_key([
            kind.value, resource, window, product, str(quantity), str(valid), reason,
            self.actor, utcnow_iso(),
        ])
        entry = OpsReviewEntry(
            review_id=review_id, kind=kind.value, resource=resource, window=window,
            product=product, quantity=quantity, valid=valid, reason=reason,
            reviewer=self.actor, created_at=utcnow_iso(),
        )
        self.repo.save_review(entry)
        self.repo.audit(self.actor, f"ops.{kind.value}", "ops_review", review_id,
                        {"resource": resource, "window": window, "product": product,
                         "quantity": quantity, "valid": valid, "reason": reason})
        return entry

    def _require_record(self, record_id: str) -> CapacityRecord:
        rec = self.repo.get_capacity_record(record_id)
        if rec is None:
            raise KeyError(f"产能记录不存在：{record_id}")
        return rec
