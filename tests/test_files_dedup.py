"""File naming (extension detection + migration), concurrent-download dedup,
and the model file download endpoint."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest

import app.downloader as dl
from app.downloader import (
    DownloadManager,
    _detect_extension,
    _unique_path,
    _with_extension,
)


def _write_3mf(path: Path) -> Path:
    """A minimal 3MF-shaped OPC package (enough for detection)."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("3D/3dmodel.model", "<model/>")
    return path


# ------------------------------------------------------- extension detection
def test_detect_extension(tmp_path):
    assert _detect_extension(_write_3mf(tmp_path / "a")) == ".3mf"
    with zipfile.ZipFile(tmp_path / "b", "w") as zf:
        zf.writestr("readme.txt", "hi")
    assert _detect_extension(tmp_path / "b") == ".zip"
    (tmp_path / "c").write_bytes(b"solid cube\nfacet normal 0 0 1\n")
    assert _detect_extension(tmp_path / "c") == ".stl"
    (tmp_path / "d").write_bytes(b"ISO-10303-21;\nHEADER;")
    assert _detect_extension(tmp_path / "d") == ".step"
    (tmp_path / "e").write_bytes(b"\x00\x01binary")
    assert _detect_extension(tmp_path / "e") == ""
    (tmp_path / "f").write_bytes(b"PK\x03\x04 truncated")
    assert _detect_extension(tmp_path / "f") == ""
    assert _detect_extension(tmp_path / "missing") == ""


def test_with_extension():
    assert _with_extension("Plate_1", ".3mf") == "Plate_1.3mf"
    assert _with_extension("Plate_1.3MF", ".3mf") == "Plate_1.3MF"
    assert _with_extension("Stand_v1.2", ".3mf") == "Stand_v1.2.3mf"
    assert _with_extension("Plate_1", "") == "Plate_1"
    assert len(_with_extension("x" * 150, ".3mf")) == 150


def test_unique_path(tmp_path):
    p = tmp_path / "a.3mf"
    assert _unique_path(p, 5) == p
    p.write_bytes(b"x")
    assert _unique_path(p, 5) == tmp_path / "a-5.3mf"


# ------------------------------------------------------------- migration
def test_fix_file_extensions(db, tmp_path):
    manager = DownloadManager(db)
    bare = _write_3mf(tmp_path / "Plate_1")
    ok = _write_3mf(tmp_path / "Other.3mf")
    for design_id, path in ((1, bare), (2, ok), (3, tmp_path / "gone")):
        db.insert_model(
            design_id=design_id,
            profile_id=None,
            title="T",
            slug="t",
            url="u",
            filename=path.name,
            file_path=str(path),
            file_size=1,
        )

    assert manager.fix_file_extensions() == 1
    assert not bare.exists()
    assert (tmp_path / "Plate_1.3mf").is_file()
    assert ok.is_file()
    rows = {r["filename"]: r for r in db.model_files()}
    assert rows["Plate_1.3mf"]["file_path"] == str(tmp_path / "Plate_1.3mf")
    assert db.get_meta("file_ext_migrated") == "1"
    # Flag set: a second boot doesn't rescan.
    _write_3mf(tmp_path / "Late")
    assert manager.fix_file_extensions() == 0


def test_fix_file_extensions_never_clobbers(db, tmp_path):
    manager = DownloadManager(db)
    _write_3mf(tmp_path / "Plate_1.3mf")  # unrelated file already has the name
    bare = _write_3mf(tmp_path / "Plate_1")
    row_id = db.insert_model(
        design_id=1,
        profile_id=None,
        title="T",
        slug="t",
        url="u",
        filename=bare.name,
        file_path=str(bare),
        file_size=1,
    )
    assert manager.fix_file_extensions() == 1
    assert (tmp_path / f"Plate_1-{row_id}.3mf").is_file()
    assert (tmp_path / "Plate_1.3mf").is_file()


