"""方案快照对比：门户更正或人工复核后比较原分配与新方案。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Plan, ActionItem


@dataclass
class OrderDiff:
    """单个受影响订单的前后对比与下一步。"""

    order_id: str
    product: str
    resource: str
    before_status: str
    after_status: str
    before_allocated: int
    after_allocated: int
    delta: int
    next_step: str
    basis_revision_after: list[int] = field(default_factory=list)
    changed: bool = False


@dataclass
class PlanComparison:
    """两个方案的结构化对比。"""

    before_id: str
    after_id: str
    window: str
    diffs: list[OrderDiff]
    new_actions: list[ActionItem]
    closed_gap_orders: list[str]

    def affected(self) -> list[OrderDiff]:
        return [d for d in self.diffs if d.changed]


class PlanningService:
    def __init__(self, repository):
        self.repo = repository

    def compare(self, before: Plan, after: Plan) -> PlanComparison:
        """逐订单比较两个方案，并列出新方案新增的处理事项。"""
        before_map = {it.order_id: it for it in before.items}
        after_map = {it.order_id: it for it in after.items}
        diffs: list[OrderDiff] = []

        for order_id in sorted(set(before_map) | set(after_map)):
            old = before_map.get(order_id)
            new = after_map.get(order_id)
            if old and new:
                changed = (
                    old.status != new.status
                    or old.allocated != new.allocated
                    or {b.record_id for b in old.basis} != {b.record_id for b in new.basis}
                )
                diffs.append(OrderDiff(
                    order_id=order_id, product=new.product, resource=new.resource,
                    before_status=old.status, after_status=new.status,
                    before_allocated=old.allocated, after_allocated=new.allocated,
                    delta=new.allocated - old.allocated,
                    next_step=new.next_step,
                    basis_revision_after=sorted({b.revision for b in new.basis}),
                    changed=changed,
                ))
            elif new:
                diffs.append(OrderDiff(
                    order_id=order_id, product=new.product, resource=new.resource,
                    before_status="(无)", after_status=new.status,
                    before_allocated=0, after_allocated=new.allocated,
                    delta=new.allocated, next_step=new.next_step,
                    basis_revision_after=sorted({b.revision for b in new.basis}),
                    changed=True,
                ))
            else:
                assert old is not None
                diffs.append(OrderDiff(
                    order_id=order_id, product=old.product, resource=old.resource,
                    before_status=old.status, after_status="(已移除)",
                    before_allocated=old.allocated, after_allocated=0,
                    delta=-old.allocated, next_step="订单不再参与本窗口分配",
                    changed=True,
                ))

        old_action_keys = {(a.order_id, a.kind, a.reason) for a in before.actions}
        new_actions = [
            a for a in after.actions
            if (a.order_id, a.kind, a.reason) not in old_action_keys
        ]
        # 原方案有缺口、新方案已补足的订单。
        closed_gap = [
            d.order_id for d in diffs
            if d.before_status == "shortfall" and d.after_status == "allocated"
        ]
        return PlanComparison(
            before_id=before.plan_id, after_id=after.plan_id,
            window=after.window, diffs=diffs, new_actions=new_actions,
            closed_gap_orders=closed_gap,
        )

    def history(self, window: str) -> list[Plan]:
        """按时间顺序返回一个窗口的全部方案快照。"""
        return self.repo.list_plans(window=window)
