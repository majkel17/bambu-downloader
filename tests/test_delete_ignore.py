"""Deleting library models, and syncs leaving deleted ("ignored") profiles alone."""

from __future__ import annotations

import pytest

# Classes via the module at call time — see test_skip_failed.py.
import app.downloader as dl
from tests.test_design_profiles import AUTHOR, INSTANCES, _ProfilesClient


def _model(db, root, pid, name, cid=7, ptitle=None):
    folder = root / "7-haloween" / "3230452-mini-ghost"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(b"x")
    (folder / "cover.webp").write_bytes(b"c")
    return db.insert_model(
        design_id=3230452,
        profile_id=pid,
        title="Mini Ghost",
        slug="g",
        url="u",
        filename=name,
        file_path=str(folder / name),
        file_size=1,
        collection_id=cid,
        collection_title="haloween",
        creator="Maxx Design",
        profile_title=ptitle,
    )


@pytest.fixture()
def root(tmp_path, monkeypatch):
    r = tmp_path / "dl"
    r.mkdir()
    monkeypatch.setattr(dl.settings, "download_dir", str(r))
    monkeypatch.setattr(dl.settings, "download_delay_seconds", 0)
    return r


def test_delete_keeps_shared_folder_until_last_profile(db, root):
    a = _model(db, root, 963510835, "a.3mf", ptitle="No AMS")
    b = _model(db, root, 968473582, "b.3mf")
    m = dl.DownloadManager(db)
    folder = root / "7-haloween" / "3230452-mini-ghost"

    row = m.delete_model(a, ignore=True)
    assert row["profile_title"] == "No AMS"
    assert not (folder / "a.3mf").exists()
    assert (folder / "cover.webp").exists()  # b still lives here
    assert db.ignored_map() == {3230452: {963510835}}
    assert db.ignored_profiles()[0]["collection_title"] is None  # not followed

    m.delete_model(b, ignore=False)
    assert not folder.exists() and not (root / "7-haloween").exists()
    assert root.exists()
    assert db.design_rows(3230452) == []
    assert db.ignored_map() == {3230452: {963510835}}
    assert sorted(db.pv_removals(10)) == sorted(
        [str(folder / "a.3mf"), str(folder / "b.3mf")]
    )
    with pytest.raises(KeyError):
        m.delete_model(a, ignore=False)


def test_delete_never_touches_files_outside_the_root(db, root, tmp_path):
    outside = tmp_path / "elsewhere" / "x.3mf"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    rid = db.insert_model(
        design_id=1, profile_id=None, title="X", slug="x", url="u",
        filename="x.3mf", file_path=str(outside), file_size=1,
    )  # fmt: skip
    dl.DownloadManager(db).delete_model(rid, ignore=True)
    assert outside.exists()
    assert db.get_model(rid) is None
    assert db.ignored_map() == {1: {-1}}


class _SyncClient(_ProfilesClient):
    async def get_collection_info(self, collection_id):
        return {"title": "haloween"}

    async def list_collection_designs(self, collection_id, page_size=100):
        return [{"id": 3230452, "designCreator": AUTHOR}]


@pytest.fixture()
def syncing(db, root):
    db.set_meta("bambu_token", "tok")
    db.upsert_collection(7, "haloween", "u", 60)
    _ProfilesClient.downloads = []
    m = dl.DownloadManager(db)
    m._client = lambda: _SyncClient()
    return m


@pytest.mark.asyncio
async def test_default_mode_does_not_bring_back_a_deleted_default(db, root, syncing):
    rid = _model(db, root, 963510835, "a.3mf")
    syncing.delete_model(rid, ignore=True)
    await syncing.sync_collection(7)
    assert _ProfilesClient.downloads == []


@pytest.mark.asyncio
async def test_default_mode_redownloads_when_not_ignored(db, root, syncing):
    rid = _model(db, root, 963510835, "a.3mf")
    syncing.delete_model(rid, ignore=False)
    await syncing.sync_collection(7)
    assert _ProfilesClient.downloads == [963510835]


@pytest.mark.asyncio
async def test_all_mode_skips_only_the_ignored_profiles(db, root, syncing):
    db.set_collection_plates_mode(7, "all")
    keep = _model(db, root, 963510835, "a.3mf")
    gone = _model(db, root, 1009777739, "petg.3mf")
    syncing.delete_model(gone, ignore=True)
    syncing.delete_model(keep, ignore=True)  # even with nothing left on disk
    await syncing.sync_collection(7)
    expected = {i["profileId"] for i in INSTANCES} - {963510835, 1009777739}
    assert set(_ProfilesClient.downloads) == expected


@pytest.mark.asyncio
async def test_manual_download_clears_the_ignore(db, root, syncing):
    rid = _model(db, root, 963510835, "a.3mf")
    syncing.delete_model(rid, ignore=True)
    r = await syncing.download_model(
        "https://makerworld.com/en/models/3230452#profileId-963510835"
    )
    assert r["status"] == "downloaded"
    assert db.ignored_map() == {}


def test_stats_count_ignored_designs_as_not_missing(db, root):
    db.upsert_collection(7, "haloween", "u", 60)
    db.record_sync(7, "ok", 0, total=3, present=1)
    rid = _model(db, root, 963510835, "a.3mf")
    dl.DownloadManager(db).delete_model(rid, ignore=True)
    assert db.collection_stats(3)[0]["ignored"] == 1


def test_delete_and_restore_routes(app_client, tmp_path, monkeypatch):
    import app.routes as routes

    client, database, _ = app_client
    monkeypatch.setattr(routes.settings, "download_dir", str(tmp_path))
    rid = _model(database, tmp_path, 5, "a.3mf")
    assert client.delete(f"/api/models/{rid}?ignore=true").json() == {
        "ok": True,
        "ignored": True,
    }
    assert client.delete(f"/api/models/{rid}").status_code == 404
    ignored = client.get("/api/ignored-models").json()["models"]
    assert [(m["design_id"], m["profile_id"]) for m in ignored] == [(3230452, 5)]
    assert client.delete(f"/api/ignored-models/{ignored[0]['id']}").json() == {
        "ok": True
    }
    assert client.get("/api/ignored-models").json()["models"] == []
    assert client.delete(f"/api/ignored-models/{ignored[0]['id']}").status_code == 404
