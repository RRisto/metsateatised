import asyncio
from unittest.mock import Mock

import app


def test_detail_batch_reports_completion(monkeypatch):
    async def fake_fetch_detail(_session, stand_id):
        return {"_stand_id": stand_id}

    monkeypatch.setattr(app, "_fetch_detail_one", fake_fetch_detail)
    progress = Mock()

    rows = asyncio.run(
        app._fetch_detail_batch(
            [11, 12, 13],
            n_workers=2,
            progress_callback=progress,
        )
    )

    assert {row["_stand_id"] for row in rows} == {11, 12, 13}
    progress.assert_any_call(3, 3)
