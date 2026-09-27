"""SQLite persistence for downloads, auth token, and collection sync state."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id INTEGER NOT NULL,
    profile_id INTEGER,
    collection_id INTEGER,
    title TEXT NOT NULL,
    slug TEXT,
    url TEXT,
    cover_url TEXT,
    collection_title TEXT,
    creator TEXT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER,
    status TEXT NOT NULL DEFAULT 'completed',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- NULL-safe dedup: SQLite treats NULLs as distinct in UNIQUE(design_id,
-- profile_id), so a plain UNIQUE constraint let repeated plate-less
-- downloads of the same design pile up duplicate rows. The coalesced index
-- matches the ON CONFLICT(design_id, COALESCE(profile_id, -1)) upsert.
CREATE UNIQUE INDEX IF NOT EXISTS idx_models_design_profile
    ON models(design_id, COALESCE(profile_id, -1));

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL UNIQUE,
    title TEXT,
    url TEXT NOT NULL,
    sync_interval_minutes INTEGER NOT NULL DEFAULT 360,
    enabled INTEGER NOT NULL DEFAULT 1,
    plates_mode TEXT NOT NULL DEFAULT 'default',
    last_sync_at TEXT,
    last_sync_status TEXT,
    last_sync_new INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

-- Snapshot of the signed-in user's own MakerWorld collections, cached from
-- the my/favorites/listlite endpoint by refresh_my_collections(). This
-- is a cache only: rows are never deleted (a collection that vanishes on
-- MakerWorld is marked hidden instead) and following a collection still
-- writes to `collections`.
-- Per-design failure counter for collection syncs. Only failures caused by
-- the model itself (removed, private, no download URL) are counted; a row
-- reaching BND_MAX_DOWNLOAD_ATTEMPTS makes syncs skip the design. Cleared
-- when the design downloads successfully or the user hits Retry. reason is
-- a short code (no_profile / not_found / forbidden / no_download_url); a
-- no_profile design is skipped after its first failure.
CREATE TABLE IF NOT EXISTS download_failures (
    design_id INTEGER PRIMARY KEY,
    collection_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    last_error TEXT,
    last_attempt_at TEXT NOT NULL
);

-- Activity log shown in the UI's Activity tab. Pruned hourly by the
-- scheduler (BND_EVENT_RETENTION_DAYS / BND_EVENT_MAX_ROWS); freed pages are
-- reused, so the file levels off instead of growing forever.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS remote_collections (
    collection_id INTEGER PRIMARY KEY,
    title TEXT,
    slug TEXT,
    design_count INTEGER NOT NULL DEFAULT 0,
    is_default INTEGER NOT NULL DEFAULT 0,
    design_ids TEXT,
    fetched_at TEXT NOT NULL
);
"""


# remote_collections.design_ids is capped at this many ids per collection
# (makerworld.list_my_collections' max_designs_per_collection default).
CAP_DESIGN_IDS = 1000

# Collection sync modes (column name predates "profiles": a MakerWorld print
# profile is what the code calls a plate).
PLATES_MODES = ("default", "author", "all")

# When a download_failures row makes syncs skip its design: enough attempts,
# or a reason that retrying can't fix (no print profile). Bound to
# (max_attempts, max_attempts); max_attempts 0 disables skipping entirely.
_SKIPPED_SQL = "(? > 0 AND (attempts >= ? OR reason = 'no_profile'))"

