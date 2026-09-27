"""Library → Profiles (list a design's print profiles, download extra ones)
and the three collection sync modes: default / author / all."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

# Classes via the module at call time — see test_skip_failed.py.
import app.downloader as dl
from tests.test_files_dedup import _write_3mf

AUTHOR = {"uid": 2743334393, "name": "Maxx Design"}


def _inst(iid, pid, title, creator):
    # Trimmed from a real /design/3230452/instances response (2026-09-27);
    # the `detail` block there is all zeros, so it's left out.
    return {
        "id": iid,
        "profileId": pid,
        "title": title,
        "creator": creator,
        "createTime": "2026-08-28T12:43:49Z",
    }


INSTANCES = [
    _inst(3658851, 963510835, "Ghost + Stand (No AMS)", AUTHOR),
    _inst(3673207, 968473582, "Ghost + Stand (Multicolor Print)", AUTHOR),
    _inst(
        3716499,
        985349536,
        "多色一盘打印，优化参数，提高打印效率",  # noqa: RUF001 — real title
        {"uid": 1816578993, "name": "Nancy"},
    ),
    _inst(
        3779637,
        1009777739,
        "PETG only Black n White",
        {"uid": 3804175667, "name": "SpyderVenom800"},
    ),
    _inst(
        3825792,
        1028145962,
        "0.16mm layer, 3 walls, 15% infill",
        {"uid": 1398364503, "name": "Dun"},
    ),
]


class _ProfilesClient:
    instance_calls = 0
    downloads: ClassVar[list[int]] = []

    async def get_design(self, design_id):
        return {
            "title": "Mini Ghost Tea Light Lantern",
            "slug": "ghost",
            "modelId": "US27554bf2b50925",
            "designCreator": AUTHOR,
        }

    async def get_design_instances(self, design_id):
        _ProfilesClient.instance_calls += 1
        return {"total": len(INSTANCES), "hits": INSTANCES}

    async def get_profile_download(self, profile_id, model_id):
        _ProfilesClient.downloads.append(profile_id)
        return {"url": "https://cdn.example/f", "name": f"ghost-{profile_id}"}

    async def download_file(self, url, dest_path):
        _write_3mf(dest_path)
        return dest_path.stat().st_size, "f"

    async def close(self):
        return None


@pytest.fixture()
def lib(db, tmp_path, monkeypatch):
    """Library with the lantern's default profile, from collection 7."""
    monkeypatch.setattr(dl.settings, "download_dir", str(tmp_path / "dl"))
    monkeypatch.setattr(dl.settings, "download_delay_seconds", 0)
    monkeypatch.setattr(dl.settings, "profiles_cache_minutes", 15)
    db.set_meta("bambu_token", "tok")
    folder = tmp_path / "dl" / "7-haloween" / "3230452-mini-ghost"
    folder.mkdir(parents=True)
    (folder / "ghost.3mf").write_bytes(b"x")
    db.insert_model(
        design_id=3230452,
        profile_id=963510835,
        title="Mini Ghost Tea Light Lantern",
        slug="ghost",
        url="u",
        filename="ghost.3mf",
        file_path=str(folder / "ghost.3mf"),
        file_size=1,
        collection_id=7,
        collection_title="haloween",
        creator="Maxx Design",
    )
    _ProfilesClient.instance_calls = 0
    _ProfilesClient.downloads = []
    m = dl.DownloadManager(db)
    m._client = lambda: _ProfilesClient()
    return m, folder


@pytest.mark.asyncio
async def test_profiles_list_marks_community_and_downloaded(lib):
    m, _ = lib
    r = await m.design_profiles(3230452)
    assert r["title"] == "Mini Ghost Tea Light Lantern"
    rows = {p["profile_id"]: p for p in r["profiles"]}
    assert len(rows) == 5  # incl. the one makerworld.com hides by region
    assert rows[963510835]["downloaded"] is True
    assert rows[968473582]["downloaded"] is False
    assert [p["community"] for p in r["profiles"]] == [False, False, True, True, True]
    assert rows[1009777739]["creator"] == "SpyderVenom800"
    assert rows[1009777739]["url"].endswith("/models/3230452#profileId-1009777739")


@pytest.mark.asyncio
async def test_profiles_are_cached(lib, monkeypatch):
    m, _ = lib
    first = await m.design_profiles(3230452)
    second = await m.design_profiles(3230452)
    assert _ProfilesClient.instance_calls == 1
    assert first["fetched_at"] == second["fetched_at"]
    monkeypatch.setattr(dl.settings, "profiles_cache_minutes", 0)  # 0 = always ask
    await m.design_profiles(3230452)
    assert _ProfilesClient.instance_calls == 2


