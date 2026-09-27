"""Background scheduler that periodically syncs registered collections."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from .config import settings
from .db import Database, utcnow
from .downloader import DownloadManager, add_event
from .makerworld import AuthRequiredError, CaptchaError, MakerWorldError

logger = logging.getLogger(__name__)

# Floor for the own-collections refresh interval (anti-abuse politeness).
MIN_MINE_REFRESH_MINUTES = 15


class SyncScheduler:
    """Polls due collections and syncs them, one at a time."""

    def __init__(self, db: Database, manager: DownloadManager) -> None:
        """Store dependencies; `active` maps collection_id -> 'syncing' for the UI."""
        self.db = db
        self.manager = manager
        self._task: asyncio.Task[None] | None = None
        self._running = False
        # In-flight syncs for UI feedback.
        self.active: dict[int, str] = {}
        # monotonic deadline for the next automatic "my collections" refresh.
        self._next_mine_refresh = 0.0
        # monotonic deadline for the next activity-log prune (hourly).
        self._next_prune = 0.0

    def start(self) -> None:
        """Launch the polling loop (a no-op if it's already running)."""
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="sync-scheduler"
            )

    async def stop(self) -> None:
        """Cancel the scheduler and any manual sync in flight (graceful shutdown)."""
        self._running = False
        tasks: list[asyncio.Task[None]] = []
        if self._task:
            tasks.append(self._task)
            self._task = None
        for task in list(_manual_tasks.values()):
            if not task.done():
                tasks.append(task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.active.clear()
        _manual_tasks.clear()

    async def _loop(self) -> None:
        """Poll forever: sync due collections one at a time, then sleep.

        Each iteration fetches due collections from the DB, syncs them
        sequentially (skipping any already active), records failures in the
        activity log, and sleeps for scheduler_interval_seconds. Once per
        my_collections_refresh_minutes it also re-fetches the user's own
        collection listing into the remote cache. Cancellation is always
        propagated so shutdown stays prompt.
        """
        logger.info("Collection sync scheduler started")
        self.reschedule_mine_refresh()
        while self._running:
            try:
                if time.monotonic() >= self._next_prune:
                    self._prune_events()
                    self._next_prune = time.monotonic() + 3600
                # Signed out: leave the deadline alone so the first tick
                # after sign-in fetches the (cleared) listing right away.
                if time.monotonic() >= self._next_mine_refresh and self.db.get_meta(
                    "bambu_token"
                ):
                    await self._refresh_my_collections()
                    self._next_mine_refresh = (
                        time.monotonic() + self.mine_refresh_minutes() * 60
                    )
                # Signed out: no collection syncs at all — every download
                # needs the token, so a sync would only spend requests on
                # listings. Due collections stay due and run after sign-in.
                if not self.db.get_meta("bambu_token"):
                    due = []
                else:
                    due = self.db.due_collections(utcnow())
                for coll in due:
                    cid = coll["collection_id"]
                    if not self._running:
                        break
                    if cid in self.active or self.manager.is_syncing(cid):
                        continue  # e.g. a manual "Sync now" is running
                    self.active[cid] = "syncing"
                    try:
                        await self.manager.sync_collection(cid)
                    except AuthRequiredError:
                        # Listing-stage auth failure (before any download
                        # attempt) — try one silent token refresh, then
                        # retry the sync once. Otherwise record the abort.
                        if await self.manager.try_token_refresh():
                            try:
                                await self.manager.sync_collection(cid)
                            except MakerWorldError as e:
                                await add_event(
                                    "error",
                                    f"Collection {cid} sync failed after token refresh — {e}",
                                )
                                self.db.record_sync(cid, "error", 0)
                            continue  # refresh worked: move on regardless
                        await add_event(
                            "error",
                            f"Collection {cid} sync skipped — not signed in to MakerWorld",
                        )
                        self.db.record_sync(cid, "auth-required", 0)
                    except CaptchaError:
                        # Same: safety net for listing-stage challenges.
                        await add_event(
                            "error",
                            f"Collection {cid} sync skipped — rate-limited (CAPTCHA)",
                        )
                        self.db.record_sync(cid, "captcha", 0)
                    except asyncio.CancelledError:
                        self.active.pop(cid, None)
                        raise
                    except MakerWorldError as e:
                        await add_event("error", f"Collection {cid} sync failed — {e}")
                        self.db.record_sync(cid, "error", 0)
                    finally:
                        self.active.pop(cid, None)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler iteration failed")
            # Poll interval is configurable; a small floor avoids busy-looping.
            await asyncio.sleep(settings.scheduler_interval_seconds)

    def mine_refresh_minutes(self) -> int:
        """Own-collections refresh interval: the UI setting (meta) if set,
        else BND_MY_COLLECTIONS_REFRESH_MINUTES; never under 15 minutes."""
        try:
            stored = int(self.db.get_meta("my_collections_refresh_minutes") or 0)
        except ValueError:
            stored = 0
        return max(
            MIN_MINE_REFRESH_MINUTES, stored or settings.my_collections_refresh_minutes
        )

    def reschedule_mine_refresh(self) -> None:
        """Set the next own-collections refresh from the cache's age.

        Due immediately when never fetched — UNLESS the cache is fresh (e.g.
        a quick container restart or an interval change): then the deadline
        is backdated so the event itself causes no MakerWorld request.
        """
        fetched_at = self.db.remote_collections_fetched_at()
        if not fetched_at:
            self._next_mine_refresh = 0.0
            return
        try:
            age = (
                datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
            ).total_seconds()
        except (ValueError, TypeError):
            self._next_mine_refresh = 0.0
            return
        remaining = self.mine_refresh_minutes() * 60 - age
        self._next_mine_refresh = time.monotonic() + max(0.0, remaining)

    def _prune_events(self) -> None:
        """Apply the activity-log retention (BND_EVENT_RETENTION_DAYS /
        BND_EVENT_MAX_ROWS). Never disturbs the loop over a DB hiccup."""
        try:
            deleted = self.db.prune_events(
                settings.event_retention_days, settings.event_max_rows, time.time()
            )
        except Exception:
            logger.exception("Activity-log prune failed")
            return
        if deleted:
            logger.info("Pruned %d old activity-log entries", deleted)

    async def _refresh_my_collections(self) -> None:
        """Re-fetch the signed-in user's collection listing into the cache.

        Best-effort: signed-out users are skipped silently (no token), and a
        failure is logged but never disturbs the sync loop — a stale listing
        is better than a crashed scheduler. Manual refreshes from the UI go
        through the API route instead (they surface errors to the user).
        """
        if not self.db.get_meta("bambu_token") or self.manager.mine_refreshing:
            return  # signed out, or the Refresh button beat us to it
        try:
            await self.manager.refresh_my_collections()
            logger.info("Own-collections listing refreshed")
        except AuthRequiredError:
            return  # token vanished mid-session; nothing to do this cycle
        except CaptchaError:
            logger.warning("Own-collections refresh skipped — rate-limited (CAPTCHA)")
        except MakerWorldError as e:
            logger.warning("Own-collections refresh failed: %s", e)

    def status(self) -> dict[str, Any]:
        """Snapshot for /api/status: is the loop running and what's in flight."""
        return {
            "running": self._running,
            "active": dict(self.active),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "next_my_collections_refresh": (
                max(0.0, self._next_mine_refresh - time.monotonic())
                if self._running
                else None
            ),
        }


# Manual trigger helper used by API routes: run a sync now in the background.
_manual_tasks: dict[int, asyncio.Task[None]] = {}


def trigger_sync(manager: DownloadManager, collection_id: int) -> bool:
    """Fire a background sync for a collection. Returns False if one is already running.

    Manual syncs run as their own tasks tracked in _manual_tasks so the
    scheduler's stop() can cancel them during graceful shutdown.
    """
    existing = _manual_tasks.get(collection_id)
    if (existing and not existing.done()) or manager.is_syncing(collection_id):
        return False

    async def run() -> None:
        """Execute the manual sync, surfacing failures to the activity log.

        (Inner closure: trigger_sync's return value already told the caller
        the sync started; errors land in the Activity tab instead.)
        """
        try:
            await manager.sync_collection(collection_id)
        except MakerWorldError as e:
            await add_event(
                "error", f"Manual sync of collection {collection_id} failed — {e}"
            )

    _manual_tasks[collection_id] = asyncio.get_running_loop().create_task(
        run(), name=f"sync-{collection_id}"
    )
    return True