# Label shown for models downloaded directly by URL (not via a collection).
MANUAL_DOWNLOAD_LABEL = "Manual download"


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string (used for all DB timestamps)."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thin wrapper over sqlite3 with WAL mode for concurrent access."""

    def __init__(self, db_path: str) -> None:
        """Open (or create) the database and apply migrations.

        Creates the schema if missing, then adds columns introduced after the
        first release (idempotent ALTERs) and backfills origin labels.
        Permission failures — the bind-mounted volume not being writable by
        the container user, common under rootless podman — raise RuntimeError
        with concrete fix instructions instead of a bare sqlite3 error.
        """
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        try:
            with self.connect() as conn:
                conn.executescript(DB_SCHEMA)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "readonly" in msg or "unable to open" in msg:
                raise RuntimeError(
                    f"Cannot write to the database at {db_path} (running as uid {os.getuid()}). "
                    f"The data volume is not writable by the container user (sqlite said: {e}). Fixes:\n"
                    "  podman run --userns=keep-id ...   (rootless podman: your host uid appears\n"
                    "                                     inside the container, bind mounts work)\n"
                    "  podman unshare chown -R 1000:1000 ./data ./downloads\n"
                    "                                     (alternative: give the dirs to your subuid range)\n"
                    "  docker: chown -R 1000:1000 ./data ./downloads on the host"
                ) from e
            raise
        # Lightweight migrations for DBs created before a column existed.
        # "duplicate column name" means the migration already ran — skip.
        with self.connect() as conn:
            for stmt in (
                "ALTER TABLE models ADD COLUMN cover_url TEXT",
                "ALTER TABLE models ADD COLUMN collection_title TEXT",
                "ALTER TABLE models ADD COLUMN creator TEXT",
                "ALTER TABLE collections ADD COLUMN plates_mode TEXT NOT NULL DEFAULT 'default'",
                # Collection size / designs in the library, as of the last sync.
                "ALTER TABLE download_failures ADD COLUMN reason TEXT",
                "ALTER TABLE collections ADD COLUMN last_sync_total INTEGER",
                "ALTER TABLE collections ADD COLUMN last_sync_present INTEGER",
                # The print profile's own name ("Ghost + Stand (No AMS)").
                "ALTER TABLE models ADD COLUMN profile_title TEXT",
                # Hot-path indexes (no-op when they exist). cover: looked up
                # by every /thumb request; created_at: list_models' default
                # sort; collection_id: the label/collection filters.
                "CREATE INDEX IF NOT EXISTS idx_models_cover ON models(cover_url)",
                "CREATE INDEX IF NOT EXISTS idx_models_created ON models(created_at DESC)",
                "CREATE INDEX IF NOT EXISTS idx_models_collection ON models(collection_id)",
            ):
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise
            # Label migration: snapshot the owning collection's title onto
            # models downloaded before labels existed (idempotent — only
            # fills empty titles, so renamed collections aren't clobbered).
            conn.execute(
                """UPDATE models SET collection_title = (
                       SELECT c.title FROM collections c
                       WHERE c.collection_id = models.collection_id)
                   WHERE collection_id IS NOT NULL
                     AND (collection_title IS NULL OR collection_title = '')"""
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a configured connection; commit on success, rollback on error.

        WAL mode plus a 30s busy timeout let the API, scheduler and backfill
        task share one database file without 'database is locked' failures.
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # WAL's standard pairing: commits don't fsync the WAL on every
            # transaction (durable through app crashes; only a host power
            # loss at commit can lose the last transactions, which is
            # acceptable for this archive — a re-sync refills everything).
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- meta (key/value for token etc.) ----
    def get_meta(self, key: str) -> str | None:
        """Read a metadata value (e.g. bambu_token); None if unset."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Insert or update a metadata value."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def delete_meta(self, key: str) -> None:
        """Remove a metadata key (no-op if absent)."""
        with self.connect() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    # ---- models ----
    def model_exists(self, design_id: int, profile_id: int | None) -> bool:
        """Dedup check: has this (design_id, profile_id) pair been downloaded?

        profile_id participates via `IS ?` so a NULL profile (no specific
        plate) matches only NULL rows — mirroring the UNIQUE constraint.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM models WHERE design_id = ? AND profile_id IS ?",
                (design_id, profile_id),
            ).fetchone()
            return row is not None

    def model_exists_any(self, design_id: int) -> bool:
        """Dedup check: has ANY plate of this design been downloaded?

        Syncs (and fragment-less URLs) don't pin a plate, so their dedup is
        design-level: a stored row for ANY profile_id counts as already
        downloaded. Without this, a NULL-vs-stored-plate-id mismatch made
        every sync re-download the whole collection.
        """
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM models WHERE design_id = ?", (design_id,)
            ).fetchone()
            return row is not None

    def design_plates(self, design_id: int) -> list[int | None]:
        """Which plates of this design are already in the library.

        None represents a plate-less/default row. Used by the 'all plates'
        sync mode to know which plate fragments to still fetch.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT profile_id FROM models WHERE design_id = ?", (design_id,)
            ).fetchall()
            return [r["profile_id"] for r in rows]

    def design_fully_pinned(
        self, design_id: int, known_plates: list[int] | None = None
    ) -> bool:
        """Is every plate of this design already downloaded?

        Without an explicit plate list this is only decidable against the
        design's OWN stored rows: with 'all-plates' syncs the caller passes
        the plates it just enumerated so a partially-downloaded multi-plate
        design re-syncs the missing ones.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS c FROM models WHERE design_id = ?", (design_id,)
            ).fetchone()
            stored = rows["c"]
        if known_plates is None:
            return False  # unknown plate list — assume more may exist
        return stored >= len(set(known_plates))

    def insert_model(
        self,
        design_id: int,
        profile_id: int | None,
        title: str,
        slug: str | None,
        url: str | None,
        filename: str,
        file_path: str,
        file_size: int | None,
        collection_id: int | None = None,
        status: str = "completed",
        error: str | None = None,
        cover_url: str | None = None,
        collection_title: str | None = None,
        creator: str | None = None,
        profile_title: str | None = None,
    ) -> int:
        """Insert a downloaded model, or refresh an existing (design, profile) row.

        On conflict the file path/size/status are overwritten, while cover,
        label and creator are only filled in when the new value is non-null
        (COALESCE) — so a re-download never blanks out metadata a backfill
        already fetched. Returns the row id.
        """
        now = utcnow()
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO models(design_id, profile_id, collection_id, title, slug, url,
                   cover_url, collection_title, creator, filename, file_path, file_size,
                   status, error, created_at, updated_at, profile_title)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(design_id, COALESCE(profile_id, -1)) DO UPDATE SET
                     filename=excluded.filename,
                     file_path=excluded.file_path, file_size=excluded.file_size,
                     status=excluded.status, error=excluded.error,
                     cover_url=COALESCE(excluded.cover_url, models.cover_url),
                     collection_title=COALESCE(excluded.collection_title, models.collection_title),
                     creator=COALESCE(excluded.creator, models.creator),
                     profile_title=COALESCE(excluded.profile_title, models.profile_title),
                     updated_at=excluded.updated_at""",
                (
                    design_id,
                    profile_id,
                    collection_id,
                    title,
                    slug,
                    url,
                    cover_url,
                    collection_title,
                    creator,
                    filename,
                    file_path,
                    file_size,
                    status,
                    error,
                    now,
                    now,
                    profile_title,
                ),
            )
            return cur.lastrowid or 0

    def design_rows(self, design_id: int) -> list[dict[str, Any]]:
        """Every library row of a design (one per downloaded profile)."""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM models WHERE design_id = ? ORDER BY id",
                    (design_id,),
                )
            ]

    def get_model(self, model_row_id: int) -> dict[str, Any] | None:
        """Fetch one model row by its primary key."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM models WHERE id = ?", (model_row_id,)
            ).fetchone()
            return dict(row) if row else None

    def model_files(self) -> list[dict[str, Any]]:
        """(id, filename, file_path) of every model row — for file migrations."""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT id, filename, file_path FROM models")
            ]

    def set_model_paths(self, updates: list[tuple[int, str]]) -> None:
        """Repoint several rows' file_path in ONE transaction (all or none)."""
        now = utcnow()
        with self.connect() as conn:
            conn.executemany(
                "UPDATE models SET file_path = ?, updated_at = ? WHERE id = ?",
                [(path, now, row_id) for row_id, path in updates],
            )

    def collection_file_paths(self, collection_id: int) -> list[str]:
        """file_path of every model downloaded through this collection."""
        with self.connect() as conn:
            return [
                r["file_path"]
                for r in conn.execute(
                    "SELECT file_path FROM models WHERE collection_id = ?",
                    (collection_id,),
                )
            ]

    def relabel_collection(self, collection_id: int, title: str) -> int:
        """Point a followed collection's Library label at its new title."""
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE models SET collection_title = ? "
                "WHERE collection_id = ? AND collection_title IS NOT ?",
                (title, collection_id, title),
            )
            return cur.rowcount

    def set_model_file(self, model_row_id: int, filename: str, file_path: str) -> None:
        """Point a model row at a renamed file."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE models SET filename = ?, file_path = ?, updated_at = ? WHERE id = ?",
                (filename, file_path, utcnow(), model_row_id),
            )

    # ---- download failures (skip after N attempts) ----
    def record_failure(
        self,
        design_id: int,
        collection_id: int | None,
        error: str,
        reason: str | None = None,
    ) -> int:
        """Count one failed attempt for a design; returns the new total."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO download_failures(design_id, collection_id, attempts,
                       reason, last_error, last_attempt_at)
                   VALUES(?, ?, 1, ?, ?, ?)
                   ON CONFLICT(design_id) DO UPDATE SET
                     attempts = attempts + 1,
                     collection_id = COALESCE(excluded.collection_id, collection_id),
                     reason = excluded.reason,
                     last_error = excluded.last_error,
                     last_attempt_at = excluded.last_attempt_at""",
                (design_id, collection_id, reason, error, utcnow()),
            )
            row = conn.execute(
                "SELECT attempts FROM download_failures WHERE design_id = ?",
                (design_id,),
            ).fetchone()
            return int(row["attempts"])

    def clear_failure(self, design_id: int) -> bool:
        """Forget a design's failures; True if there were any."""
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM download_failures WHERE design_id = ?", (design_id,)
            )
            return cur.rowcount > 0

    def skipped_design_ids(self, max_attempts: int) -> set[int]:
        """Designs syncs should skip (see _SKIPPED_SQL; 0 = none)."""
        with self.connect() as conn:
            return {
                int(r["design_id"])
                for r in conn.execute(
                    f"SELECT design_id FROM download_failures WHERE {_SKIPPED_SQL}",
                    (max_attempts, max_attempts),
                )
            }

    def skipped_models(self, max_attempts: int) -> list[dict[str, Any]]:
        """Skipped designs with their last error and collection title."""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    f"""SELECT f.*, c.title AS collection_title
                       FROM download_failures f
                       LEFT JOIN collections c ON c.collection_id = f.collection_id
                       WHERE {_SKIPPED_SQL}
                       ORDER BY f.last_attempt_at DESC""",
                    (max_attempts, max_attempts),
                )
            ]

    def update_model_status(
        self, model_row_id: int, status: str, error: str | None = None
    ) -> None:
        """Update a model row's status/error (e.g. mark a failed download)."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE models SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                (status, error, utcnow(), model_row_id),
            )

    # ---- metadata backfill ----
    def models_missing_meta(self, since: str | None = None) -> list[dict[str, Any]]:
        """Rows needing a metadata pass: cover/creator never fetched, or the
        cover.webp file is missing next to the model file.
        NULL = never checked; "" = checked, none exists (those rows only
        re-qualify through the file check when a cover exists).

        since (ISO timestamp): only consider rows whose updated_at is newer.
        Used with mark_meta_scan() so a boot after a clean scan checks just
        the new/changed rows instead of re-statting the whole library.
        """
        query = (
            "SELECT design_id, profile_id, file_path, cover_url, creator FROM models"
        )
        params: list[Any] = []
        if since:
            query += " WHERE updated_at > ?"
            params.append(since)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        missing: list[dict[str, Any]] = []
        for r in rows:
            row = dict(r)
            needs = row["cover_url"] is None or row["creator"] is None
            if not needs and row["file_path"] and row["cover_url"]:
                needs = not (Path(row["file_path"]).parent / "cover.webp").exists()
            if needs:
                missing.append(row)
        return missing

    def meta_scan_state(self) -> tuple[str | None, bool]:
        """(last full-scan timestamp, last scan found nothing missing)."""
        scan_at = self.get_meta("meta_scan_at")
        clean = self.get_meta("meta_scan_clean") == "1"
        return scan_at, clean

    def mark_meta_scan(self, clean: bool) -> None:
        """Record a metadata scan's outcome (see models_missing_meta).

        clean=True establishes the watermark: the next boot only re-checks
        rows updated after this moment. clean=False (missing rows found)
        clears it, so the boot after backfilling re-scans fully to verify.
        """
        self.set_meta("meta_scan_at", utcnow())
        self.set_meta("meta_scan_clean", "1" if clean else "0")

    def find_model_path_by_cover(self, cover_url: str) -> str | None:
        """Locate the model file that owns this cover URL — lets the /thumb
        proxy serve the on-disk cover.webp instead of the CDN."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT file_path FROM models WHERE cover_url = ? LIMIT 1", (cover_url,)
            ).fetchone()
            return row["file_path"] if row else None

    def set_model_meta(
        self,
        design_id: int,
        profile_id: int | None,
        cover_url: str | None,
        creator: str | None = None,
    ) -> None:
        """Fill in a model's cover URL and/or creator name (backfill path).

        Values are COALESCEd, so None means "leave alone" while "" records
        "checked, none exists" — the backfill only looks at NULL rows, so
        empty strings stop it from re-fetching on every boot.
        """
        with self.connect() as conn:
            conn.execute(
                """UPDATE models SET
                     cover_url = COALESCE(?, cover_url),
                     creator = COALESCE(?, creator),
                     updated_at = ?
                   WHERE design_id = ? AND profile_id IS ?""",
                (cover_url, creator, utcnow(), design_id, profile_id),
            )

    @staticmethod
    def _model_filters(
        collection_id: int | None = None,
        no_collection: bool = False,
        label: str | None = None,
        q: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Shared WHERE builder so list/count totals always match the grid.
        Filters combine with AND; q is a case-insensitive (ASCII) substring
        match on title, creator, filename and origin label."""
        where: list[str] = []
        params: list[Any] = []
        if collection_id is not None:
            where.append("collection_id = ?")
            params.append(collection_id)
        if no_collection:
            where.append("collection_id IS NULL")
        if label is not None:
            if label == MANUAL_DOWNLOAD_LABEL:
                where.append("collection_id IS NULL")
            else:
                where.append("collection_title = ?")
                params.append(label)
        if q and q.strip():
            # Escape LIKE wildcards so "100%" or "a_b" match literally.
            pat = "%" + re.sub(r"([\\%_])", r"\\\1", q.strip()) + "%"
            where.append(
                "("
                + " OR ".join(
                    f"{col} LIKE ? ESCAPE '\\'"
                    for col in ("title", "creator", "filename", "collection_title")
                )
                + ")"
            )
            params.extend([pat] * 4)
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def list_models(
        self,
        collection_id: int | None = None,
        no_collection: bool = False,
        label: str | None = None,
        limit: int = 500,
        offset: int = 0,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        """List model rows (newest first) with the given origin filters."""
        where_sql, params = self._model_filters(collection_id, no_collection, label, q)
        query = (
            f"SELECT * FROM models{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        params = [*params, limit, offset]
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def count_models(
        self,
        collection_id: int | None = None,
        no_collection: bool = False,
        label: str | None = None,
        q: str | None = None,
    ) -> int:
        """Count model rows matching the same filters list_models accepts."""
        where_sql, params = self._model_filters(collection_id, no_collection, label, q)
        query = f"SELECT COUNT(*) as c FROM models{where_sql}"
        with self.connect() as conn:
            return conn.execute(query, params).fetchone()["c"]

    # ---- labels ----
    def model_labels(self) -> list[dict[str, Any]]:
        """Origin labels with counts, for the Library filter bar.

        The label is the snapshot of the collection title the model was
        downloaded through; models with no collection are 'Manual download'.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT collection_id, collection_title, COUNT(*) AS n
                   FROM models GROUP BY collection_id, collection_title"""
            ).fetchall()
        labels: list[dict[str, Any]] = []
        for r in rows:
            if r["collection_id"] is None:
                labels.append(
                    {
                        "label": MANUAL_DOWNLOAD_LABEL,
                        "count": r["n"],
                        "collection_id": None,
                    }
                )
            else:
                name = r["collection_title"] or f"Collection #{r['collection_id']}"
                labels.append(
                    {
                        "label": name,
                        "count": r["n"],
                        "collection_id": r["collection_id"],
                    }
                )
        labels.sort(key=lambda x: -x["count"])
        return labels

    # ---- collections ----
    def upsert_collection(
        self, collection_id: int, title: str, url: str, interval: int
    ) -> int:
        """Register or refresh a followed collection; returns the row id."""
        now = utcnow()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO collections(collection_id, title, url, sync_interval_minutes, created_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(collection_id) DO UPDATE SET
                     title=excluded.title, url=excluded.url, sync_interval_minutes=excluded.sync_interval_minutes""",
                (collection_id, title, url, interval, now),
            )
            row = conn.execute(
                "SELECT id FROM collections WHERE collection_id = ?", (collection_id,)
            ).fetchone()
            return row["id"]

    def list_collections(self) -> list[dict[str, Any]]:
        """All followed collections, newest first."""
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM collections ORDER BY created_at DESC"
                ).fetchall()
            ]

    def get_collection(self, collection_id: int) -> dict[str, Any] | None:
        """Fetch one followed collection, or None if not registered."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM collections WHERE collection_id = ?", (collection_id,)
            ).fetchone()
            return dict(row) if row else None

    def set_collection_enabled(self, collection_id: int, enabled: bool) -> None:
        """Pause or resume a collection's automatic syncs."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE collections SET enabled = ? WHERE collection_id = ?",
                (1 if enabled else 0, collection_id),
            )

    def set_collection_interval(self, collection_id: int, minutes: int) -> None:
        """Change a collection's sync interval (minutes)."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE collections SET sync_interval_minutes = ? WHERE collection_id = ?",
                (minutes, collection_id),
            )

    def set_collection_plates_mode(self, collection_id: int, mode: str) -> None:
        """Set a collection's profile-download mode: 'default' (first profile
        of each design), 'author' (every profile by the design's author) or
        'all' (every profile incl. community ones), deduped per design+profile."""
        if mode not in PLATES_MODES:
            raise ValueError(f"Invalid plates mode: {mode!r}")
        with self.connect() as conn:
            conn.execute(
                "UPDATE collections SET plates_mode = ? WHERE collection_id = ?",
                (mode, collection_id),
            )

    def record_sync(
        self,
        collection_id: int,
        status: str,
        new_count: int,
        total: int | None = None,
        present: int | None = None,
    ) -> None:
        """Stamp a collection with the outcome of a sync run.

        Writing last_sync_at here also schedules the next attempt: the
        scheduler only picks a collection up again once its interval has
        elapsed past this timestamp. total/present (collection size and how
        many of its designs are in the library) keep their previous values
        when a failed sync couldn't list the collection (None).
        """
        with self.connect() as conn:
            conn.execute(
                """UPDATE collections SET last_sync_at = ?, last_sync_status = ?,
                     last_sync_new = ?,
                     last_sync_total = COALESCE(?, last_sync_total),
                     last_sync_present = COALESCE(?, last_sync_present)
                   WHERE collection_id = ?""",
                (utcnow(), status, new_count, total, present, collection_id),
            )

    def count_present(self, design_ids: list[int]) -> int:
        """How many of these designs have at least one file in the library."""
        found: set[int] = set()
        ordered = sorted(set(design_ids))
        with self.connect() as conn:
            for start in range(0, len(ordered), 500):
                chunk = ordered[start : start + 500]
                qmarks = ",".join("?" * len(chunk))
                found.update(
                    int(r["design_id"])
                    for r in conn.execute(
                        f"SELECT DISTINCT design_id FROM models WHERE design_id IN ({qmarks})",
                        chunk,
                    )
                )
        return len(found)

    def collection_stats(self, max_attempts: int) -> list[dict[str, Any]]:
        """Per-collection progress for dashboards (Home Assistant etc.)."""
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT c.collection_id, c.title, c.enabled, c.last_sync_at,
                          c.last_sync_status, c.last_sync_new,
                          c.last_sync_total AS total, c.last_sync_present AS present,
                          (SELECT COUNT(*) FROM download_failures f
                            WHERE f.collection_id = c.collection_id
                              AND {_SKIPPED_SQL}) AS skipped
                   FROM collections c ORDER BY c.created_at DESC""",
                (max_attempts, max_attempts),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- events (activity log) ----
    def add_event(self, ts: float, kind: str, message: str, extra: str | None) -> None:
        """Append one activity-log row (extra is a JSON object or None)."""
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(ts, kind, message, extra) VALUES(?, ?, ?, ?)",
                (ts, kind, message, extra),
            )

    def recent_events(
        self, limit: int = 50, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """Newest-first activity rows, optionally of one kind only."""
        where, params = ("WHERE kind = ?", [kind]) if kind else ("", [])
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT ts, kind, message, extra FROM events {where} "
                "ORDER BY id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        out = []
        for r in rows:
            ev = {"ts": r["ts"], "kind": r["kind"], "message": r["message"]}
            if r["extra"]:
                try:
                    ev = {**json.loads(r["extra"]), **ev}
                except ValueError:
                    pass
            out.append(ev)
        return out

    def prune_events(self, max_age_days: int, max_rows: int, now: float) -> int:
        """Drop events older than max_age_days and beyond the newest max_rows
        (0 disables either limit). Returns the number of rows deleted."""
        deleted = 0
        with self.connect() as conn:
            if max_age_days > 0:
                deleted += conn.execute(
                    "DELETE FROM events WHERE ts < ?", (now - max_age_days * 86400,)
                ).rowcount
            if max_rows > 0:
                deleted += conn.execute(
                    """DELETE FROM events WHERE id <= (
                         SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?)""",
                    (max_rows,),
                ).rowcount
        return deleted

    def delete_collection(self, collection_id: int) -> None:
        """Unfollow a collection (model rows are kept; see delete_models)."""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM collections WHERE collection_id = ?", (collection_id,)
            )

    def delete_models(self, collection_id: int) -> int:
        """Remove all library rows belonging to a collection (used by unfollow
        with file deletion). Returns the number of rows removed."""
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM models WHERE collection_id = ?", (collection_id,)
            )
            return cur.rowcount or 0

    def due_collections(self, now_iso: str) -> list[dict[str, Any]]:
        """Enabled collections whose sync interval has elapsed (or never synced).

        The interval math is done in SQLite via julianday() on the stored
        ISO timestamps — no date parsing in Python needed.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM collections WHERE enabled = 1 AND (
                     last_sync_at IS NULL
                     OR (julianday(?) - julianday(last_sync_at)) * 1440.0 >= sync_interval_minutes
                   )""",
                (now_iso,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- remote collections (own MakerWorld collections, cached) ----
    def replace_remote_collections(self, rows: list[dict[str, Any]]) -> None:
        """Overwrite the remote_collections cache with a fresh listing.

        Rows carry {collection_id, title, slug, design_count, is_default,
        design_ids} where design_ids is a list of ints. This is the only
        writer of the table: rows that disappeared from MakerWorld simply
        vanish from the cache view, and nothing here touches the followed
        `collections` table.
        """
        now = utcnow()
        with self.connect() as conn:
            conn.execute("DELETE FROM remote_collections")
            conn.executemany(
                """INSERT INTO remote_collections(
                       collection_id, title, slug, design_count, is_default, design_ids, fetched_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        int(r["collection_id"]),
                        r.get("title") or "",
                        r.get("slug") or "",
                        int(r.get("design_count") or 0),
                        1 if r.get("is_default") else 0,
                        json.dumps(list(r.get("design_ids") or [])),
                        now,
                    )
                    for r in rows
                ],
            )

    def remote_collections_cache(self) -> dict[int, dict[str, Any]]:
        """{collection_id: {design_count, design_ids, slug}} as last fetched —
        what an incremental refresh reuses for unchanged collections."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT collection_id, design_count, design_ids, slug FROM remote_collections"
            ).fetchall()
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            try:
                ids = [int(x) for x in json.loads(r["design_ids"] or "[]")]
            except (ValueError, TypeError):
                continue  # unreadable -> treated as new, re-paged
            out[int(r["collection_id"])] = {
                "design_count": r["design_count"],
                "design_ids": ids,
                "slug": r["slug"] or "",
            }
        return out

    def remote_collections(self) -> list[dict[str, Any]]:
        """Cached own-collections snapshot, each annotated with download state.

        Adds: downloaded_count (library rows among this collection's
        designs), downloaded (count >= design_count when the count is
        known), and checked_ids (which of the stored design ids are already
        in the library) so the UI can render per-item checkmarks.
        """
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM remote_collections ORDER BY is_default DESC, title COLLATE NOCASE"
            ).fetchall()
            # Parse every collection's design-id list first, then check them
            # against the library in ONE query (chunked) — the per-collection
            # IN(...) loop was N+1 (22 queries per read; the UI polls every
            # 5 min with the tab open).
            parsed: list[tuple[sqlite3.Row, list[int]]] = []
            all_ids: set[int] = set()
            for r in rows:
                try:
                    ids = [int(x) for x in json.loads(r["design_ids"] or "[]")]
                except (ValueError, TypeError):
                    ids = []
                parsed.append((r, ids))
                all_ids.update(ids)
            downloaded: set[int] = set()
            ordered = sorted(all_ids)
            for chunk_start in range(0, len(ordered), 500):
                chunk = ordered[chunk_start : chunk_start + 500]
                qmarks = ",".join("?" * len(chunk))
                for d in conn.execute(
                    f"SELECT DISTINCT design_id FROM models WHERE design_id IN ({qmarks})",
                    chunk,
                ):
                    downloaded.add(int(d["design_id"]))
            out: list[dict[str, Any]] = []
            for r, ids in parsed:
                checked_ids = [d for d in ids if d in downloaded]
                item = dict(r)
                item["design_ids"] = ids
                item["checked_ids"] = checked_ids
                item["downloaded_count"] = len(checked_ids)
                # MakerWorld's designCnt also counts designs that were removed
                # or hidden since they were added, but the designs pager only
                # returns visible ones — the gap can never be downloaded.
                # (Only trusted below the pager cap, where ids are complete.)
                count = item["design_count"] or 0
                item["hidden_count"] = (
                    max(0, count - len(ids)) if 0 < len(ids) < CAP_DESIGN_IDS else 0
                )
                available = count - item["hidden_count"]
                item["available_count"] = available
                item["downloaded"] = (
                    item["downloaded_count"] >= available if available > 0 else False
                )
                out.append(item)
            return out

    def clear_remote_collections(self) -> None:
        """Drop the own-collections cache (on sign-out: it's that account's)."""
        with self.connect() as conn:
            conn.execute("DELETE FROM remote_collections")

    def remote_collections_fetched_at(self) -> str | None:
        """When the cache was last refreshed (None = never)."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(fetched_at) AS t FROM remote_collections"
            ).fetchone()
            return row["t"] if row else None