# ------------------------------------------------------ concurrent dedup
class _DownloadFakeClient:
    """Fake client for the full download_model path (profile download)."""

    downloads: ClassVar[list[str]] = []

    async def get_design(self, design_id):
        return {"title": "Dice Tower", "slug": "dice", "modelId": "abc"}

    async def get_design_instances(self, design_id):
        return {"hits": [{"id": 1, "profileId": 5}]}

    async def get_profile_download(self, profile_id, model_id):
        return {"url": "https://cdn.example/f", "name": "Plate 1"}

    async def download_file(self, url, dest_path):
        _DownloadFakeClient.downloads.append(url)
        await asyncio.sleep(0.05)  # let the racing request catch up
        _write_3mf(dest_path)
        return dest_path.stat().st_size, "f"

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_concurrent_downloads_of_same_design_download_once(
    db, tmp_path, monkeypatch
):
    monkeypatch.setattr(dl.settings, "download_dir", str(tmp_path / "dl"))
    db.set_meta("bambu_token", "tok")
    manager = DownloadManager(db)
    _DownloadFakeClient.downloads = []
    manager._client = lambda: _DownloadFakeClient()

    url = "https://makerworld.com/en/models/42"
    results = await asyncio.gather(
        manager.download_model(url), manager.download_model(url)
    )

    assert sorted(r["status"] for r in results) == ["downloaded", "exists"]
    assert len(_DownloadFakeClient.downloads) == 1
    saved = Path(next(r for r in results if r["status"] == "downloaded")["path"])
    assert saved.name == "Plate_1.3mf"
    assert saved.is_file()
    assert manager._design_locks == {}  # no lock leak


@pytest.mark.asyncio
async def test_concurrent_syncs_of_same_collection_run_once(db):
    manager = DownloadManager(db)
    db.upsert_collection(1, "Cats", "u", 60)
    listings = []

    class FakeClient:
        async def get_collection_info(self, collection_id):
            await asyncio.sleep(0.05)
            return {"title": "Cats"}

        async def list_collection_designs(self, collection_id, page_size=100):
            listings.append(collection_id)
            return []

        async def close(self):
            return None

    manager._client = lambda: FakeClient()
    results = await asyncio.gather(
        manager.sync_collection(1), manager.sync_collection(1)
    )

    assert sorted(r["status"] for r in results) == ["already-running", "ok"]
    assert listings == [1]
    assert not manager.is_syncing(1)


@pytest.mark.asyncio
async def test_manual_trigger_refused_while_scheduled_sync_runs(db):
    from app.scheduler import trigger_sync

    manager = DownloadManager(db)
    manager._syncing.add(7)  # a scheduled sync is in flight
    assert trigger_sync(manager, 7) is False


# ------------------------------------------------------- file endpoint
def test_model_file_endpoint(app_client, tmp_env):
    client, database, _ = app_client
    model_dir = tmp_env / "downloads" / "coll" / "42-dice"
    model_dir.mkdir(parents=True)
    f = _write_3mf(model_dir / "Plate_1.3mf")
    row_id = database.insert_model(
        design_id=42,
        profile_id=5,
        title="Dice",
        slug="d",
        url="u",
        filename=f.name,
        file_path=str(f),
        file_size=f.stat().st_size,
    )

    r = client.get(f"/api/models/{row_id}/file")
    assert r.status_code == 200
    assert r.content == f.read_bytes()
    assert r.headers["content-type"] == "model/3mf"
    assert 'filename="Plate_1.3mf"' in r.headers["content-disposition"]

    assert client.get("/api/models/999/file").status_code == 404
    f.unlink()
    assert client.get(f"/api/models/{row_id}/file").status_code == 404


def test_model_file_endpoint_refuses_paths_outside_downloads(app_client, tmp_env):
    client, database, _ = app_client
    outside = tmp_env / "secret.txt"
    outside.write_text("nope")
    row_id = database.insert_model(
        design_id=1,
        profile_id=None,
        title="T",
        slug="t",
        url="u",
        filename="secret.txt",
        file_path=str(outside),
        file_size=4,
    )
    assert client.get(f"/api/models/{row_id}/file").status_code == 404
