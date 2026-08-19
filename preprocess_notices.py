"""Command-line entry point for resumable notice preprocessing."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from notice_analysis import ANALYSIS_MODEL_VERSION
from notice_preprocessing import PreprocessProgress, preprocess_notices
from processed_notice_store import MonthKey


def _month(value: str) -> MonthKey:
    try:
        year_text, month_text = value.split("-", 1)
        return MonthKey(int(year_text), int(month_text))
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("month must use YYYY-MM format") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Preprocess downloaded forest notices month by month."
    )
    parser.add_argument("--raw-root", type=Path, default=Path("data/notices"))
    parser.add_argument(
        "--processed-root", type=Path, default=Path("data/processed/notices")
    )
    parser.add_argument("--start", type=_month)
    parser.add_argument("--end", type=_month)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--detail-workers", type=int, default=12)
    return parser


def _progress(event: PreprocessProgress) -> None:
    count = (
        f" {event.completed}/{event.total}"
        if event.completed is not None and event.total is not None
        else ""
    )
    print(f"{event.month.year:04d}-{event.month.month:02d} {event.stage}{count}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.detail_workers < 1:
        build_parser().error("--detail-workers must be at least 1")
    if args.start is not None and args.end is not None and args.start > args.end:
        build_parser().error("--start must not be after --end")

    result = preprocess_notices(
        args.raw_root,
        args.processed_root,
        model_version=ANALYSIS_MODEL_VERSION,
        start=args.start,
        end=args.end,
        force=args.force,
        detail_workers=args.detail_workers,
        progress=_progress,
    )
    print(
        f"completed={len(result.completed)} skipped={len(result.skipped)} "
        f"failed={len(result.failed)}"
    )
    for failure in result.failed:
        print(
            f"FAILED {failure.month.year:04d}-{failure.month.month:02d}: "
            f"{failure.message}"
        )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
