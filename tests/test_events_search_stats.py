"""Persistent activity log + retention, server-side library search,
collection stats for dashboards, and the read-only API key."""

from __future__ import annotations

import time

import pytest

import app.downloader as dl


def _model(db, design_id, title, creator="", collection_title=None, cid=None):
    db.insert_model(
        design_id=design_id,
        profile_id=None,
        title=title,
        slug="s",
        url="u",
        filename=f"{title.replace(' ', '_')}.3mf",
        file_path=f"/x/{design_id}.3mf",
        file_size=1,
        collection_id=cid,
        collection_title=collection_title,
        creator=creator,
    )


# ------------------------------------------------------------ event store
def test_events_db_roundtrip_and_kind_filter(db):
    db.add_event(1.0, "sync", "synced", None)
    db.add_event(2.0, "error", "boom", '{"design_id": 5}')
    events = db.recent_events(10)
    assert [e["message"] for e in events] == ["boom", "synced"]  # newest first
    assert events[0]["design_id"] == 5
    assert [e["message"] for e in db.recent_events(10, "sync")] == ["synced"]


def test_prune_events_by_age_and_count(db):
    now = time.time()
    db.add_event(now - 40 * 86400, "sync", "ancient", None)
    for i in range(10):
        db.add_event(now - 60 + i, "sync", f"e{i}", None)
    assert db.prune_events(30, 0, now) == 1
    assert db.prune_events(0, 4, now) == 6
    assert [e["message"] for e in db.recent_events(100)] == ["e9", "e8", "e7", "e6"]
    assert db.prune_events(0, 0, now) == 0  # both limits disabled


@pytest.mark.asyncio
async def test_add_event_persists_when_store_set(db):
    dl.set_event_store(db)
    try:
        await dl.add_event("download", "Downloaded X", design_id=7)
        assert db.recent_events(1)[0]["message"] == "Downloaded X"
        assert dl.recent_events(1, "download")[0]["design_id"] == 7
    finally:
        dl.set_event_store(None)


@pytest.mark.asyncio
async def test_scheduler_prunes_with_settings(db, monkeypatch):
    import app.scheduler as sched
    from app.scheduler import SyncScheduler

    monkeypatch.setattr(sched.settings, "event_retention_days", 30)
    monkeypatch.setattr(sched.settings, "event_max_rows", 2)
    for i in range(5):
        db.add_event(time.time(), "sync", f"e{i}", None)
    SyncScheduler(db, dl.DownloadManager(db))._prune_events()
    assert len(db.recent_events(100)) == 2


# ----------------------------------------------------------------- search
def test_search_matches_title_creator_filename_label(db):
    _model(db, 1, "Dice Tower", creator="Alice")
    _model(db, 2, "Display Stand", collection_title="Favorites", cid=9)
    _model(db, 3, "100% infill test")
    _model(db, 4, "a_b cube")
    _model(db, 5, "axb cube")  # an unescaped _ would match this too

    def titles(q, **kw):
        return sorted(m["title"] for m in db.list_models(q=q, **kw))

    assert titles("dice") == ["Dice Tower"]  # case-insensitive
    assert titles("alice") == ["Dice Tower"]  # creator
    assert titles("favorites") == ["Display Stand"]  # origin label
    assert titles("Display_Stand.3mf") == ["Display Stand"]  # filename
    assert titles("%") == ["100% infill test"]  # wildcard matched literally
    assert titles("a_b") == ["a_b cube"]
    assert titles("d", label="Favorites") == ["Display Stand"]  # AND with label
    assert db.count_models(q="d") == 2  # Dice Tower, Display Stand
    assert titles("   ") == titles(None)  # blank = no filter


def test_models_route_search(app_client):
    client, database, _ = app_client
    _model(database, 1, "Dice Tower")
    _model(database, 2, "Display Stand")
    r = client.get("/api/models", params={"q": "tower"}).json()
    assert r["total"] == 1
    assert r["models"][0]["title"] == "Dice Tower"


