"""Where the model file's link comes from (Bambu's 2026-11-03 API change),
refusing non-model files, and repairing the JSON saved in the meantime."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

# Classes via the module at call time — see test_skip_failed.py.
import app.downloader as dl
import app.makerworld as mw
from tests.test_files_dedup import _write_3mf

URL = "https://makerworld.com/en/models/42"
SETTINGS = json.dumps({"printer_settings_id": "Bambu Lab P1S 0.4 nozzle"})
EMPTY = {"url": "", "context": {"configs": [{"url": "https://cdn.example/cfg"}]}}


class _Client:
    """Two profiles; `instance` / `profile` are a manifest or an exception."""

    def __init__(self, instance=None, profile=None, body=None):
        self.instance = (
            {"url": "https://cdn.example/3mf", "name": "p.3mf"}
            if instance is None
            else instance
        )
        self.profile = EMPTY if profile is None else profile
        self.body = body
        self.calls: list[tuple[str, object]] = []

    async def get_design(self, design_id):
        return {"title": "Dice", "slug": "dice", "modelId": "US1"}

    async def get_design_instances(self, design_id):
        return {
            "hits": [
                {"id": 100, "profileId": 5, "title": "Fine"},
                {"id": 200, "profileId": 6, "title": "Draft"},
            ]
        }

    async def get_instance_manifest(self, instance_id):
        self.calls.append(("instance", instance_id))
        if isinstance(self.instance, Exception):
            raise self.instance
        return self.instance

    async def get_profile_download(self, profile_id, model_id):
        self.calls.append(("profile", profile_id))
        if isinstance(self.profile, Exception):
            raise self.profile
        return self.profile

    async def download_file(self, url, dest_path):
        self.calls.append(("file", url))
        if self.body is None:
            _write_3mf(dest_path)
        else:
            dest_path.write_text(self.body)
        return dest_path.stat().st_size, "f"

    async def close(self):
        return None


@pytest.fixture()
def manager(db, tmp_path, monkeypatch):
    monkeypatch.setattr(dl.settings, "download_dir", str(tmp_path / "dl"))
    monkeypatch.setattr(dl.settings, "download_delay_seconds", 0)
    db.set_meta("bambu_token", "tok")
    return dl.DownloadManager(db)


def _use(manager, client):
    manager._client = lambda: client
    return client


def _files(tmp_path):
    return [p for p in (tmp_path / "dl").rglob("*") if p.is_file()]


# ----------------------------------------------------------- link resolution
@pytest.mark.asyncio
async def test_link_comes_from_the_instance_of_the_pinned_profile(manager):
    client = _use(manager, _Client())
    result = await manager.download_model(URL + "#profileId-6")
    assert client.calls == [("instance", 200), ("file", "https://cdn.example/3mf")]
    assert result["path"].endswith("Dice__Draft.3mf")


@pytest.mark.asyncio
async def test_profile_manifest_is_the_fallback(manager):
    client = _use(
        manager,
        _Client(
            instance=dl.MakerWorldError("HTTP 500"),
            profile={"url": "https://cdn.example/old"},
        ),
    )
    await manager.download_model(URL)
    assert client.calls[-1] == ("file", "https://cdn.example/old")


@pytest.mark.asyncio
async def test_empty_url_never_follows_other_links(manager, db, tmp_path):
    client = _use(manager, _Client(instance=EMPTY))
    with pytest.raises(dl.MakerWorldError) as err:
        await manager.download_model(URL)
    assert [c[0] for c in client.calls] == ["instance", "profile"]
    assert not dl._counts_against_model(err.value)
    assert not db.model_exists_any(42)
    assert _files(tmp_path) == []


@pytest.mark.asyncio
async def test_refused_model_counts_against_it(manager):
    _use(manager, _Client(instance=dl.ForbiddenError("points only")))
    with pytest.raises(dl.ForbiddenError):
        await manager.download_model(URL)


@pytest.mark.asyncio
async def test_json_instead_of_a_model_is_not_saved(manager, db, tmp_path):
    _use(manager, _Client(body=SETTINGS))
    with pytest.raises(dl.MakerWorldError, match="not a 3MF"):
        await manager.download_model(URL)
    assert not db.model_exists_any(42)
    assert _files(tmp_path) == []


# ------------------------------------------------------------------- repair
def _broken_row(db, tmp_path, profile_id=6, name="Dice__Draft"):
    folder = tmp_path / "dl" / "7-games" / "42-dice"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(SETTINGS)
    row_id = db.insert_model(
        design_id=42,
        profile_id=profile_id,
        title="Dice",
        slug="dice",
        url="u",
        filename=name,
        file_path=str(path),
        file_size=path.stat().st_size,
        collection_id=7,
        collection_title="Games",
    )
    return row_id, path


@pytest.mark.asyncio
async def test_repair_replaces_json_with_the_model(manager, db, tmp_path):
    row_id, old = _broken_row(db, tmp_path)
    good = _write_3mf(old.parent / "Dice__Fine.3mf")
    db.insert_model(
        design_id=42,
        profile_id=5,
        title="Dice",
        slug="dice",
        url="u",
        filename=good.name,
        file_path=str(good),
        file_size=1,
    )
    client = _use(manager, _Client())

    assert await manager.repair_broken_downloads() == 1
    assert client.calls[0] == ("instance", 200)  # only the broken profile
    row = db.get_model(row_id)
    assert row["file_path"] == str(old.parent / "Dice__Draft.3mf")
    assert row["collection_title"] == "Games"
    assert Path(row["file_path"]).is_file() and not old.exists()
    assert db.pv_removals(10) == [str(old)]
    assert await manager.repair_broken_downloads() == 0


@pytest.mark.asyncio
async def test_repair_of_a_row_without_profile_leaves_one_row(manager, db, tmp_path):
    _, old = _broken_row(db, tmp_path, profile_id=None)
    _use(manager, _Client())
    assert await manager.repair_broken_downloads() == 1
    assert [r["profile_id"] for r in db.design_rows(42)] == [5]
    assert not old.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile_id", "client"),
    [
        (6, _Client(instance=EMPTY)),  # still no link
        (999, _Client()),  # profile gone: never substitute another one
    ],
)
async def test_failed_repair_keeps_row_and_file(
    manager, db, tmp_path, profile_id, client
):
    row_id, old = _broken_row(db, tmp_path, profile_id=profile_id)
    _use(manager, client)
    assert await manager.repair_broken_downloads() == 0
    assert db.get_model(row_id)["file_path"] == str(old)
    assert old.read_text() == SETTINGS


@pytest.mark.asyncio
async def test_repair_stops_on_captcha(manager, db, tmp_path):
    _broken_row(db, tmp_path, profile_id=5, name="a")
    _broken_row(db, tmp_path, profile_id=6, name="b")
    client = _use(manager, _Client(instance=dl.CaptchaError()))
    assert await manager.repair_broken_downloads() == 0
    assert len(client.calls) == 1


# ------------------------------------------------------------------- client
def _http_client(handler):
    client = mw.MakerWorldClient(auth_token="tok")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_instance_manifest_request():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"url": "https://cdn.example/3mf"})

    client = _http_client(handler)
    try:
        assert (await client.get_instance_manifest(100))["url"]
    finally:
        await client.close()
    assert str(seen[0].url) == (
        "https://api.bambulab.com/v1/design-service/instance/100/f3mf?type=download"
    )
    assert seen[0].headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [(401, "AuthExpiredError"), (403, "ForbiddenError"), (404, "NotFoundError")],
)
async def test_instance_manifest_errors(status, error):
    client = _http_client(lambda r: httpx.Response(status, json={"error": "x"}))
    try:
        with pytest.raises(getattr(mw, error)):
            await client.get_instance_manifest(100)
    finally:
        await client.close()
