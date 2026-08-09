from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile

DEFAULT_CACHE_ROOT = Path("data/cache")


def _cache_path(namespace: str, key: str, cache_root: Path) -> Path:
    safe_key = "".join(
        character if character.isalnum() or character in "-_" else "_" for character in key
    )
    return cache_root / namespace / f"{safe_key}.json"


def write_json_cache(
    namespace: str,
    key: str,
    payload: object,
    *,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    now: datetime | None = None,
) -> None:
    cached_at = now or datetime.now(UTC)
    path = _cache_path(namespace, key, cache_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"cached_at": cached_at.isoformat(), "payload": payload}
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.stem}-",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(document, temporary, ensure_ascii=False)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def read_json_cache(
    namespace: str,
    key: str,
    *,
    max_age: timedelta,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    now: datetime | None = None,
) -> object | None:
    path = _cache_path(namespace, key, cache_root)
    if not path.exists():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    cached_at = datetime.fromisoformat(document["cached_at"])
    current_time = now or datetime.now(UTC)
    if current_time - cached_at > max_age:
        return None
    return document["payload"]


def clear_data_cache(*, cache_root: Path = DEFAULT_CACHE_ROOT) -> None:
    if cache_root.exists():
        shutil.rmtree(cache_root)
