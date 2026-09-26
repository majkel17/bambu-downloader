"""No-print-profile detection, sign-out behavior, and the configurable
own-collections refresh interval."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

# Classes via the module at call time — see test_skip_failed.py.
import app.downloader as dl
import app.scheduler as sched


class _NoProfileClient:
    """A design with no plate instances whose legacy endpoint refuses too."""

    async def get_design(self, design_id):
        return {"title": "STL only", "modelId": "abc"}

    async def get_design_instances(self, design_id):
        return {"hits": []}

    async def get_design_model_download(self, design_id):
        raise dl.MakerWorldError("HTTP 400")

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_design_without_instances_raises_no_profile(db):
    db.set_meta("bambu_token", "tok")
    m = dl.DownloadManager(db)
    m._client = lambda: _NoProfileClient()
    with pytest.raises(dl.NoProfileError):
        await m.download_model("https://makerworld.com/en/models/42")


class _ListingClient:
    async def get_collection_info(self, collection_id):
        return {"title": "Cats"}

    async def list_collection_designs(self, collection_id, page_size=100):
        return [{"id": 1}, {"id": 2}]

    async def close(self):
        return None


@pytest.fixture()
def manager(db, monkeypatch):
    monkeypatch.setattr(dl.settings, "download_delay_seconds", 0)
    monkeypatch.setattr(dl.settings, "max_download_attempts", 3)
    db.upsert_collection(10, "Cats", "u", 60)
    m = dl.DownloadManager(db)
    m._client = lambda: _ListingClient()
    return m


def _failing(calls, design_id, error):
    async def fake(url, collection_id=None, subfolder=None):
        calls.append(url)
        if url.endswith(f"/{design_id}"):
            raise error
        return {"status": "exists"}

    return fake


@pytest.mark.asyncio
async def test_no_profile_is_skipped_after_first_failure(manager, db):
    calls: list[str] = []
    manager.download_model = _failing(calls, 1, dl.NoProfileError("no profile"))
    await manager.sync_collection(10)
    assert db.skipped_design_ids(3) == {1}
    row = db.skipped_models(3)[0]
    assert (row["reason"], row["attempts"]) == ("no_profile", 1)
    assert "no print profile" in dl.recent_events(5)[1]["message"]

    calls.clear()
    summary = await manager.sync_collection(10)
    assert not any(u.endswith("/1") for u in calls)
    assert summary["skipped"] == 1


@pytest.mark.asyncio
async def test_other_reasons_still_need_all_attempts(manager, db):
    manager.download_model = _failing([], 1, dl.NotFoundError("gone"))
    await manager.sync_collection(10)
    assert db.skipped_design_ids(3) == set()
    assert db.skipped_models(1)[0]["reason"] == "not_found"


def test_zero_limit_disables_no_profile_skip_too(db):
    db.record_failure(1, 10, "no profile", "no_profile")
    assert db.skipped_design_ids(0) == set()
    assert db.skipped_models(0) == []


def test_legacy_rows_without_reason_count_by_attempts(db):
    for _ in range(3):
        db.record_failure(7, 10, "old error")  # reason NULL, pre-upgrade rows
    assert db.skipped_design_ids(3) == {7}


def test_stats_count_no_profile_as_skipped(db):
    db.upsert_collection(10, "Cats", "u", 60)
    db.record_failure(1, 10, "no profile", "no_profile")
    assert db.collection_stats(3)[0]["skipped"] == 1


# ------------------------------------------------------------ sign-out
def test_logout_clears_own_collections_but_keeps_library(app_client):
    client, database, _ = app_client
    database.set_meta("bambu_token", "tok")
    database.upsert_collection(10, "Cats", "u", 60)
    database.replace_remote_collections(
        [
            {
                "collection_id": 5,
                "title": "Mine",
                "slug": "m",
                "design_count": 1,
                "is_default": False,
                "design_ids": [1],
            }
        ]
    )
    database.insert_model(
        design_id=1,
        profile_id=None,
        title="T",
        slug="t",
        url="u",
        filename="t.3mf",
        file_path="/x/t.3mf",
        file_size=1,
    )
    assert client.post("/api/auth/logout").json() == {"ok": True}
    assert database.remote_collections() == []
    assert database.get_meta("bambu_token") is None
    assert database.get_collection(10) is not None
    assert database.count_models() == 1
    r = client.get("/api/my-collections").json()
    assert r["authenticated"] is False and r["collections"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("signed_in", [False, True])
async def test_scheduler_syncs_only_when_signed_in(db, monkeypatch, signed_in):
    if signed_in:
        db.set_meta("bambu_token", "tok")
    db.upsert_collection(10, "Cats", "u", 60)  # never synced -> due
    manager = dl.DownloadManager(db)
    synced: list[int] = []

    async def fake_sync(cid):
        synced.append(cid)
        return {}

    manager.sync_collection = fake_sync
    s = sched.SyncScheduler(db, manager)

    async def no_refresh():
        return None

    s._refresh_my_collections = no_refresh
    s.start()
    await asyncio.sleep(0.1)  # one loop iteration, then it sleeps
    await s.stop()
    assert synced == ([10] if signed_in else [])


# --------------------------------------------------- refresh interval
def test_mine_refresh_minutes_setting(db, monkeypatch):
    monkeypatch.setattr(sched.settings, "my_collections_refresh_minutes", 60)
    s = sched.SyncScheduler(db, dl.DownloadManager(db))
    assert s.mine_refresh_minutes() == 60  # env default
    db.set_meta("my_collections_refresh_minutes", "120")
    assert s.mine_refresh_minutes() == 120  # UI setting wins
    db.set_meta("my_collections_refresh_minutes", "5")
    assert s.mine_refresh_minutes() == 15  # floor


def test_reschedule_keeps_fresh_cache_from_refetching(db):
    s = sched.SyncScheduler(db, dl.DownloadManager(db))
    s.reschedule_mine_refresh()
    assert s._next_mine_refresh == 0.0  # never fetched -> due now
    fetched = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    db.replace_remote_collections(
        [
            {
                "collection_id": 5,
                "title": "M",
                "slug": "m",
                "design_count": 0,
                "is_default": False,
                "design_ids": [],
            }
        ]
    )
    with db.connect() as conn:
        conn.execute("UPDATE remote_collections SET fetched_at = ?", (fetched,))
    s.reschedule_mine_refresh()
    import time

    remaining = s._next_mine_refresh - time.monotonic()
    assert 49 * 60 < remaining <= 50 * 60  # 60 min interval, 10 min old


def test_my_collections_settings_route(app_client):
    client, database, _ = app_client
    assert client.get("/api/my-collections").json()["refresh_minutes"] >= 15
    r = client.put("/api/my-collections/settings", json={"refresh_minutes": 90})
    assert r.json() == {"refresh_minutes": 90}
    assert database.get_meta("my_collections_refresh_minutes") == "90"
    assert client.get("/api/my-collections").json()["refresh_minutes"] == 90
    for bad in (5, 20000):
        r = client.put("/api/my-collections/settings", json={"refresh_minutes": bad})
        assert r.status_code == 400
