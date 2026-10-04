"""Push library metadata to Printventory (optional integration).

Printventory (github.com/TechJeeper/Printventory) catalogs the downloads
folder by scanning it (its "STL Home"); this module fills in what a scan
can't know, through its MCP endpoint — plain JSON-RPC over HTTP POST
("Streamable HTTP" transport), no AI involved. Per downloaded file:

- source   = the MakerWorld link (with #profileId), always ours;
- designer = the model's author, only when Printventory's is empty;
- notes    = the print profile's name, only when empty;
- tag      = the collection's title, added (the user's own tags stay).

Files deleted here are removed from Printventory's library (`remove_model`,
library only — the file is already gone). Everything runs in the
background from a queue (models.pv_synced_at, pv_removals): Printventory
being down never blocks a download, and work is retried on the next run.

Verified against Printventory 2.2.10 and 2.2.16 (2026-10-03). Its MCP API is marked
experimental upstream; a changed tool shows up as an error in the UI and
the Activity log, downloads carry on regardless.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from .config import settings
from .db import Database

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
BATCH = 100  # rows per DB read; a run keeps going until the queue is empty
MAX_PER_RUN = 5000  # ...or this many rows (~6 min at the ~15 rows/s seen live)


class PrintventoryError(Exception):
    """Printventory unreachable, or a tool call failed."""


def _parse(resp: httpx.Response) -> dict[str, Any]:
    """JSON-RPC message from a JSON or a single-event SSE response."""
    text = resp.text
    if text.lstrip().startswith(("event:", "data:")):
        text = "".join(
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        )
    try:
        data = json.loads(text)
    except ValueError as e:
        raise PrintventoryError(f"Unexpected MCP reply: {text[:200]}") from e
    if not isinstance(data, dict):
        raise PrintventoryError(f"Unexpected MCP reply: {text[:200]}")
    return data


class McpClient:
    """Minimal MCP client: initialize once, then tools/call."""

    def __init__(self, url: str, http: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/") + "/mcp"
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=300.0))
        self._session: str | None = None
        self._ids = 0
        self.server_version: str | None = None

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session:
            h["Mcp-Session-Id"] = self._session
        return h

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self._http.post(
                self.url, headers=self._headers(), json=payload
            )
        except httpx.HTTPError as e:
            raise PrintventoryError(f"Printventory unreachable: {e}") from e

    async def connect(self) -> None:
        self._session = None
        self._ids += 1
        resp = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._ids,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "bambu-downloader", "version": "1"},
                },
            }
        )
        if resp.status_code != 200:
            raise PrintventoryError(f"MCP initialize failed: HTTP {resp.status_code}")
        info = _parse(resp).get("result", {}).get("serverInfo", {})
        self.server_version = info.get("version")
        self._session = resp.headers.get("mcp-session-id")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def call(self, tool: str, args: dict[str, Any]) -> Any:
        """Call a tool; returns its JSON result (None for "null")."""
        if self._session is None:
            await self.connect()
        for attempt in (1, 2):
            self._ids += 1
            resp = await self._post(
                {
                    "jsonrpc": "2.0",
                    "id": self._ids,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": args},
                }
            )
            if resp.status_code in (400, 404) and attempt == 1:
                await self.connect()  # session expired (Printventory restarted)
                continue
            break
        if resp.status_code != 200:
            raise PrintventoryError(f"{tool}: HTTP {resp.status_code}")
        msg = _parse(resp)
        if "error" in msg:
            raise PrintventoryError(
                f"{tool}: {msg['error'].get('message', msg['error'])}"
            )
        result = msg.get("result") or {}
        text = "".join(
            c.get("text", "")
            for c in result.get("content", [])
            if c.get("type") == "text"
        )
        if result.get("isError"):
            raise PrintventoryError(f"{tool}: {text[:200]}")
        try:
            return json.loads(text) if text else None
        except ValueError:
            return text

    async def close(self) -> None:
        if self._session:
            try:
                await self._http.delete(self.url, headers=self._headers())
            except httpx.HTTPError:
                pass
        self._session = None
        await self._http.aclose()


def source_url(row: dict[str, Any]) -> str:
    slug = f"-{row['slug']}" if row.get("slug") else ""
    pid = f"#profileId-{row['profile_id']}" if row.get("profile_id") else ""
    return f"https://makerworld.com/en/models/{row['design_id']}{slug}{pid}"


class PrintventorySync:
    """Drains the Printventory queue; one run at a time."""

    def __init__(self, db: Database, client_factory: Any = None) -> None:
        self.db = db
        self._factory = client_factory or (lambda: McpClient(settings.printventory_url))
        self._lock = asyncio.Lock()
        self.last_run_at: float | None = None
        self.last_error: str | None = None
        self.server_version: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(settings.printventory_url)

    def map_path(self, local: str) -> str | None:
        """Our file path as Printventory sees it (None: outside downloads/)."""
        root = Path(settings.download_dir).resolve()
        try:
            rel = Path(local).resolve().relative_to(root)
        except ValueError:
            return None
        base = settings.printventory_path or settings.download_dir
        return str(PurePosixPath(base, *rel.parts))

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "url": settings.printventory_url or None,
            "path": settings.printventory_path or settings.download_dir,
            "pending": self.db.pv_pending_count() if self.enabled else 0,
            "running": self._lock.locked(),
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "server_version": self.server_version,
        }

    async def run_once(self) -> dict[str, int]:
        """Send queued removals and metadata. Returns counts; raises
        PrintventoryError when Printventory can't be reached at all."""
        if not self.enabled or self._lock.locked():
            return {"updated": 0, "waiting": 0, "removed": 0}
        async with self._lock:
            client = self._factory()
            try:
                result = await self._run(client)
                self.last_error = None
                return result
            except PrintventoryError as e:
                if str(e) != self.last_error:
                    from .downloader import add_event  # no import cycle at load

                    await add_event("error", f"Printventory sync failed — {e}")
                self.last_error = str(e)
                raise
            finally:
                # A run with nothing to send never connects: keep the last one.
                self.server_version = (
                    getattr(client, "server_version", None) or self.server_version
                )
                self.last_run_at = time.time()
                await client.close()

    async def _run(self, client: Any) -> dict[str, int]:
        removed = 0
        while removed < MAX_PER_RUN and (paths := self.db.pv_removals(BATCH)):
            mapped = [p for p in (self.map_path(x) for x in paths) if p]
            if mapped:
                await client.call(
                    "remove_model", {"filePaths": mapped, "confirm": True}
                )
            self.db.pv_removal_done(paths)
            removed += len(paths)

        updated = waiting = 0
        scanned: set[str] = set()
        after = 0  # rows left waiting stay queued: page past them by id
        while updated + waiting < MAX_PER_RUN and (
            rows := self.db.pv_pending(BATCH, after)
        ):
            after = rows[-1]["id"]
            for row in rows:
                if await self._send(client, row, scanned):
                    updated += 1
                else:
                    waiting += 1
        if updated or removed:
            logger.info(
                "Printventory: %d updated, %d removed, %d not catalogued yet",
                updated,
                removed,
                waiting,
            )
            from .downloader import add_event  # no import cycle at load

            msg = f"Printventory: {updated} updated, {removed} removed"
            await add_event(
                "sync", msg + (f", {waiting} not catalogued yet" if waiting else "")
            )
        return {"updated": updated, "waiting": waiting, "removed": removed}

    async def _send(self, client: Any, row: dict[str, Any], scanned: set[str]) -> bool:
        """Push one row's metadata; False when Printventory doesn't know
        the file yet (the row stays queued)."""
        path = self.map_path(row["file_path"])
        if path is None:
            self.db.pv_mark_synced(row["id"])  # nothing Printventory can see
            return True
        model = await client.call("get_model", {"filePath": path})
        folder = str(PurePosixPath(path).parent)
        if model is None and folder not in scanned:
            # Not catalogued yet: scan just this design's folder rather
            # than wait for Printventory's periodic STL Home scan.
            scanned.add(folder)
            await client.call("scan_directory", {"directory": folder})
            model = await client.call("get_model", {"filePath": path})
        if not isinstance(model, dict):
            return False  # still unknown (path mapping? scan running?)
        fields: dict[str, Any] = {"filePath": path, "source": source_url(row)}
        if not model.get("designer") and row.get("creator"):
            fields["designer"] = row["creator"]
        if not model.get("notes") and row.get("profile_title"):
            fields["notes"] = row["profile_title"]
        await client.call("update_model", fields)
        if row.get("collection_title"):
            await client.call(
                "add_model_tags",
                {"filePath": path, "tags": [row["collection_title"]]},
            )
        self.db.pv_mark_synced(row["id"])
        return True

    async def loop(self, interval: float = 60.0) -> None:
        """Background task: a run every `interval` seconds while enabled."""
        while True:
            if self.enabled:
                try:
                    await self.run_once()
                except PrintventoryError as e:
                    logger.warning("Printventory sync failed: %s", e)
                except Exception:
                    logger.exception("Printventory sync crashed")
            await asyncio.sleep(interval)
