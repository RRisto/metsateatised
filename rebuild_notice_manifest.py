"""Rebuild the durable notice manifest without rewriting Parquet partitions."""

from pathlib import Path

from notice_store import rebuild_manifest
from notice_sync import IDENTITY_CANDIDATES


def main() -> None:
    manifest = rebuild_manifest(
        Path("data/notices"),
        identity_candidates=IDENTITY_CANDIDATES,
    )
    records = sum(
        entry["record_count"] for entry in manifest["partitions"].values()
    )
    print(f"Rebuilt {len(manifest['partitions'])} partitions with {records} records")


if __name__ == "__main__":
    main()
