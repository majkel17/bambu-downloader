"""Bambu Downloader — configuration loaded from environment variables.

Every setting maps to a BND_* environment variable; all are optional and
default to values that work in the container image. The settings object is
instantiated once at import time (`settings`).
"""

import os


def _env(key: str, default: str) -> str:
    """Read an environment variable, falling back to a default string."""
    return os.environ.get(key, default)


class Settings:
    """App settings, sourced from the environment."""

    def __init__(self) -> None:
        """Read all BND_* environment variables into typed settings.

        Values are captured once at import time; per-setting comments below
        explain the anti-abuse / security reasoning behind each default.
        """
        self.download_dir: str = _env("BND_DOWNLOAD_DIR", "/app/downloads")
        self.data_dir: str = _env("BND_DATA_DIR", "/app/data")
        self.db_path: str = _env(
            "BND_DB_PATH", os.path.join(self.data_dir, "bambu_downloader.db")
        )
        self.port: int = int(_env("BND_PORT", "8080"))
        # Bambu Cloud / MakerWorld endpoints. makerworld.com's /api/* JSON
        # gateway is not Cloudflare-challenged (verified), and api.bambulab.com
        # handles login + download endpoints without a challenge.
        self.bambu_api_base: str = _env(
            "BND_BAMBU_API_BASE", "https://api.bambulab.com"
        )
        self.bambu_api_base_cn: str = _env(
            "BND_BAMBU_API_BASE_CN", "https://api.bambulab.cn"
        )
        self.makerworld_base: str = _env(
            "BND_MAKERWORLD_BASE", "https://makerworld.com"
        )
        # Honest client identification — no browser impersonation.
        self.user_agent: str = _env(
            "BND_USER_AGENT", "bambu-downloader/0.1 (personal archiver)"
        )
        # Default sync interval for collections, in minutes.
        self.default_sync_interval_minutes: int = int(
            _env("BND_SYNC_INTERVAL_MINUTES", "360")
        )
        # How often the scheduler wakes up to look for due collections.
        # 5 minutes is plenty: collection sync intervals are >= 15 min.
        self.scheduler_interval_seconds: int = max(
            15, int(_env("BND_SCHEDULER_INTERVAL_SECONDS", "300"))
        )
        # Politeness delay between individual model downloads inside a
        # collection sync. MakerWorld's anti-abuse layer (HTTP 418 CAPTCHA)
        # trips on burst patterns; a few seconds between requests keeps it
        # calm. Zero disables the delay.
        self.download_delay_seconds: float = float(
            _env("BND_DOWNLOAD_DELAY_SECONDS", "3")
        )
        # How often the "my collections" listing is re-fetched from
        # MakerWorld (minutes). Only one light paginated GET every cycle —
        # hourly by default, and a 15-minute floor keeps accidental very
        # small values from hammering the anti-abuse layer.
        self.my_collections_refresh_minutes: int = max(
            15, int(_env("BND_MY_COLLECTIONS_REFRESH_MINUTES", "60"))
        )
        # Refuse new downloads when the downloads volume has less free
        # space than this (MB). 0 disables the check. Guards against
        # truncated .3mf files when the disk fills mid-write.
        self.min_free_mb: int = max(0, int(_env("BND_MIN_FREE_MB", "500")))
        # Syncs stop retrying a model after this many failed attempts where
        # the model itself was the problem (404, 403, no download URL) —
        # removed/censored designs otherwise earn a request (and anti-abuse
        # score) on every sync forever. 0 = retry forever.
        self.max_download_attempts: int = max(
            0, int(_env("BND_MAX_DOWNLOAD_ATTEMPTS", "3"))
        )
        # How long a design's profile list (Library → Profiles) is reused
        # before MakerWorld is asked again. 0 = always ask.
        self.profiles_cache_minutes: int = max(
            0, int(_env("BND_PROFILES_CACHE_MINUTES", "15"))
        )
        # Copy the SQLite database to data/backup/ on boot (before the
        # scheduler touches it). Simple belt-and-suspenders for a NAS-ish
        # setup; keep the most recent copy.
        self.backup_db_on_boot: bool = _env("BND_BACKUP_ON_BOOT", "1") == "1"
        # Optional shared secret for the web API. When set, every /api/*
        # request must carry it in the X-API-Key header — protects the stored
        # Bambu token from anyone else on the network. Empty = open (LAN trust).
        self.api_key: str | None = _env("BND_API_KEY", "").strip() or None
        # Optional second key that only opens read-only (GET) endpoints —
        # for dashboards like Home Assistant, so the full key (which can
        # sign in, download and unfollow) never sits in their config.
        # Only meaningful together with BND_API_KEY.
        self.read_api_key: str | None = _env("BND_READ_API_KEY", "").strip() or None
        # Activity-log retention: rows older than this many days, and rows
        # beyond the newest N, are pruned hourly. 0 disables either limit.
        self.event_retention_days: int = max(
            0, int(_env("BND_EVENT_RETENTION_DAYS", "30"))
        )
        self.event_max_rows: int = max(0, int(_env("BND_EVENT_MAX_ROWS", "5000")))


settings = Settings()
