"""CSV/JSON 样例数据加载器（仅使用标准库）。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .models import CollectedRow, Order, RoutingEntry


def load_orders(path: str | Path) -> list[Order]:
    """读取订单 CSV；priority 列缺省为 0。"""
    orders: list[Order] = []
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh, strict=True):
            orders.append(Order(
                order_id=row["order_id"],
                product=row["product"],
                window=row["week"],
                quantity=int(row["quantity"]),
                due=row["due"],
                state=row["state"],
                priority=int(row.get("priority") or 0),
            ))
    return orders


def load_routing(path: str | Path) -> list[RoutingEntry]:
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return [
            RoutingEntry(
                product=row["product"],
                resource=row["resource"],
                units_per_lot=int(row["units_per_lot"]),
                valid_from=row["valid_from"],
            )
            for row in csv.DictReader(fh, strict=True)
        ]


def load_export_json(path: str | Path) -> list[CollectedRow]:
    """读取门户导出 JSON 并转为结构化行（不做容错，契约错误交给连接器抛出）。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        CollectedRow(
            window=payload["window"],
            reported_as=e["reported_as"],
            report_kind=e["report_kind"],
            quantity=e["quantity"],
            revision=e["revision"],
        )
        for e in payload["entries"]
    ]
