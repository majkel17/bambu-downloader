"""Cloudflare challenge detection, the incremental background refresh of
own collections, and <id>-<title> collection folders that follow renames."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

# Classes via the module at call time — see test_skip_failed.py. The
# client raises app.makerworld's names, the manager app.downloader's.
import app.downloader as dl
import app.makerworld as mw
from tests.test_makerworld import _client_with_transport


# ------------------------------------------------------------- Cloudflare
@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "tok"])
async def test_cloudflare_challenge_is_a_captcha(token):
    """A 403 "Just a moment..." page is Cloudflare, not MakerWorld: it must
    not read as access denied / private collection (verified 2026-09-27)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"cf-mitigated": "challenge", "content-type": "text/html"},
            text="<!DOCTYPE html><title>Just a moment...</title>",
        )

    client = _client_with_transport(handler)
    client.auth_token = token
    try:
        with pytest.raises(mw.CaptchaError):
            await client.get_collection_info(1)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_plain_403_is_still_forbidden():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "no access"})

    client = _client_with_transport(handler)
    try:
        with pytest.raises(mw.ForbiddenError):
            await client.get_collection_info(1)
    finally:
        await client.close()


# ------------------------------------------------------ incremental listing
def _listing_handler(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/my/favorites/listlite"):
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {"id": 1, "title": "Same", "designCnt": 2},
                        {"id": 2, "title": "Grew", "designCnt": 2},
                        {"id": 3, "title": "New", "designCnt": 1},
                    ]
                },
            )
        cid = path.split("/favorites/")[1].split("/")[0]
        hits = {"1": [{"id": 90}, {"id": 91}], "2": [{"id": 20}, {"id": 21}]}
        return httpx.Response(
            200, json={"total": 2, "hits": hits.get(cid, [{"id": 30}])}
        )

    return handler


@pytest.mark.asyncio
async def test_listing_reuses_unchanged_collections(monkeypatch):
    monkeypatch.setattr(dl.settings, "download_delay_seconds", 0)
    calls: list[str] = []
    progress: list[tuple[int, int]] = []
    client = _client_with_transport(_listing_handler(calls))
    client.auth_token = "tok"
    previous = {
        1: {"design_count": 2, "design_ids": [10, 11], "slug": "same"},
        2: {"design_count": 1, "design_ids": [20], "slug": ""},
    }
    try:
        mine = await client.list_my_collections(
            previous=previous, progress=lambda d, t: progress.append((d, t))
        )
    finally:
        await client.close()
    by_id = {c["collection_id"]: c for c in mine}
    assert by_id[1]["design_ids"] == [10, 11]  # cached, not re-paged
    assert by_id[1]["slug"] == "same"  # carried over
    assert by_id[2]["design_ids"] == [20, 21]  # count changed -> re-paged
    assert by_id[3]["design_ids"] == [30]  # new collection
    assert not any("/favorites/1/" in p for p in calls)
    assert not any(p.endswith("/withoutdesign") for p in calls)
    assert progress == [(1, 3), (2, 3), (3, 3)]


class _RecordingClient:
    def __init__(self, seen):
        self.seen = seen

    async def list_my_collections(self, previous=None, progress=None):
        self.seen.append(previous)
        if progress:
            progress(1, 1)
        return [
            {
                "collection_id": 1,
                "title": "A",
                "slug": "",
                "design_count": 1,
                "is_default": False,
                "design_ids": [5],
            }
        ]

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_manager_full_refresh_daily_or_forced(db):
    db.set_meta("bambu_token", "tok")
    seen: list = []
    m = dl.DownloadManager(db)
    m._client = lambda: _RecordingClient(seen)
    await m.refresh_my_collections()  # never full before -> full
    await m.refresh_my_collections()  # incremental
    await m.refresh_my_collections(full=True)
    assert seen[0] is None
    assert seen[1] == {1: {"design_count": 1, "design_ids": [5], "slug": ""}}
    assert seen[2] is None
    db.set_meta("my_collections_full_at", "0")  # a day+ ago
    await m.refresh_my_collections()
    assert seen[3] is None


@pytest.mark.asyncio
async def test_background_refresh_runs_once_and_reports_errors(db):
    db.set_meta("bambu_token", "tok")
    m = dl.DownloadManager(db)
    gate = asyncio.Event()

    class SlowFailing:
        async def list_my_collections(self, previous=None, progress=None):
            progress(1, 4)
            await gate.wait()
            raise dl.MakerWorldError("boom")

        async def close(self):
            return None

    m._client = lambda: SlowFailing()
    assert m.start_mine_refresh() is True
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert m.mine_refreshing
    assert m.start_mine_refresh() is False  # one at a time
    assert m.mine_progress == {"done": 1, "total": 4}
    gate.set()
    await m._mine_task
    assert not m.mine_refreshing
    assert m.mine_error == "boom"
    assert m.mine_progress is None


# ---------------------------------------------------- collection folders
class _SyncClient:
    def __init__(self, title):
        self.title = title

    async def get_collection_info(self, collection_id):
        return {"title": self.title}

    async def list_collection_designs(self, collection_id, page_size=100):
        return []

    async def close(self):
        return None


