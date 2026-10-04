"""Printventory integration: a fake MCP server (JSON-RPC over HTTP, SSE
replies like the real one) stands in for Printventory 2.2.10."""

from __future__ import annotations

import json

import httpx
import pytest

import app.printventory as pv


class FakePrintventory:
    """Library keyed by filePath; records every tool call."""

    def __init__(self, known=(), catalogue_on_scan=True):
        self.models = {p: {"filePath": p, "designer": None, "notes": None, "tags": []}
                       for p in known}  # fmt: skip
        self.catalogue_on_scan = catalogue_on_scan
        self.calls: list[tuple[str, dict]] = []
        self.sessions = 0
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        method = msg["method"]
        if method == "initialize":
            self.sessions += 1
            return self._reply(
                msg, {"serverInfo": {"name": "printventory", "version": "2.2.10"}},
                headers={"mcp-session-id": f"s{self.sessions}"},
            )  # fmt: skip
        if method == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["mcp-session-id"] == f"s{self.sessions}"
        name, args = msg["params"]["name"], msg["params"]["arguments"]
        self.calls.append((name, args))
        if name == "get_model":
            return self._text(msg, self.models.get(args["filePath"]))
        if name == "scan_directory":
            if self.catalogue_on_scan:
                for p in self.pending_files:
                    if p.startswith(args["directory"] + "/"):
                        self.models.setdefault(
                            p,
                            {
                                "filePath": p,
                                "designer": None,
                                "notes": None,
                                "tags": [],
                            },
                        )
            return self._text(msg, {"started": True})
        if name == "update_model":
            m = self.models[args["filePath"]]
            m.update({k: v for k, v in args.items() if k != "filePath"})
            return self._text(msg, m)
        if name == "add_model_tags":
            self.models[args["filePath"]].setdefault("tags", []).extend(args["tags"])
            return self._text(msg, {"ok": True})
        if name == "remove_model":
            assert args["confirm"] is True
            for p in args["filePaths"]:
                self.models.pop(p, None)
            return self._text(msg, {"removed": len(args["filePaths"])})
        return self._reply(
            msg, {"content": [{"type": "text", "text": "nope"}], "isError": True}
        )

    pending_files: tuple[str, ...] = ()

    @staticmethod
    def _reply(msg, result, headers=None):
        body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        return httpx.Response(
            200, text=f"event: message\ndata: {body}\n\n", headers=headers or {}
        )

    def _text(self, msg, value):
        return self._reply(
            msg, {"content": [{"type": "text", "text": json.dumps(value)}]}
        )


@pytest.fixture()
def setup(db, tmp_path, monkeypatch):
    root = tmp_path / "dl"
    monkeypatch.setattr(pv.settings, "download_dir", str(root))
    monkeypatch.setattr(pv.settings, "printventory_url", "http://pv:5000")
    monkeypatch.setattr(pv.settings, "printventory_path", "/mnt/bambu-backup")
    fake = FakePrintventory()

    def factory():
        return pv.McpClient(
            "http://pv:5000",
            httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )

    sync = pv.PrintventorySync(db, factory)

    def add(design_id, rel, **kw):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        fields = dict(
            design_id=design_id, profile_id=kw.pop("pid", 963510835), title="Mini Ghost",
            slug="mini-ghost", url="u", filename=path.name, file_path=str(path),
            file_size=1, collection_id=7, collection_title="haloween",
            creator="Maxx Design", profile_title="Ghost + Stand (No AMS)",
        )  # fmt: skip
        fields.update(kw)
        return db.insert_model(**fields), "/mnt/bambu-backup/" + rel

    return sync, fake, add


@pytest.mark.asyncio
async def test_pushes_metadata_and_respects_user_edits(setup, db):
    sync, fake, add = setup
    _, p1 = add(3230452, "7-haloween/3230452-mini-ghost/Mini_Ghost__No_AMS.3mf")
    _, p2 = add(2469947, "7-haloween/2469947-spider/Spider__PLA.3mf", title="Spider")
    fake.models[p1] = {
        "filePath": p1,
        "designer": None,
        "notes": None,
        "tags": ["mine"],
    }
    fake.models[p2] = {"filePath": p2, "designer": "Me", "notes": "my note", "tags": []}
    assert await sync.run_once() == {"updated": 2, "waiting": 0, "removed": 0}
    assert fake.models[p1] == {
        "filePath": p1,
        "designer": "Maxx Design",
        "notes": "Ghost + Stand (No AMS)",
        "source": "https://makerworld.com/en/models/3230452-mini-ghost#profileId-963510835",
        "tags": ["mine", "haloween"],  # added, not replaced
    }
    assert fake.models[p2]["designer"] == "Me"  # user's value kept
    assert fake.models[p2]["notes"] == "my note"
    assert db.pv_pending_count() == 0
    fake.calls.clear()
    assert await sync.run_once() == {"updated": 0, "waiting": 0, "removed": 0}
    assert fake.calls == []  # nothing pending -> no calls at all
    assert sync.status()["server_version"] == "2.2.10"


@pytest.mark.asyncio
async def test_uncatalogued_file_scans_its_folder_once(setup, db):
    sync, fake, add = setup
    _, p1 = add(1, "7-h/1-a/A__x.3mf")
    _, p2 = add(1, "7-h/1-a/A__y.3mf", pid=2)
    fake.pending_files = (p1, p2)
    assert (await sync.run_once())["updated"] == 2
    scans = [a for n, a in fake.calls if n == "scan_directory"]
    assert scans == [{"directory": "/mnt/bambu-backup/7-h/1-a"}]


