from pathlib import Path

import preprocess_notices as cli
from notice_preprocessing import MonthFailure, PreprocessResult
from processed_notice_store import MonthKey


def test_cli_defaults_to_durable_roots(monkeypatch):
    captured = {}

    def run(raw_root, processed_root, **_kwargs):
        captured.update(raw=raw_root, processed=processed_root)
        return PreprocessResult((), (), ())

    monkeypatch.setattr(cli, "preprocess_notices", run)

    assert cli.main([]) == 0
    assert captured == {
        "raw": Path("data/notices"),
        "processed": Path("data/processed/notices"),
    }


def test_cli_returns_nonzero_when_a_month_fails(monkeypatch):
    failure = MonthFailure(MonthKey(2025, 1), "network down")
    monkeypatch.setattr(
        cli,
        "preprocess_notices",
        lambda *_args, **_kwargs: PreprocessResult((), (), (failure,)),
    )

    assert cli.main(["--start", "2025-01", "--end", "2025-01"]) == 1
