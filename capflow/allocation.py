"""订单分配引擎。

不可违背的规则：

1. **只用确认后的有效产能**：输入来自 :class:`~capflow.operations.OpsService`
   计算出的有效供应，collected/rejected/exception 记录已被排除；
2. **共享工序产能只能使用一次**：每个资源维护单一池（共享池 + 产品专用余量），
   所有订单按顺序从同一个池扣减，不存在重复计算；
3. **已向客户确认的交期受保护**：confirmed 订单先满足并保留承诺记录；
   新回报使其不可兑现时产生 HELD 明细与改期/升级事项，
   **绝不静默挪用其他订单的产能去填补**，也不会把其他订单挤掉后不告知；
4. **临时停机影响多个型号**：运营登记停机后重跑分配，所有尚未承诺（planned）
   的订单在同一轮重新核对，输出完整的新方案与受影响清单；
5. 每条分配都带版本化依据（记录 ID、revision、回执 ID），可逐单追溯。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import (
    ActionItem,
    ActionKind,
    ActionStatus,
    BasisEntry,
    ItemStatus,
    Order,
    Plan,
    PlanItem,
    SupplyBucket,
    stable_key,
    utcnow_iso,
)

if TYPE_CHECKING:
    from .operations import EffectiveSupply


@dataclass
class AllocationResult:
    """单窗口分配结果。"""

    plan: Plan
    shortfall_orders: list[str]
    held_orders: list[str]


class AllocationEngine:
    def __init__(self, repository, actor: str = "planner"):
        self.repo = repository
        self.actor = actor

    def allocate_window(self, window: str, supply: EffectiveSupply,
                        plan_note: str = "") -> AllocationResult:
        """对一个时间窗执行完整分配。"""
        orders = self.repo.list_orders(window=window)
        ordered = sorted(orders, key=self._sort_key)

        items: list[PlanItem] = []
        actions: list[ActionItem] = []
        shortfall: list[str] = []
        held: list[str] = []
        # 上一版方案中的已分配数量，用于判断再分配是否使既有承诺恶化。
        previous = self._previous_plan_allocations(window)

        for order in ordered:
            resource = self.repo.resource_for_product(order.product)
            if resource is None:
                items.append(self._shortfall(
                    order, "?", 0, order.quantity,
                    "工艺路由缺失，无法定位共享资源，转运营处理"))
                shortfall.append(order.order_id)
                actions.append(self._action(
                    order, ActionKind.RECONCILE, "产品缺少工艺路由，产能记录存在异常待处理"))
                continue

            bucket = supply.buckets.get(resource)
            demand_lots = self._demand_lots(order, resource)
            available_lots = self._available_lots(bucket, order.product) if bucket else 0
            granted_lots = min(demand_lots, available_lots)
            granted_units = self._lots_to_units(order.product, resource, granted_lots)
            basis = self._consume(bucket, order.product, granted_lots, resource) if granted_lots else []

            if granted_units >= order.quantity:
                item = PlanItem(
                    order_id=order.order_id, product=order.product, resource=resource,
                    window=window, demanded=order.quantity, allocated=granted_units,
                    status=ItemStatus.ALLOCATED.value, basis=basis,
                    next_step="维持承诺" if order.state == "confirmed" else "可向客户确认交期",
                )
            elif order.state == "confirmed":
                item = self._held(window, order, resource, granted_units, basis,
                                  previous, actions)
                held.append(order.order_id)
            else:
                item = PlanItem(
                    order_id=order.order_id, product=order.product, resource=resource,
                    window=window, demanded=order.quantity, allocated=granted_units,
                    status=ItemStatus.SHORTFALL.value, basis=basis,
                    next_step=(f"缺口 {order.quantity - granted_units} 件，"
                               "等待新产能或按优先级改期"),
                )
                shortfall.append(order.order_id)
                actions.append(self._action(
                    order, ActionKind.RESCHEDULE,
                    f"有效产能不足，缺口 {order.quantity - granted_units} 件"))
            items.append(item)

        plan = Plan(
            plan_id="PLAN-" + stable_key([window, utcnow_iso()]),
            window=window,
            created_at=utcnow_iso(),
            note=plan_note or f"{window} 分配方案",
            items=items,
            actions=actions,
        )
        self.repo.save_plan(plan)
        self.repo.audit(self.actor, "allocate.window", "plan", plan.plan_id,
                        {"window": window, "orders": len(orders),
                         "shortfall": shortfall, "held": held})
        return AllocationResult(plan, shortfall, held)

    # ------------------------------------------------------------------ 规则

    @staticmethod
    def _sort_key(order: Order) -> tuple[int, int, str, str]:
        # confirmed 优先于 planned；同级按优先级（数字小者优先）、交期、单号排序。
        confirmed_first = 0 if order.state == "confirmed" else 1
        return confirmed_first, order.priority, order.due, order.order_id

    def _demand_lots(self, order: Order, resource: str) -> int:
        upl = self.repo.units_per_lot(order.product, resource)
        return -(-order.quantity // upl)  # 向上取整

    def _lots_to_units(self, product: str, resource: str, lots: int) -> int:
        return lots * self.repo.units_per_lot(product, resource)

    @staticmethod
    def _available_lots(bucket: SupplyBucket | None, product: str) -> int:
        if bucket is None:
            return 0
        return bucket.shared + bucket.earmark.get(product, 0)

    @staticmethod
    def _consume(bucket: SupplyBucket, product: str, lots: int,
                 resource: str) -> list[BasisEntry]:
        """从单一资源池扣减并产生按记录版本拆分的依据。

        扣减顺序：先该产品专用余量，再共享池；每个 basis 记录实际用量，
        保证同一条共享工序产能在整个方案中只被扣一次。
        """
        remaining = lots
        basis: list[BasisEntry] = []
        earmark = bucket.earmark.get(product, 0)
        if earmark:
            take = min(remaining, earmark)
            bucket.earmark[product] = earmark - take
            if bucket.earmark[product] == 0:
                del bucket.earmark[product]
            remaining -= take
            AllocationEngine._allocate_basis(bucket.basis, basis, take, prefer_product=product)
        if remaining:
            take = min(remaining, bucket.shared)
            bucket.shared -= take
            remaining -= take
            AllocationEngine._allocate_basis(bucket.basis, basis, take, prefer_product=None)
        return basis

    @staticmethod
    def _allocate_basis(source: list[BasisEntry], target: list[BasisEntry],
                        lots: int, prefer_product: str | None) -> None:
        """把扣减量按 FIFO 摊到版本化依据上（依据按批/件计量，演示中与 lot 等价）。"""
        need = lots
        for entry in source:
            if need <= 0:
                break
            take = min(need, entry.quantity)
            entry.quantity -= take
            target.append(BasisEntry(
                record_id=entry.record_id, supplier=entry.supplier,
                revision=entry.revision, receipt_id=entry.receipt_id, quantity=take,
            ))
            need -= take
        source[:] = [e for e in source if e.quantity > 0]

    def _held(self, window: str, order: Order, resource: str, granted: int,
              basis: list[BasisEntry], prior: dict[str, int],
              actions: list[ActionItem]) -> PlanItem:
        gap = order.quantity - granted
        previously = prior.get(order.order_id)
        if gap <= 0:
            return PlanItem(
                order_id=order.order_id, product=order.product, resource=resource,
                window=window, demanded=order.quantity, allocated=granted,
                status=ItemStatus.HELD.value, basis=basis,
                next_step="保留已确认承诺",
            )
        if previously is None:
            reason = (f"已确认交期无法兑现：{resource} 仅可覆盖 {granted}/"
                      f"{order.quantity} 件，缺口 {gap} 件")
        elif granted < previously:
            reason = (f"停机/更正后承诺进一步受损：可覆盖由 {previously} 降至 {granted} 件，"
                      f"当前缺口 {gap} 件")
        else:
            reason = f"已确认交期仍有 {gap} 件缺口待处理"
        actions.append(self._action(order, ActionKind.ESCALATE, reason, extra={
            "resource": resource, "granted": granted, "gap": gap,
            "previously_allocated": previously,
            "rule": "保留承诺记录，不挪用其他订单产能",
        }))
        return PlanItem(
            order_id=order.order_id, product=order.product, resource=resource,
            window=window, demanded=order.quantity, allocated=granted,
            status=ItemStatus.HELD.value, basis=basis,
            next_step="升级处理：联系客户改期或追加供应，原承诺记录保留",
        )

    @staticmethod
    def _shortfall(order: Order, resource: str, granted: int, gap: int,
                   next_step: str) -> PlanItem:
        return PlanItem(
            order_id=order.order_id, product=order.product, resource=resource,
            window=order.window, demanded=order.quantity, allocated=granted,
            status=ItemStatus.SHORTFALL.value, basis=[], next_step=next_step,
        )

    def _action(self, order: Order, kind: ActionKind, reason: str,
                extra: dict | None = None) -> ActionItem:
        return ActionItem(
            action_id="ACT-" + stable_key([order.order_id, kind.value, reason]),
            order_id=order.order_id,
            kind=kind.value,
            reason=reason,
            status=ActionStatus.OPEN.value,
            detail=extra or {"product": order.product, "window": order.window,
                             "due": order.due},
            created_at=utcnow_iso(),
        )

    def _previous_plan_allocations(self, window: str) -> dict[str, int]:
        """读取再分配之前最后一版方案的已分配数量；首版方案返回空字典。"""
        plans = self.repo.list_plans(window=window)
        if not plans:
            return {}
        latest = plans[-1]
        return {
            it.order_id: it.allocated
            for it in latest.items
            if it.status in (ItemStatus.ALLOCATED.value, ItemStatus.HELD.value)
        }
