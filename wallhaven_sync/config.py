from __future__ import annotations

import contextlib
import fcntl
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

API_ROOT = "https://wallhaven.cc/api/v1"

USER_AGENT = "wallhaven-sync/1.0 (personal Noctalia wallpaper pool)"

SCHEME = "m3-content"

DEFAULT_TARGET = 365

MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024

TOPLIST_RANGES = ("1M", "3M", "6M", "1y")

SOURCE_TAGS = (
    "landscape",
    "city",
    "abstract",
    "minimal",
    "space",
    "nature",
    "architecture",
    "cyberpunk",
)

LATEST_MIN_FAVORITES = 100

LATEST_MIN_VIEWS = 2000

DEFAULT_MAX_PER_DAY = 24

DEFAULT_TRASH_DAYS = 7

DEFAULT_MAX_BYTES = 3 * 1024**3

API_ATTEMPTS = 5

DOWNLOAD_ATTEMPTS = 5

BACKOFF_BASE = 2.0

BACKOFF_CAP = 300.0

DB_VERSION = 1

API_DELAY = 1.4

DOWNLOAD_DELAY = 0.25

MIN_LUMA = 0.03

MAX_LUMA = 0.70

MIN_STDDEV = 0.03

MIN_SAT = 0.06

MIN_CHROMA = 10

MUTED_SHARE = 0.10

HUE_CAP_FACTOR = 1.5

SOFT_REASONS = frozenset({"hue-quota", "muted-quota"})

PHASH_SIZE = 16

PHASH_LOW = 8

PHASH_AUTO_DUPLICATE = 3

PHASH_SUSPECT = 7


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


@dataclass(frozen=True)
class Paths:
    pool: Path
    state: Path
    key_file: Path

    @classmethod
    def resolve(cls) -> "Paths":
        return cls(
            pool=_env_path("WALLHAVEN_POOL_DIR", Path.home() / "Pictures" / "wallhaven"),
            state=_env_path("WALLHAVEN_STATE_DIR", _xdg("XDG_STATE_HOME", ".local/state") / "wallhaven-sync"),
            key_file=_env_path("WALLHAVEN_API_KEY_FILE", _xdg("XDG_CONFIG_HOME", ".config") / "wallhaven" / "apikey"),
        )

    @property
    def index(self) -> Path:
        return self.state / "index.json"

    @property
    def lock(self) -> Path:
        return self.state / "lock"

    @property
    def history(self) -> Path:
        return self.state / "history.jsonl"

    @property
    def db(self) -> Path:
        return self.state / "library.db"

    @property
    def trash(self) -> Path:
        return self.pool.parent / f"{self.pool.name}-trash"

    @property
    def quarantine(self) -> Path:
        return self.pool.parent / f"{self.pool.name}-quarantine" / "duplicates"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_index() -> dict:
    return {
        "version": 1,
        "phash_version": 1,
        "updated_at": None,
        "images": {},
        "rejected": {},
        "duplicates": {},
        "banned": {},
        "quarantine": {},
    }


def load_index(paths: Paths) -> dict:
    base = new_index()
    try:
        raw = json.loads(paths.index.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return base
    except json.JSONDecodeError as exc:
        raise SystemExit(f"索引损坏：{paths.index}（{exc}）；请先修复或移走该文件") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise SystemExit(f"索引版本不支持：{paths.index}")
    base.update(raw)
    return base


def save_index(paths: Paths, index: dict) -> None:
    index["updated_at"] = now()
    paths.state.mkdir(parents=True, exist_ok=True)
    tmp = paths.index.with_name(paths.index.name + ".tmp")
    payload = json.dumps(index, ensure_ascii=False, indent=1) + "\n"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, paths.index)


@contextlib.contextmanager
def exclusive_lock(path: Path, shared: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+", encoding="utf-8") as handle:
        mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        try:
            fcntl.flock(handle, mode)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _target_default() -> int:
    try:
        return int(os.environ.get("WALLHAVEN_TARGET", DEFAULT_TARGET))
    except ValueError:
        return DEFAULT_TARGET
