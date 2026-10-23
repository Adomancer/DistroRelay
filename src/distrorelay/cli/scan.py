"""`scan` dan `plan`: hanya membaca."""

from __future__ import annotations

import argparse
from pathlib import Path


def scan_with_status(console, flow=None):
    from ..windows.scanner import run_scan

    with console.status("Scanning…") as status:
        if flow is not None:
            report = flow.scan(progress=lambda message: status.update(message))
        else:
            report = run_scan(progress=lambda message: status.update(message))
    for issue in report.issues:
        if issue.severity != "info":
            console.print(f"Scan: [{issue.step}] {issue.message}", style="yellow", markup=False, highlight=False)
    return report


def cmd_scan(args: argparse.Namespace, log_path: Path | None) -> int:
    from rich.console import Console

    from .. import render
    from ..windows.scanner import report_to_json, run_scan, save_raw

    console = Console()
    with console.status("Scanning…") as status:
        report = run_scan(progress=lambda message: status.update(message))
    console.print(render.to_rich_tree("DistroRelay: hasil scan (read-only)", render.summary_nodes(report)))
    console.print(render.info_text(report, str(log_path) if log_path else None))
    if args.json:
        json_path = Path(args.json)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(report_to_json(report), encoding="utf-8")
        console.print(f"Laporan JSON disimpan ke {json_path.resolve()}")
    if args.raw_dir:
        written = save_raw(report, Path(args.raw_dir))
        console.print(f"{len(written)} file output mentah disimpan ke {Path(args.raw_dir).resolve()}")
    return 0 if all(i.severity != "error" for i in report.issues) else 1


def cmd_plan(args: argparse.Namespace) -> int:
    from rich.console import Console

    from .. import render
    from ..windows.uninstall_plan import build_plan, expand_selection

    console = Console()
    report = scan_with_status(console)
    if report.analysis is None:
        console.print("Analisis tidak tersedia.")
        return 1
    if not args.select:
        console.print(render.candidates_text(report.analysis))
        return 0
    refs, unknown = expand_selection(report.analysis, args.select)
    for selector in unknown:
        console.print(f"Tidak dikenal: {selector}", style="red", markup=False)
    plan = build_plan(report.analysis, report.boot_entries, refs, inventory=report.inventory, fsinfo=report.fsinfo,
                      extend_c=args.extend_c)
    console.print(render.plan_text(plan))
    return 1 if unknown else 0