@pytest.mark.asyncio
async def test_still_unknown_after_scan_stays_queued(setup, db):
    sync, fake, add = setup
    fake.catalogue_on_scan = False
    add(1, "7-h/1-a/A__x.3mf")
    assert await sync.run_once() == {"updated": 0, "waiting": 1, "removed": 0}
    assert db.pv_pending_count() == 1


@pytest.mark.asyncio
async def test_deleted_files_are_removed_from_the_library(setup, db, tmp_path):
    from app.downloader import DownloadManager

    sync, fake, add = setup
    rid, p = add(1, "7-h/1-a/A__x.3mf")
    fake.models[p] = {"filePath": p}
    await sync.run_once()
    DownloadManager(db).delete_model(rid, ignore=False)
    assert (await sync.run_once())["removed"] == 1
    assert p not in fake.models
    assert db.pv_removals(10) == []


@pytest.mark.asyncio
async def test_renamed_file_is_sent_again(setup, db):
    sync, fake, add = setup
    rid, p = add(1, "7-h/1-a/A__x.3mf")
    fake.models[p] = {"filePath": p}
    await sync.run_once()
    row = db.get_model(rid)
    db.set_model_file(rid, "A__y.3mf", row["file_path"].replace("A__x", "A__y"))
    assert db.pv_pending_count() == 1


@pytest.mark.asyncio
async def test_down_printventory_reports_once_and_keeps_the_queue(setup, db):
    import app.downloader as dl

    sync, fake, add = setup
    add(1, "7-h/1-a/A__x.3mf")
    fake.down = True
    for _ in range(2):
        with pytest.raises(pv.PrintventoryError):
            await sync.run_once()
    assert "unreachable" in sync.status()["last_error"]
    errors = [
        e
        for e in dl.recent_events(10, "error")
        if e["message"].startswith("Printventory sync failed")
    ]
    assert len(errors) == 1  # not once per minute
    assert db.pv_pending_count() == 1
    fake.down = False
    assert (await sync.run_once())["waiting"] == 1  # reachable again; file unknown
    assert sync.status()["last_error"] is None


@pytest.mark.asyncio
async def test_expired_session_reconnects(setup, db):
    sync, fake, add = setup
    _, p = add(1, "7-h/1-a/A__x.3mf")
    fake.models[p] = {"filePath": p}
    client = sync._factory()
    await client.connect()
    fake.sessions += 1  # server restarted: our session id is stale
    original = fake.handler

    def stale_once(request):
        if request.headers.get("mcp-session-id") == "s1":
            return httpx.Response(404)
        return original(request)

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(stale_once))
    assert (await client.call("get_model", {"filePath": p}))["filePath"] == p
    await client.close()


def test_path_mapping(setup, tmp_path, monkeypatch):
    sync, _, _ = setup
    root = tmp_path / "dl"
    assert sync.map_path(str(root / "7-h" / "x.3mf")) == "/mnt/bambu-backup/7-h/x.3mf"
    assert sync.map_path(str(tmp_path / "elsewhere.3mf")) is None
    monkeypatch.setattr(pv.settings, "printventory_path", "")
    assert sync.map_path(str(root / "x.3mf")) == str(root / "x.3mf")


@pytest.mark.asyncio
async def test_disabled_does_nothing(setup, monkeypatch, db):
    sync, fake, add = setup
    add(1, "7-h/1-a/A__x.3mf")
    monkeypatch.setattr(pv.settings, "printventory_url", "")
    assert await sync.run_once() == {"updated": 0, "waiting": 0, "removed": 0}
    assert fake.sessions == 0
    assert sync.status()["pending"] == 0


def test_routes(app_client, monkeypatch):
    import app.routes as routes

    client, _, _ = app_client
    monkeypatch.setattr(routes.settings, "printventory_url", "")
    assert client.get("/api/status").json()["printventory"]["enabled"] is False
    assert client.post("/api/printventory/sync").status_code == 409


@pytest.mark.asyncio
async def test_one_run_drains_the_whole_queue(setup, db, monkeypatch):
    """Batches are read until the queue is empty (live: ~100 rows in 7 s,
    so waiting a minute per hundred was needless); rows Printventory doesn't
    know are paged past, not retried in a loop."""
    import app.downloader as dl

    monkeypatch.setattr(pv, "BATCH", 3)
    sync, fake, add = setup
    fake.catalogue_on_scan = False
    for i in range(7):
        _, p = add(i + 1, f"7-h/{i}-m/M{i}__x.3mf")
        if i != 3:
            fake.models[p] = {"filePath": p}
    assert await sync.run_once() == {"updated": 6, "waiting": 1, "removed": 0}
    assert db.pv_pending_count() == 1
    last = dl.recent_events(1)[0]
    assert last["message"] == "Printventory: 6 updated, 0 removed, 1 not catalogued yet"
    # A run that changes nothing leaves no Activity entry.
    await sync.run_once()
    assert dl.recent_events(1)[0]["message"] == last["message"]
    assert (
        len([e for e in dl.recent_events(20) if e["message"] == last["message"]]) == 1
    )


def test_static_files_are_revalidated(app_client):
    client, _, _ = app_client
    r = client.get("/static/app.js")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
