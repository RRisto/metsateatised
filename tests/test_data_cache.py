from datetime import UTC, datetime, timedelta

from data_cache import clear_data_cache, read_json_cache, write_json_cache


def test_json_cache_round_trip_and_clear(tmp_path):
    written_at = datetime(2026, 8, 8, 12, tzinfo=UTC)
    payload = {"features": [{"id": 12148722}]}

    write_json_cache("wfs", "stand-2", payload, cache_root=tmp_path, now=written_at)

    assert (
        read_json_cache(
            "wfs",
            "stand-2",
            max_age=timedelta(hours=24),
            cache_root=tmp_path,
            now=written_at + timedelta(hours=1),
        )
        == payload
    )
    clear_data_cache(cache_root=tmp_path)
    assert not any(tmp_path.rglob("*.json"))


def test_json_cache_ignores_expired_entry(tmp_path):
    written_at = datetime(2026, 8, 7, 12, tzinfo=UTC)
    write_json_cache("details", "12148722", {"id": 1}, cache_root=tmp_path, now=written_at)

    cached = read_json_cache(
        "details",
        "12148722",
        max_age=timedelta(hours=24),
        cache_root=tmp_path,
        now=written_at + timedelta(days=2),
    )

    assert cached is None