@pytest.mark.asyncio
async def test_unknown_author_means_no_community_tag(lib, db):
    m, _ = lib
    with db.connect() as conn:
        conn.execute("UPDATE models SET creator = NULL")
    r = await m.design_profiles(3230452)
    assert {p["community"] for p in r["profiles"]} == {None}


@pytest.mark.asyncio
async def test_profiles_need_a_library_design(lib):
    m, _ = lib
    with pytest.raises(dl.NotFoundError):
        await m.design_profiles(999)
    with pytest.raises(dl.NotFoundError):
        await m.download_profile(999, 1)
    assert _ProfilesClient.instance_calls == 0


@pytest.mark.asyncio
async def test_download_profile_lands_next_to_the_default(lib, db):
    m, folder = lib
    r = await m.download_profile(3230452, 1009777739)
    assert r["status"] == "downloaded"
    assert Path(r["path"]).parent == folder
    assert _ProfilesClient.downloads == [1009777739]
    row = next(x for x in db.design_rows(3230452) if x["profile_id"] == 1009777739)
    assert row["profile_title"] == "PETG only Black n White"
    assert (row["collection_id"], row["collection_title"]) == (7, "haloween")
    assert "profile “PETG only Black n White”" in dl.recent_events(1)[0]["message"]
    # Listed as downloaded now, without another MakerWorld request.
    after = await m.design_profiles(3230452)
    assert {p["profile_id"] for p in after["profiles"] if p["downloaded"]} == {
        963510835,
        1009777739,
    }
    assert _ProfilesClient.instance_calls == 2  # listing + download_model's own


@pytest.mark.asyncio
async def test_download_profile_rejects_unlisted_profile(lib):
    """download_model would fall back to the default profile and store it
    under the requested id — refuse instead."""
    m, _ = lib
    with pytest.raises(dl.NotFoundError):
        await m.download_profile(3230452, 12345)
    assert _ProfilesClient.downloads == []


@pytest.mark.asyncio
async def test_download_profile_again_is_exists(lib):
    m, _ = lib
    r = await m.download_profile(3230452, 963510835)
    assert r["status"] == "exists"


# -------------------------------------------------------------- sync modes
class _SyncClient(_ProfilesClient):
    async def get_collection_info(self, collection_id):
        return {"title": "haloween"}

    async def list_collection_designs(self, collection_id, page_size=100):
        return [{"id": 3230452, "designCreator": AUTHOR}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("default", []),
        ("author", [968473582]),
        ("all", [968473582, 985349536, 1009777739, 1028145962]),
    ],
)
async def test_sync_modes(lib, db, mode, expected):
    m, _ = lib
    m._client = lambda: _SyncClient()
    db.upsert_collection(7, "haloween", "u", 60)
    db.set_collection_plates_mode(7, mode)
    await m.sync_collection(7)
    assert sorted(_ProfilesClient.downloads) == expected


def test_author_filter_falls_back_to_first_profile_creator():
    assert [i["profileId"] for i in dl._authors_instances({}, INSTANCES)] == [
        963510835,
        968473582,
    ]


def test_plates_mode_validation(db):
    db.upsert_collection(7, "c", "u", 60)
    db.set_collection_plates_mode(7, "author")
    assert db.get_collection(7)["plates_mode"] == "author"
    with pytest.raises(ValueError):
        db.set_collection_plates_mode(7, "community")


# ------------------------------------------------------------------ routes
def test_profile_routes(app_client, monkeypatch):
    """Error mapping. Exceptions come from app.routes' namespace: the fixture
    reloads makerworld after downloader, so a real manager call would raise
    classes routes can't match (a test-only artefact)."""
    import app.routes as routes

    client, _, _ = app_client

    async def fake_profiles(design_id):
        if design_id == 999:
            raise routes.NotFoundError("not in library")
        return {
            "design_id": design_id,
            "title": "T",
            "fetched_at": None,
            "profiles": [],
        }

    async def fake_download(design_id, profile_id):
        raise routes.CaptchaError("slow down")

    monkeypatch.setattr(routes.manager, "design_profiles", fake_profiles)
    monkeypatch.setattr(routes.manager, "download_profile", fake_download)
    assert client.get("/api/models/999/profiles").status_code == 404
    assert client.get("/api/models/5/profiles").json()["design_id"] == 5
    assert client.post("/api/models/5/profiles/1/download").status_code == 429