def test_events_route_kind(app_client):
    client, database, _ = app_client
    database.add_event(1.0, "error", "bad", None)
    database.add_event(2.0, "sync", "ok", None)
    import app.downloader as fresh_dl  # reloaded by the fixture

    fresh_dl.set_event_store(database)
    try:
        r = client.get("/api/events", params={"kind": "error"}).json()
    finally:
        fresh_dl.set_event_store(None)
    assert [e["message"] for e in r["events"]] == ["bad"]


# ---------------------------------------------------------- collection stats
@pytest.mark.asyncio
async def test_sync_records_total_and_present(db, monkeypatch):
    monkeypatch.setattr(dl.settings, "download_delay_seconds", 0)
    db.upsert_collection(10, "Cats", "u", 60)
    _model(db, 1, "Have it", cid=10)
    manager = dl.DownloadManager(db)

    class FakeClient:
        async def get_collection_info(self, collection_id):
            return {"title": "Cats"}

        async def list_collection_designs(self, collection_id, page_size=100):
            return [{"id": 1}, {"id": 2}, {"id": 3}]

        async def close(self):
            return None

    manager._client = lambda: FakeClient()

    async def fake_download(url, collection_id=None, subfolder=None):
        if url.endswith("/3"):
            raise dl.NotFoundError("gone")
        _model(db, 2, "New one", cid=10)
        return {"status": "downloaded"}

    manager.download_model = fake_download
    await manager.sync_collection(10)
    coll = db.get_collection(10)
    assert (coll["last_sync_total"], coll["last_sync_present"]) == (3, 2)
    # A failed sync (no listing) keeps the previous numbers.
    db.record_sync(10, "error", 0)
    coll = db.get_collection(10)
    assert (coll["last_sync_total"], coll["last_sync_present"]) == (3, 2)


def test_stats_routes(app_client):
    client, database, _ = app_client
    database.upsert_collection(10, "Cats", "u", 60)
    database.upsert_collection(11, "Never synced", "u", 60)
    database.record_sync(10, "ok", 4, total=10, present=7)
    for _ in range(3):
        database.record_failure(99, 10, "404")

    stats = {
        c["collection_id"]: c
        for c in client.get("/api/collections/stats").json()["collections"]
    }
    cats = stats[10]
    assert (cats["total"], cats["present"], cats["skipped"], cats["missing"]) == (
        10,
        7,
        1,
        2,
    )
    assert cats["syncing"] is False and cats["enabled"] is True
    assert cats["last_sync_status"] == "ok"
    assert stats[11]["missing"] is None  # no numbers before a first sync

    one = client.get("/api/collections/10/stats").json()
    assert one["title"] == "Cats" and one["missing"] == 2
    assert client.get("/api/collections/12345/stats").status_code == 404


# ---------------------------------------------------------- read-only key
def test_read_only_api_key(app_client, monkeypatch):
    import app.routes as routes

    client, database, _ = app_client
    database.upsert_collection(10, "Cats", "u", 60)
    monkeypatch.setattr(routes.settings, "api_key", "full-key")
    monkeypatch.setattr(routes.settings, "read_api_key", "read-key")

    def get(key):
        return client.get(
            "/api/collections/stats", headers={"X-API-Key": key} if key else {}
        )

    def patch(key):
        return client.patch(
            "/api/collections/10", json={"enabled": False}, headers={"X-API-Key": key}
        )

    assert get(None).status_code == 401
    assert get("wrong").status_code == 401
    assert get("read-key").status_code == 200
    assert get("full-key").status_code == 200
    assert patch("read-key").status_code == 403
    assert database.get_collection(10)["enabled"] == 1  # untouched
    assert patch("full-key").status_code == 200


def test_read_key_alone_does_not_lock_the_app(app_client, monkeypatch):
    """Without BND_API_KEY the app is open; a lone read key changes nothing."""
    import app.routes as routes

    client, _, _ = app_client
    monkeypatch.setattr(routes.settings, "api_key", None)
    monkeypatch.setattr(routes.settings, "read_api_key", "read-key")
    assert client.get("/api/collections/stats").status_code == 200
