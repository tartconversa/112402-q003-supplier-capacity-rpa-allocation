"""capflow 命令行入口。

示例（在仓库根目录执行）：

    python3 -m capflow.cli load-samples --db data/capflow.db
    python3 -m capflow.cli collect-export --db data/capflow.db --supplier V-1 \
        --window 2026-W45 --file samples/exports/v1_w45_rev1.json
    python3 -m capflow.cli runs --db data/capflow.db
    python3 -m capflow.cli confirm-all --db data/capflow.db --window 2026-W45
    python3 -m capflow.cli supply --db data/capflow.db --window 2026-W45
    python3 -m capflow.cli allocate --db data/capflow.db --window 2026-W45
    python3 -m capflow.cli compare --db data/capflow.db --window 2026-W45
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .allocation import AllocationEngine
from .collectors import CollectionService, FakeBrowser, JSONExportConnector, RPAScript, Step
from .errors import CollectionError
from .models import RecordStatus, ReviewKind
from .operations import OpsService
from .planning import PlanningService
from .reference import load_orders, load_routing
from .repository import Repository


# --------------------------------------------------------------------- 渲染

def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def show_runs(repo: Repository) -> None:
    _print_header("采集运行")
    for run in repo.list_runs():
        line = f"{run.run_id}  {run.supplier}  {run.window}  {run.connector}  -> {run.state}"
        print(line)
        if run.checkpoint:
            cp = run.checkpoint
            print(f"    暂停位置: 步骤 {cp.step_index} ({cp.step_name})，"
                  f"提交状态={cp.submission_state}，幂等键={cp.idempotency_key}")
            print(f"    恢复说明: {cp.instruction}")


def show_records(repo: Repository, window: str) -> None:
    _print_header(f"产能记录（{window}）")
    for rec in repo.list_capacity_records(window=window):
        print(f"{rec.record_id}  {rec.supplier}  {rec.report_kind:7s}  "
              f"{rec.reported_as:10s} -> {rec.resource or '?':8s}  "
              f"rev{rec.revision}  {rec.quantity:4d} 件  [{rec.status}]  {rec.note}")


def show_supply(repo: Repository, window: str) -> None:
    supply = OpsService(repo).effective_supply(window)
    _print_header(f"有效供应（{window}，仅含运营确认的最新版本）")
    for resource, bucket in sorted(supply.buckets.items()):
        print(f"资源 {resource}: 共享池={bucket.shared} 件, 产品专用={bucket.earmark}")
        for note in bucket.notes:
            print(f"    - {note}")
        for b in bucket.basis:
            print(f"    依据: {b.supplier} rev{b.revision} {b.record_id} "
                  f"回执={b.receipt_id} 剩余={b.quantity}")
    for record_id, reason in supply.excluded:
        print(f"    排除: {record_id}（{reason}）")


def show_plan(plan) -> None:
    _print_header(f"方案 {plan.plan_id}（{plan.window}）{plan.note}")
    for it in plan.items:
        basis = ", ".join(f"{b.supplier}/rev{b.revision}/{b.quantity}件" for b in it.basis) or "无"
        print(f"{it.order_id}  {it.product:8s} 需{it.demanded:3d} 分{it.allocated:3d}  "
              f"[{it.status:9s}] 资源={it.resource} 依据=[{basis}]")
        print(f"    下一步: {it.next_step}")
    if plan.actions:
        _print_header("处理事项")
        for act in plan.actions:
            print(f"{act.action_id}  {act.order_id}  {act.kind:10s} [{act.status}] {act.reason}")


# --------------------------------------------------------------------- 命令

def cmd_load_samples(args, repo: Repository) -> None:
    root = Path(args.samples)
    for routing in load_routing(root / "routing.csv"):
        repo.upsert_routing(routing)
    for order in load_orders(root / "orders.csv"):
        repo.upsert_order(order)
    print(f"已载入路由与订单：{root}")


def cmd_collect_export(args, repo: Repository) -> None:
    service = CollectionService(repo)
    connector = JSONExportConnector(args.supplier, args.file)
    try:
        run = service.collect_export(args.supplier, args.window, connector)
    except CollectionError as exc:
        print(f"采集暂停：{exc}")
        print(f"恢复说明：{exc.instruction}")
        show_runs(repo)
        return
    print(f"采集完成：{run.run_id}")
    show_records(repo, args.window)


def _build_demo_script(supplier: str, window: str, scenario_file: Path) -> RPAScript:
    payload = json.loads(scenario_file.read_text(encoding="utf-8"))
    from .models import CollectedRow
    rows = [CollectedRow(window=window, **e) for e in payload["entries"]]
    steps = [Step(**s) for s in payload["steps"]]
    return RPAScript(
        script_ref=payload["script_ref"],
        portal_url=payload["portal_url"],
        rows=rows,
        steps=steps,
    )


def cmd_collect_rpa(args, repo: Repository) -> None:
    service = CollectionService(repo)
    script = _build_demo_script(args.supplier, args.window, Path(args.scenario))
    browser = FakeBrowser(args.supplier, scenario=args.scenario_name)
    try:
        service.collect_rpa(args.supplier, args.window, script, browser)
        print("RPA 采集完成，门户已给出明确回执")
    except CollectionError as exc:
        print(f"RPA 流程暂停：{exc}")
        print(f"恢复说明：{exc.instruction}")
    show_runs(repo)


def cmd_resume_rpa(args, repo: Repository) -> None:
    service = CollectionService(repo)
    script = _build_demo_script(args.supplier, args.window, Path(args.scenario))
    browser = FakeBrowser(args.supplier, scenario=args.scenario_name,
                          already_submitted=not args.fresh_browser)
    try:
        service.resume_rpa(args.run_id, script, browser)
        print("RPA 流程已恢复并完成")
    except CollectionError as exc:
        print(f"恢复后仍暂停：{exc}")
        print(f"恢复说明：{exc.instruction}")
    show_runs(repo)


def cmd_runs(args, repo: Repository) -> None:
    show_runs(repo)


def cmd_records(args, repo: Repository) -> None:
    show_records(repo, args.window)


def cmd_confirm_all(args, repo: Repository) -> None:
    ops = OpsService(repo, actor=args.actor)
    n = 0
    for rec in repo.list_capacity_records(window=args.window,
                                          status=RecordStatus.COLLECTED.value):
        ops.confirm_record(rec.record_id, note="批量确认（演练）")
        n += 1
    print(f"已确认 {n} 条回报；异常/驳回记录不会被确认")


def cmd_review(args, repo: Repository) -> None:
    ops = OpsService(repo, actor=args.actor)
    if args.kind == ReviewKind.DOWNTIME.value:
        entry = ops.register_downtime(args.resource, args.window, args.quantity,
                                      args.reason, product=args.product or "")
    elif args.kind == ReviewKind.SHIFT.value:
        entry = ops.register_shift(args.resource, args.window, args.quantity,
                                   args.reason, valid=args.valid,
                                   product=args.product or "")
    elif args.kind == ReviewKind.SHARING.value:
        entry = ops.set_sharing(args.resource, args.window, args.product,
                                args.valid, args.reason)
    else:
        raise SystemExit(f"不支持的复核类型：{args.kind}")
    print(f"已登记运营复核：{entry.review_id}")


def cmd_supply(args, repo: Repository) -> None:
    show_supply(repo, args.window)


def cmd_allocate(args, repo: Repository) -> None:
    supply = OpsService(repo).effective_supply(args.window)
    result = AllocationEngine(repo).allocate_window(args.window, supply, args.note)
    show_plan(result.plan)
    print(f"\n缺口订单：{result.shortfall_orders or '无'}；承诺受损订单：{result.held_orders or '无'}")


def cmd_compare(args, repo: Repository) -> None:
    plans = repo.list_plans(window=args.window)
    if len(plans) < 2:
        print("该窗口至少需要两个方案快照才能对比")
        return
    before, after = plans[-2], plans[-1]
    cmp_ = PlanningService(repo).compare(before, after)
    _print_header(f"方案对比 {before.plan_id} -> {after.plan_id}（{args.window}）")
    for d in cmp_.diffs:
        flag = "变更" if d.changed else "不变"
        print(f"[{flag}] {d.order_id} {d.product}: "
              f"{d.before_status}/{d.before_allocated}件 -> "
              f"{d.after_status}/{d.after_allocated}件 (Δ{d.delta:+d})")
        if d.changed:
            print(f"    依据版本: {d.basis_revision_after or '无'}；下一步: {d.next_step}")
    if cmp_.new_actions:
        _print_header("新增处理事项")
        for act in cmp_.new_actions:
            print(f"{act.action_id}  {act.order_id}  {act.kind}  {act.reason}")
    if cmp_.closed_gap_orders:
        print(f"\n缺口已闭合订单：{cmp_.closed_gap_orders}")


def cmd_audit(args, repo: Repository) -> None:
    _print_header("审计事件（最近优先）")
    for event in repo.list_audit(entity_type=args.entity_type or "",
                                 entity_id=args.entity_id or "", limit=args.limit):
        print(f"{event.ts}  {event.actor:10s}  {event.action:28s}  "
              f"{event.entity_type}:{event.entity_id}  {event.detail}")


def cmd_receipts(args, repo: Repository) -> None:
    _print_header("原始回执")
    for rcp in repo.list_receipts(args.supplier or ""):
        print(f"{rcp.receipt_id}  {rcp.supplier}  {rcp.media:12s}  {rcp.source_ref}")
        print(f"    sha256={rcp.content_hash}  收到于 {rcp.received_at}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capflow", description="供应商产能采集与订单分配")
    parser.add_argument("--db", default="data/capflow.db", help="SQLite 数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load-samples", help="载入样例订单与路由")
    p.add_argument("--samples", default="samples")
    p.set_defaults(func=cmd_load_samples)

    p = sub.add_parser("collect-export", help="通过授权导出接口采集")
    p.add_argument("--supplier", required=True)
    p.add_argument("--window", required=True)
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_collect_export)

    p = sub.add_parser("collect-rpa", help="通过可审计 RPA 网页提交采集")
    p.add_argument("--supplier", required=True)
    p.add_argument("--window", required=True)
    p.add_argument("--scenario", required=True, help="RPA 脚本 JSON")
    p.add_argument("--scenario-name", default="normal",
                   choices=["normal", "login", "structure", "ambiguous",
                            "ambiguous_then_confirmed"])
    p.set_defaults(func=cmd_collect_rpa)

    p = sub.add_parser("resume-rpa", help="从检查点恢复暂停的 RPA 流程")
    p.add_argument("--run-id", required=True)
    p.add_argument("--supplier", required=True)
    p.add_argument("--window", required=True)
    p.add_argument("--scenario", required=True)
    p.add_argument("--scenario-name", default="ambiguous_then_confirmed",
                   choices=["normal", "login", "structure", "ambiguous",
                            "ambiguous_then_confirmed"])
    p.add_argument("--fresh-browser", action="store_true",
                   help="模拟门户侧无提交记录（默认按已有提交恢复，仅执行查询）")
    p.set_defaults(func=cmd_resume_rpa)

    p = sub.add_parser("runs", help="查看采集运行与检查点")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("records", help="查看产能记录")
    p.add_argument("--window", required=True)
    p.set_defaults(func=cmd_records)

    p = sub.add_parser("confirm-all", help="运营批量确认已采集记录")
    p.add_argument("--window", required=True)
    p.add_argument("--actor", default="ops-lead")
    p.set_defaults(func=cmd_confirm_all)

    p = sub.add_parser("review", help="登记停机/班次/共享复核")
    p.add_argument("--kind", required=True,
                    choices=[k.value for k in (ReviewKind.DOWNTIME, ReviewKind.SHIFT,
                                               ReviewKind.SHARING)])
    p.add_argument("--resource", default="")
    p.add_argument("--window", default="")
    p.add_argument("--product", default="")
    p.add_argument("--quantity", type=int, default=0)
    p.add_argument("--reason", required=True)
    p.add_argument("--valid", action="store_true", help="班次/共享是否有效（默认无效）")
    p.add_argument("--actor", default="ops-lead")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("supply", help="查看窗口的有效供应")
    p.add_argument("--window", required=True)
    p.set_defaults(func=cmd_supply)

    p = sub.add_parser("allocate", help="执行窗口分配并保存方案快照")
    p.add_argument("--window", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_allocate)

    p = sub.add_parser("compare", help="比较窗口最近两个方案")
    p.add_argument("--window", required=True)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("audit", help="查看审计事件")
    p.add_argument("--entity-type", default="")
    p.add_argument("--entity-id", default="")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("receipts", help="查看原始回执与哈希")
    p.add_argument("--supplier", default="")
    p.set_defaults(func=cmd_receipts)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = Repository(args.db)
    try:
        args.func(args, repo)
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
