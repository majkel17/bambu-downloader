"""Skip models after BND_MAX_DOWNLOAD_ATTEMPTS model-caused sync failures."""

from __future__ import annotations

import pytest

# Exception classes are taken from app.downloader's namespace at call time:
# the app_client fixture importlib.reload()s the modules (downloader before
# makerworld), so only the downloader's own names are guaranteed to match
# its except clauses.
import app.downloader as dl


class _ListingClient:
    """Collection listing with designs 1 and 2."""

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


def _fake_download(calls, errors):
    """download_model stand-in: raises errors[design_id] if set."""

    async def fake(url, collection_id=None, subfolder=None):
        design_id = int(url.rsplit("/", 1)[-1].split("#")[0])
        calls.append(design_id)
        if design_id in errors:
            raise errors[design_id]
        return {"status": "downloaded"}

    return fake


def test_failure_counter_db(db):
    assert db.record_failure(5, 10, "404") == 1
    assert db.record_failure(5, None, "404 again") == 2
    assert db.skipped_design_ids(3) == set()
    db.record_failure(5, None, "x")
    assert db.skipped_design_ids(3) == {5}
    assert db.skipped_design_ids(0) == set()  # 0 = never skip
    rows = db.skipped_models(3)
    assert rows[0]["collection_id"] == 10  # kept despite later None
    assert rows[0]["last_error"] == "x"
    assert db.clear_failure(5) is True
    assert db.clear_failure(5) is False


@pytest.mark.asyncio
async def test_sync_skips_model_after_max_failures(manager, db):
    calls: list[int] = []
    manager.download_model = _fake_download(
        calls, {1: dl.NotFoundError("MakerWorld resource not found")}
    )
    for _ in range(3):
        await manager.sync_collection(10)
    assert calls.count(1) == 3
    assert any("skipped from now on" in e["message"] for e in dl.recent_events(20))

    calls.clear()
    summary = await manager.sync_collection(10)
    assert 1 not in calls
    assert summary["skipped"] == 1
    assert "1 skipped" in dl.recent_events(1)[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cls", "msg"),
    [("CaptchaError", "418"), ("MakerWorldError", "Could not reach: timeout")],
)
async def test_transient_failures_do_not_count(manager, db, cls, msg):
    calls: list[int] = []
    manager.download_model = _fake_download(calls, {1: getattr(dl, cls)(msg)})
    for _ in range(4):
        await manager.sync_collection(10)
    assert db.skipped_design_ids(3) == set()
    assert db.skipped_models(1) == []


@pytest.mark.asyncio
async def test_no_download_url_counts(manager, db):
    manager.download_model = _fake_download(
        [], {1: dl.ModelUnavailableError("no download URL")}
    )
    for _ in range(3):
        await manager.sync_collection(10)
    assert db.skipped_design_ids(3) == {1}


@pytest.mark.asyncio
async def test_zero_limit_never_skips(manager, db, monkeypatch):
    monkeypatch.setattr(dl.settings, "max_download_attempts", 0)
    calls: list[int] = []
    manager.download_model = _fake_download(calls, {1: dl.NotFoundError("gone")})
    for _ in range(5):
        await manager.sync_collection(10)
    assert calls.count(1) == 5


@pytest.mark.asyncio
async def test_successful_download_clears_failures(db, tmp_path, monkeypatch):
    from tests.test_files_dedup import _DownloadFakeClient

    monkeypatch.setattr(dl.settings, "download_dir", str(tmp_path / "dl"))
    db.set_meta("bambu_token", "tok")
    db.record_failure(42, None, "earlier 404")
    m = dl.DownloadManager(db)
    m._client = lambda: _DownloadFakeClient()
    result = await m.download_model("https://makerworld.com/en/models/42")
    assert result["status"] == "downloaded"
    assert db.clear_failure(42) is False  # already gone


def test_skipped_models_routes(app_client):
    client, database, _ = app_client
    database.upsert_collection(10, "Cats", "u", 60)
    for _ in range(3):
        database.record_failure(7, 10, "MakerWorld resource not found")

    r = client.get("/api/skipped-models").json()
    assert r["max_attempts"] == 3
    assert r["models"][0]["design_id"] == 7
    assert r["models"][0]["collection_title"] == "Cats"

    assert client.post("/api/skipped-models/7/retry").json() == {"ok": True}
    assert client.get("/api/skipped-models").json()["models"] == []
    assert client.post("/api/skipped-models/7/retry").status_code == 404