def _model_on_disk(db, root: Path, rel: str, design_id, cid, label, row_title="M"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    (path.parent / "cover.webp").write_bytes(b"c")
    db.insert_model(
        design_id=design_id,
        profile_id=None,
        title=row_title,
        slug="m",
        url="u",
        filename=path.name,
        file_path=str(path),
        file_size=1,
        collection_id=cid,
        collection_title=label,
    )
    return path


@pytest.fixture()
def dl_root(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    root.mkdir()
    monkeypatch.setattr(dl.settings, "download_dir", str(root))
    monkeypatch.setattr(dl.settings, "download_delay_seconds", 0)
    return root


def _paths(db):
    return sorted(r["file_path"] for r in db.model_files())


@pytest.mark.asyncio
async def test_legacy_folder_moves_under_id_prefix(db, dl_root):
    db.upsert_collection(10, "Cats", "u", 60)
    _model_on_disk(db, dl_root, "cats/1-foo/foo.3mf", 1, 10, "Cats")
    _model_on_disk(db, dl_root, "cats/2-bar/bar.3mf", 2, 10, "Cats")
    m = dl.DownloadManager(db)
    m._client = lambda: _SyncClient("Cats")
    await m.sync_collection(10)
    assert _paths(db) == [
        str(dl_root / "10-cats/1-foo/foo.3mf"),
        str(dl_root / "10-cats/2-bar/bar.3mf"),
    ]
    assert (dl_root / "10-cats/1-foo/cover.webp").exists()
    assert not (dl_root / "cats").exists()  # emptied and removed
    assert any("moved 2 model folder(s)" in e["message"] for e in dl.recent_events(5))
    # Nothing left to move: a second sync is quiet.
    await m.sync_collection(10)
    assert not any("moved" in e["message"] for e in dl.recent_events(1))


@pytest.mark.asyncio
async def test_rename_moves_folder_and_relabels(db, dl_root):
    db.upsert_collection(10, "Cats", "u", 60)
    _model_on_disk(db, dl_root, "10-cats/1-foo/foo.3mf", 1, 10, "Cats")
    m = dl.DownloadManager(db)
    m._client = lambda: _SyncClient("Kitties")
    await m.sync_collection(10)
    assert _paths(db) == [str(dl_root / "10-kitties/1-foo/foo.3mf")]
    assert (dl_root / "10-kitties/1-foo/foo.3mf").read_bytes() == b"x"
    assert db.list_models()[0]["collection_title"] == "Kitties"
    assert db.get_collection(10)["title"] == "Kitties"
    messages = [e["message"] for e in dl.recent_events(5)]
    assert any("renamed on MakerWorld: Cats → Kitties" in m for m in messages)


@pytest.mark.asyncio
async def test_existing_target_is_left_alone(db, dl_root):
    db.upsert_collection(10, "Cats", "u", 60)
    old = _model_on_disk(db, dl_root, "cats/1-foo/foo.3mf", 1, 10, "Cats")
    (dl_root / "10-cats/1-foo").mkdir(parents=True)
    (dl_root / "10-cats/1-foo/other.3mf").write_bytes(b"keep")
    m = dl.DownloadManager(db)
    m._client = lambda: _SyncClient("Cats")
    await m.sync_collection(10)
    assert old.exists() and _paths(db) == [str(old)]  # untouched
    assert (dl_root / "10-cats/1-foo/other.3mf").read_bytes() == b"keep"
    assert any("already exists" in e["message"] for e in dl.recent_events(5))


@pytest.mark.asyncio
async def test_only_this_collection_and_root_are_touched(db, dl_root, tmp_path):
    db.upsert_collection(10, "Cats", "u", 60)
    other = _model_on_disk(db, dl_root, "dogs/3-x/x.3mf", 3, 11, "Dogs")
    manual = _model_on_disk(db, dl_root, "4-y/y.3mf", 4, None, None)
    outside = _model_on_disk(db, tmp_path, "elsewhere/cats/5-z/z.3mf", 5, 10, "Cats")
    m = dl.DownloadManager(db)
    m._client = lambda: _SyncClient("Cats")
    await m.sync_collection(10)
    assert other.exists() and manual.exists() and outside.exists()
    assert sorted(_paths(db)) == sorted([str(other), str(manual), str(outside)])


def test_interrupted_move_is_repaired(db, dl_root):
    """Rename done, DB write lost (crash): the next run just fixes the rows."""
    db.upsert_collection(10, "Cats", "u", 60)
    _model_on_disk(db, dl_root, "cats/1-foo/foo.3mf", 1, 10, "Cats")
    (dl_root / "10-cats").mkdir()
    (dl_root / "cats/1-foo").rename(dl_root / "10-cats/1-foo")
    m = dl.DownloadManager(db)
    moved, conflicts = m._relocate_collection(10, "10-cats")
    assert (moved, conflicts) == (1, [])
    assert _paths(db) == [str(dl_root / "10-cats/1-foo/foo.3mf")]


def test_failed_db_write_rolls_the_rename_back(db, dl_root, monkeypatch):
    db.upsert_collection(10, "Cats", "u", 60)
    old = _model_on_disk(db, dl_root, "cats/1-foo/foo.3mf", 1, 10, "Cats")

    def boom(updates):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(db, "set_model_paths", boom)
    m = dl.DownloadManager(db)
    with pytest.raises(RuntimeError):
        m._relocate_collection(10, "10-cats")
    assert old.exists()
    assert not (dl_root / "10-cats/1-foo").exists()
