"""API routes for the downloader."""

from __future__ import annotations

import hmac
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .config import settings
from .db import Database
from .downloader import DownloadManager, recent_events
from .makerworld import (
    AuthRequiredError,
    CaptchaError,
    ForbiddenError,
    MakerWorldClient,
    MakerWorldError,
    NotFoundError,
    get_client,
    invalidate_shared_clients,
    parse_collection_url,
    parse_model_url,
    release_client,
)
from .scheduler import MIN_MINE_REFRESH_MINUTES, SyncScheduler, trigger_sync


def _key_matches(given: str | None, expected: str | None) -> bool:
    """Constant-time key comparison (hmac.compare_digest: no timing leaks)."""
    return bool(given and expected) and hmac.compare_digest(given, expected)


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Gate every /api/* route behind the configured shared secret (if any).

    BND_API_KEY opens everything. BND_READ_API_KEY (optional) opens only
    GET/HEAD — status, stats, library, events — for dashboards that must
    not be able to sign in, download or unfollow. When no BND_API_KEY is
    configured the app stays open — intended for a trusted LAN.
    """
    if not settings.api_key:
        return
    if _key_matches(x_api_key, settings.api_key):
        return
    if _key_matches(x_api_key, settings.read_api_key):
        if request.method in ("GET", "HEAD"):
            return
        raise HTTPException(
            status_code=403, detail="This API key is read-only (BND_READ_API_KEY)"
        )
    raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])

# Wiring is injected from main.py at startup (avoids import cycles).
db: Database  # set in init()
manager: DownloadManager
scheduler: SyncScheduler


def init(database: Database, dl_manager: DownloadManager, sched: SyncScheduler) -> None:
    """Wire the module-level singletons from main.py's lifespan.

    Routes import the module, not instances, to avoid import cycles; this is
    called once at startup before any request is served.
    """
    global db, manager, scheduler
    db = database
    manager = dl_manager
    scheduler = sched


class LoginRequest(BaseModel):
    """Body for POST /api/auth/login: credentials + account region."""

    email: str
    password: str
    region: str = "global"


class VerifyRequest(BaseModel):
    """Body for POST /api/auth/verify: the 2FA code completing a login.

    tfa_key selects the TOTP flow (from the login response); without it the
    emailed code flow is used.
    """

    email: str = ""
    code: str
    tfa_key: str = ""
    region: str = "global"


class TokenRequest(BaseModel):
    """Body for POST /api/auth/token: paste an existing Bambu access token."""

    access_token: str
    region: str = "global"


class DownloadRequest(BaseModel):
    """Body for POST /api/download and /api/resolve: a MakerWorld model URL."""

    url: str


class CollectionAddRequest(BaseModel):
    """Body for POST /api/collections: a collection URL + sync interval."""

    url: str
    sync_interval_minutes: int = 360


class CollectionUpdateRequest(BaseModel):
    """Body for PATCH /api/collections/{id}: interval, enabled, plates mode."""

    sync_interval_minutes: int | None = None
    enabled: bool | None = None
    plates_mode: str | None = None  # 'default' | 'all'


# ------------------------------------------------------------------ status
# Cache "is the token still valid" answers: the UI polls /status every 30s,
# and Bambu is the authority — but a 401-because-outage must not sign users
# out, and we shouldn't round-trip to Bambu on every poll either.
_TOKEN_CHECK_TTL = 300.0
_token_cache: dict[str, tuple[float, bool]] = {}


def _remember_token_state(token: str, valid: bool) -> None:
    """Cache a token validation result for _TOKEN_CHECK_TTL seconds.

    The cache holds a single entry (single-user app): a new login clears any
    previous token's state so its status is re-checked fresh.
    """
    _token_cache.clear()  # single-user app: only the current token matters
    _token_cache[token] = (time.monotonic() + _TOKEN_CHECK_TTL, valid)


async def _token_state() -> bool | None:
    """True/False cached; None = unknown (never treat as signed-out).

    On an explicit rejection, first tries one silent refresh with the
    stored refresh token (downloader.try_token_refresh): if the session is
    renewed the answer is True and the UI never flashes "expired".
    """
    token = db.get_meta("bambu_token")
    if not token:
        return None
    cached = _token_cache.get(token)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    client = get_client()  # anonymous — token is passed per-request
    try:
        valid = await client.validate_token(token)
    finally:
        await release_client(client)
    if (
        valid is False
        and db.get_meta("bambu_token_refresh")
        and await manager.try_token_refresh()
    ):
        return True
    if valid is not None:
        _remember_token_state(token, valid)
    return valid


@router.get("/status")
async def status() -> dict[str, Any]:
    """App overview for the header badge and Settings tab.

    Reports sign-in state (with a cached live token check — tri-state, so a
    Bambu outage never shows as signed-out), library/collection counts and
    scheduler health.
    """
    token = db.get_meta("bambu_token")
    token_email = db.get_meta("bambu_email")
    token_valid = await _token_state() if token else None
    return {
        "authenticated": bool(token) and token_valid is not False,
        "email": token_email if token else None,
        "token_invalid": token_valid is False,
        "region": db.get_meta("bambu_region") or "global",
        "download_dir": settings.download_dir,
        "model_count": db.count_models(),
        "collection_count": len(db.list_collections()),
        "scheduler": scheduler.status(),
        "downloads": manager.queue_status(),
    }


# -------------------------------------------------------------------- auth
@router.post("/auth/login")
async def login(req: LoginRequest) -> dict[str, Any]:
    """Start a Bambu Cloud login with email + password.

    Returns {"step": "done"} and persists the token when Bambu hands one
    over immediately, else {"step": "email_code"|"totp", "tfa_key"} so the
    UI can collect the second factor (POST /api/auth/verify).
    """
    # Ad-hoc client on purpose: login flows (CSRF cookies, pre-auth state)
    # shouldn't share cookies with the pooled identity clients.
    client = MakerWorldClient(region=req.region)
    try:
        result = await client.login(req.email, req.password)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        await release_client(client)
    if result["step"] == "done":
        db.set_meta("bambu_token", result["access_token"])
        db.set_meta("bambu_token_refresh", result.get("refresh_token") or "")
        db.set_meta("bambu_email", req.email)
        db.set_meta("bambu_region", req.region)
        # Stored credentials changed — pooled clients are now stale.
        await invalidate_shared_clients()
        _remember_token_state(result["access_token"], True)
        return {"step": "done", "email": req.email}
    return result


@router.post("/auth/verify")
async def verify(req: VerifyRequest) -> dict[str, Any]:
    """Complete login with the emailed code or a TOTP code.

    On success the token/refresh token/email/region are persisted and the
    validation cache is primed as valid.
    """
    # Ad-hoc client on purpose: login flows (CSRF cookies, pre-auth state)
    # shouldn't share cookies with the pooled identity clients.
    client = MakerWorldClient(region=req.region)
    try:
        if req.tfa_key:
            result = await client.verify_totp(req.tfa_key, req.code)
        else:
            result = await client.verify_email_code(req.email, req.code)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        await release_client(client)
    if result["step"] != "done" or not result.get("access_token"):
        raise HTTPException(status_code=400, detail="Verification failed")
    db.set_meta("bambu_token", result["access_token"])
    db.set_meta("bambu_token_refresh", result.get("refresh_token") or "")
    db.set_meta("bambu_email", req.email)
    db.set_meta("bambu_region", req.region)
    # Stored credentials changed — pooled clients are now stale.
    await invalidate_shared_clients()
    _remember_token_state(result["access_token"], True)
    return {"step": "done", "email": req.email}


@router.post("/auth/token")
async def set_token(req: TokenRequest) -> dict[str, Any]:
    """Sign in by pasting an existing Bambu Cloud access token.

    The token is validated against Bambu first: a rejection is a 400, an
    unreachable Bambu is a 502 (so the UI can say "try again" rather than
    implying the token is bad).
    """
    client = get_client()  # anonymous — the token travels as a parameter
    try:
        valid = await client.validate_token(req.access_token)
    finally:
        await release_client(client)
    if valid is False:
        raise HTTPException(status_code=400, detail="Token rejected by Bambu Cloud")
    if valid is None:
        raise HTTPException(
            status_code=502,
            detail="Could not reach Bambu Cloud to verify the token — try again in a moment",
        )
    db.set_meta("bambu_token", req.access_token)
    db.set_meta("bambu_email", "token-auth")
    db.set_meta("bambu_region", req.region)
    # Stored credentials changed — pooled clients are now stale.
    await invalidate_shared_clients()
    _remember_token_state(req.access_token, True)
    return {"step": "done", "email": "token-auth"}


@router.post("/auth/logout")
async def logout() -> dict[str, Any]:
    """Forget the stored credentials (token, refresh token, email).

    The own-collections cache goes too (it belongs to that account); the
    library, downloaded files and followed collections stay — syncs just
    pause until someone signs in again.
    """
    for key in ("bambu_token", "bambu_token_refresh", "bambu_email"):
        db.delete_meta(key)
    db.clear_remote_collections()
    scheduler.reschedule_mine_refresh()
    # Pooled clients carried the old token — drop them so nothing reused
    # after logout still sends it.
    await invalidate_shared_clients()
    _token_cache.clear()
    return {"ok": True}


# ---------------------------------------------------------------- downloads
@router.post("/download")
async def download(req: DownloadRequest) -> dict[str, Any]:
    """Download a model by URL (dedup: re-downloads report {"status": "exists"}).

    Typed client errors map 1:1 to HTTP codes so the UI can tailor its
    messages: auth -> 401, not found -> 404, forbidden/private -> 403,
    rate-limited -> 429, anything else -> 400.
    """
    try:
        result = await manager.download_model(req.url)
    except AuthRequiredError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ForbiddenError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except CaptchaError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@router.post("/resolve")
async def resolve(req: DownloadRequest) -> dict[str, Any]:
    """Preview a model URL: metadata + plates, no download."""
    try:
        return await manager.resolve_design(req.url)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/models")
async def models(
    collection_id: int | None = None,
    no_collection: bool = False,
    label: str | None = None,
    limit: int = 200,
    offset: int = 0,
    q: str | None = None,
) -> dict[str, Any]:
    """List downloaded models, optionally filtered by origin and search.

    Filters: collection_id (a followed collection), no_collection (manual
    downloads / 'no collection'), label (exact snapshot title match), q
    (substring of title / creator / filename / label).
    """
    return {
        "models": db.list_models(
            collection_id=collection_id,
            no_collection=no_collection,
            label=label,
            limit=limit,
            offset=offset,
            q=q,
        ),
        "total": db.count_models(
            collection_id=collection_id, no_collection=no_collection, label=label, q=q
        ),
    }


# Media types for the file endpoint; anything else is served as a generic blob.
_MEDIA_TYPES = {
    ".3mf": "model/3mf",
    ".stl": "model/stl",
    ".step": "model/step",
    ".zip": "application/zip",
}


@router.get("/models/{model_id}/file")
async def model_file(model_id: int) -> FileResponse:
    """Download a stored model file as an attachment.

    Only files inside the downloads directory are served (the DB path is
    resolved and checked), so a tampered row can't expose the host fs.
    """
    row = db.get_model(model_id)
    if not row:
        raise HTTPException(status_code=404, detail="Model not found")
    root = Path(settings.download_dir).resolve()
    path = Path(row["file_path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="File is missing on disk")
    return FileResponse(
        path,
        filename=path.name,
        media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
    )


@router.get("/model-labels")
async def model_labels() -> list[dict[str, Any]]:
    """Origin labels + counts for the Library filter bar."""
    return db.model_labels()


@router.get("/events")
async def events(limit: int = 50, kind: str | None = None) -> dict[str, Any]:
    """Recent activity-log events, newest first; kind = download/sync/error."""
    return {"events": recent_events(min(max(limit, 1), 1000), kind)}


# -------------------------------------------------------------- collections
@router.get("/collections")
async def collections() -> list[dict[str, Any]]:
    """List all followed collections with their sync state.

    next_sync_at is when the scheduler considers the collection due again
    (last sync + interval; null = due now / never synced); the actual run
    happens on the next scheduler tick after that. syncing = in flight.
    """
    out = []
    for c in db.list_collections():
        next_at = None
        if c["last_sync_at"]:
            try:
                next_at = (
                    datetime.fromisoformat(c["last_sync_at"])
                    + timedelta(minutes=c["sync_interval_minutes"])
                ).isoformat()
            except ValueError:
                pass
        cid = c["collection_id"]
        out.append(
            {
                **c,
                "next_sync_at": next_at,
                "syncing": manager.is_syncing(cid) or cid in scheduler.active,
            }
        )
    return out


@router.get("/my-collections")
async def my_collections() -> dict[str, Any]:
    """Your own MakerWorld collections with per-collection download checkmarks.

    Serves the hourly-refreshed cache (remote_collections table) — never hits
    MakerWorld, so it stays cheap even if the UI polls it. The listing is
    empty until signed in and either the scheduler's first hourly tick or a
    manual POST /my-collections/refresh has run. Each item carries
    downloaded_count / downloaded / checked_ids for the ✓ UI, plus `followed`
    so the list can show which ones are already being synced.
    """
    rows = db.remote_collections()
    followed = {c["collection_id"]: c for c in db.list_collections()}
    for row in rows:
        row["followed"] = row["collection_id"] in followed
        row["sync_interval_minutes"] = (
            followed[row["collection_id"]]["sync_interval_minutes"]
            if row["followed"]
            else None
        )
    return {
        "collections": rows,
        "fetched_at": db.remote_collections_fetched_at(),
        "authenticated": bool(db.get_meta("bambu_token")),
        "refresh_minutes": scheduler.mine_refresh_minutes(),
    }


class MyCollectionsSettings(BaseModel):
    """Body for PUT /api/my-collections/settings."""

    refresh_minutes: int


@router.put("/my-collections/settings")
async def my_collections_settings(req: MyCollectionsSettings) -> dict[str, Any]:
    """Set how often the own-collections listing is re-fetched (minutes).

    Stored in the DB (overrides BND_MY_COLLECTIONS_REFRESH_MINUTES); 15
    minutes is the floor, one week the ceiling. The next refresh is
    rescheduled from the cache's age, so a change doesn't fire a request.
    """
    if not MIN_MINE_REFRESH_MINUTES <= req.refresh_minutes <= 10080:
        raise HTTPException(
            status_code=400,
            detail=f"Interval must be {MIN_MINE_REFRESH_MINUTES} to 10080 minutes",
        )
    db.set_meta("my_collections_refresh_minutes", str(req.refresh_minutes))
    scheduler.reschedule_mine_refresh()
    return {"refresh_minutes": scheduler.mine_refresh_minutes()}


@router.post("/my-collections/refresh")
async def refresh_my_collections_now() -> dict[str, Any]:
    """Re-fetch the own-collections listing from MakerWorld right now.

    Normally the scheduler refreshes it hourly; this exists for the "Refresh"
    button. 401 when signed out, 429 on a CAPTCHA challenge (the listing
    endpoint is subject to the same anti-abuse layer as everything else).
    """
    if not db.get_meta("bambu_token"):
        raise HTTPException(status_code=401, detail="Sign in to MakerWorld first")
    try:
        result = await manager.refresh_my_collections()
    except AuthRequiredError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except CaptchaError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    except MakerWorldError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return result


@router.post("/collections")
async def add_collection(req: CollectionAddRequest) -> dict[str, Any]:
    """Follow a collection: validate the URL against MakerWorld and register it.

    The stored token is attached so the user's OWN private collections
    resolve; a 403 from MakerWorld is translated into a hint about private
    collections needing the owner's account.
    """
    try:
        collection_id = parse_collection_url(req.url)
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # Fetch metadata to validate the collection and get its title. Attach the
    # stored token so the user's OWN PRIVATE collections resolve — anonymous
    # requests get 403 on private collections.
    token = db.get_meta("bambu_token")
    region = db.get_meta("bambu_region") or "global"
    client = get_client(auth_token=token, region=region)
    try:
        info = await client.get_collection_info(collection_id)
    except NotFoundError:
        raise HTTPException(
            status_code=404, detail="Collection not found on MakerWorld"
        ) from None
    except ForbiddenError:
        raise HTTPException(
            status_code=403,
            detail="No access rights to this collection. Sign in first — private "
            "collections need the owner's (or a collaborator's) account.",
        ) from None
    except MakerWorldError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        await release_client(client)
    title = str(info.get("title") or f"collection-{collection_id}")
    db.upsert_collection(collection_id, title, req.url, req.sync_interval_minutes)
    return db.get_collection(collection_id)


@router.patch("/collections/{collection_id}")
async def update_collection(
    collection_id: int, req: CollectionUpdateRequest
) -> dict[str, Any]:
    """Update a followed collection's interval, paused state, or plates mode.

    plates_mode 'default' downloads the first plate of each design; 'all'
    enumerates every plate per design and downloads missing ones (deduped
    per design+plate). Invalid modes are rejected client-side too, but a
    400 here keeps the API honest.
    """
    if not db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Collection not registered")
    if req.plates_mode is not None:
        try:
            db.set_collection_plates_mode(collection_id, req.plates_mode)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    if req.sync_interval_minutes is not None:
        db.set_collection_interval(collection_id, req.sync_interval_minutes)
    if req.enabled is not None:
        db.set_collection_enabled(collection_id, req.enabled)
    return db.get_collection(collection_id)


@router.delete("/collections/{collection_id}")
async def delete_collection(
    collection_id: int, delete_files: bool = False
) -> dict[str, Any]:
    """Unfollow a collection. With delete_files=true, also remove its
    downloaded model files (and their cover.webp + now-empty folders) and
    the matching library rows.

    Deletion is path-guarded: only files that resolve inside the configured
    downloads directory are ever unlinked, so a corrupted file_path in the
    DB can't make us delete arbitrary host files.
    """
    if not db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Collection not registered")
    deleted_files = 0
    failed = 0
    if delete_files:
        rows = db.list_models(collection_id=collection_id, limit=100000)
        dl_root = Path(settings.download_dir).resolve()
        model_dirs: set[Path] = set()
        for row in rows:
            fp = Path(row["file_path"]).resolve()
            try:
                if fp.is_relative_to(dl_root) and fp.is_file():
                    fp.unlink()
                    deleted_files += 1
                cover = fp.parent / "cover.webp"
                if cover.is_file():
                    cover.unlink()
            except OSError:
                failed += 1
            model_dirs.add(fp.parent)
        # Sweep now-empty model folders and the collection folder itself;
        # rmdir only succeeds when empty, so other content is untouched.
        for d in model_dirs | {p.parent for p in model_dirs}:
            if d == dl_root or not d.is_relative_to(dl_root):
                continue
            try:
                d.rmdir()
            except OSError:
                pass
        db.delete_models(collection_id)
    db.delete_collection(collection_id)
    return {"ok": True, "deleted_files": deleted_files, "failed": failed}


def _collection_stats() -> list[dict[str, Any]]:
    """Per-collection progress rows with live sync state and 'missing'."""
    out = []
    for c in db.collection_stats(settings.max_download_attempts):
        cid = c["collection_id"]
        total, present = c["total"], c["present"]
        out.append(
            {
                **c,
                "enabled": bool(c["enabled"]),
                "syncing": manager.is_syncing(cid) or cid in scheduler.active,
                "missing": (
                    max(0, total - present - c["skipped"])
                    if total is not None and present is not None
                    else None
                ),
            }
        )
    return out


@router.get("/collections/stats")
async def collections_stats() -> dict[str, Any]:
    """Progress of every followed collection — for dashboards such as Home
    Assistant (readable with BND_READ_API_KEY). total/present/missing are
    as of each collection's last sync; null until it has synced once."""
    return {"collections": _collection_stats()}


@router.get("/collections/{collection_id}/stats")
async def collection_stats(collection_id: int) -> dict[str, Any]:
    """One collection's progress as a flat object (easy HA REST sensor)."""
    for c in _collection_stats():
        if c["collection_id"] == collection_id:
            return c
    raise HTTPException(status_code=404, detail="Collection not registered")


@router.get("/skipped-models")
async def skipped_models() -> dict[str, Any]:
    """Designs syncs no longer try (BND_MAX_DOWNLOAD_ATTEMPTS model-caused
    failures: removed, private, no download URL)."""
    limit = settings.max_download_attempts
    return {"max_attempts": limit, "models": db.skipped_models(limit)}


@router.post("/skipped-models/{design_id}/retry")
async def retry_skipped_model(design_id: int) -> dict[str, Any]:
    """Reset a design's failure count so the next sync tries it again."""
    if not db.clear_failure(design_id):
        raise HTTPException(status_code=404, detail="Model is not skipped")
    return {"ok": True}


@router.post("/collections/{collection_id}/sync")
async def sync_collection_now(collection_id: int) -> dict[str, Any]:
    """Trigger a background sync of one collection right now.

    Returns {"started": true} or false when a sync for it is already in
    flight. Requires a stored token (401 otherwise) since every download
    needs auth.
    """
    if not db.get_collection(collection_id):
        raise HTTPException(status_code=404, detail="Collection not registered")
    token = db.get_meta("bambu_token")
    if not token:
        raise HTTPException(status_code=401, detail="Sign in to MakerWorld first")
    started = trigger_sync(manager, collection_id)
    return {"started": started}


# --------------------------------------------------------------- share/quick
@router.get("/shared-model")
async def shared_model(url: str) -> dict[str, Any]:
    """PWA share-target entry: validate a shared MakerWorld URL."""
    try:
        parse_model_url(url)
        return {"valid": True, "url": url}
    except MakerWorldError as e:
        return {"valid": False, "detail": str(e)}
