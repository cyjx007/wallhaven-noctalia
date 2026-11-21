#!/usr/bin/env python3
"""wallhaven-sync - maintain a de-duplicated wallhaven pool for Noctalia.

The pool is a plain directory of image files.  Noctalia owns everything after
that: wallpaper rotation, Material You palette generation and app templates.
This tool only decides *which* images enter the pool.

Commands
    sync      top up the pool from wallhaven toplists
    status    pool statistics, hue distribution and rotation progress
    doctor    check key, pool and Noctalia/Umbriel integration
    ban       remove an image and never download it again
    add       download one wallhaven id or URL
    preview   palette swatches and readability stats for a file

Only the Python standard library is used.  ImageMagick (`magick`) and the
`noctalia` CLI are optional at runtime: without them the corresponding quality
checks are skipped with a warning.
"""

from __future__ import annotations

import argparse
import colorsys
from collections import Counter
import contextlib
import fcntl
import hashlib
import http.client
import json
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
DEFAULT_MAX_BYTES = 3 * 1024**3  # 3 GiB hard cap for the active pool
API_ATTEMPTS = 5
DOWNLOAD_ATTEMPTS = 5
BACKOFF_BASE = 2.0
BACKOFF_CAP = 300.0
DB_VERSION = 1
API_DELAY = 1.4  # wallhaven 限流 45 次/分钟；1.4s 约 43 次
_last_api_call = 0.0
DOWNLOAD_DELAY = 0.25

# Quality gates.  Material You already guarantees text/background contrast
# (~14:1), so the useful filters are image brightness (a white wallpaper under
# translucent panels is what actually hurts readability), flat/greyscale
# images and hue diversity.
MIN_LUMA = 0.03
MAX_LUMA = 0.70
MIN_STDDEV = 0.03
MIN_SAT = 0.06
MIN_CHROMA = 10
MUTED_SHARE = 0.10
HUE_CAP_FACTOR = 1.5
SOFT_REASONS = frozenset({"hue-quota", "muted-quota"})

# pHash: 16x16 DCT, top-left 8x8, median threshold (64-bit fingerprint).
PHASH_SIZE = 16
PHASH_LOW = 8
PHASH_AUTO_DUPLICATE = 3
PHASH_SUSPECT = 7


# --------------------------------------------------------------------------- #
# paths / index
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# metadata database (SQLite)
# --------------------------------------------------------------------------- #

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    file TEXT,
    file_type TEXT,
    source TEXT,
    theme TEXT,
    width INTEGER,
    height INTEGER,
    resolution TEXT,
    bytes INTEGER,
    sha256 TEXT,
    phash TEXT,
    luma REAL,
    stddev REAL,
    sat REAL,
    primary_color TEXT,
    hue INTEGER,
    chroma INTEGER,
    favorites INTEGER,
    views INTEGER,
    category TEXT,
    purity TEXT,
    created_at TEXT,
    url TEXT,
    thumb_url TEXT,
    tags TEXT,
    added_at TEXT,
    first_shown_at TEXT,
    last_shown_at TEXT,
    shown_count INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    evicted_at TEXT,
    evicted_reason TEXT,
    trash_path TEXT
);
CREATE TABLE IF NOT EXISTS display_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT,
    shown_at TEXT,
    connector TEXT,
    source TEXT,
    path TEXT
);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
CREATE INDEX IF NOT EXISTS idx_images_phash ON images(phash);
CREATE INDEX IF NOT EXISTS idx_images_hue ON images(hue);
CREATE INDEX IF NOT EXISTS idx_images_last_shown ON images(last_shown_at);
CREATE INDEX IF NOT EXISTS idx_history_id ON display_history(id);
"""


def db_init(conn: sqlite3.Connection) -> None:
    conn.executescript(DB_SCHEMA)
    # Forward-only, additive migrations for databases created by older versions.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(images)")}
    for name, declaration in (
        ("file_type", "TEXT"),
        ("theme", "TEXT"),
        ("created_at", "TEXT"),
        ("trash_path", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE images ADD COLUMN {name} {declaration}")
    if conn.execute("PRAGMA user_version").fetchone()[0] < DB_VERSION:
        conn.execute(f"PRAGMA user_version = {DB_VERSION}")
    conn.commit()


@contextlib.contextmanager
def open_db(paths: Paths, timeout: float = 5.0):
    paths.state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.db, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        db_init(conn)
        yield conn
    finally:
        conn.close()


def entry_metadata(item: dict, source: str) -> dict:
    """Collect the API fields worth keeping.  Tags are filled in at eviction."""
    thumbs = item.get("thumbs") or {}
    return {
        "source": source,
        "theme": theme_for_source(source),
        "width": item.get("dimension_x"),
        "height": item.get("dimension_y"),
        "resolution": item.get("resolution"),
        "file_type": item.get("file_type"),
        "url": item.get("url"),
        "thumb_url": thumbs.get("large") or thumbs.get("original"),
        "favorites": item.get("favorites"),
        "views": item.get("views"),
        "category": item.get("category"),
        "purity": item.get("purity"),
        "created_at": item.get("created_at"),
    }


def theme_for_source(source: str) -> str:
    if source.startswith("tag:"):
        return source.split(":", 3)[1]
    if source.startswith("latest"):
        return "latest"
    if source.startswith("toplist"):
        return "toplist"
    if source.startswith("restored"):
        return "restored"
    return "manual"


def _entry_values(image_id: str, entry: dict, status: str) -> dict:
    stats = entry.get("stats") or {}
    palette = entry.get("palette") or {}
    tags = entry.get("tags")
    if isinstance(tags, list):
        tags = json.dumps(tags, ensure_ascii=False)
    return {
        "id": image_id,
        "status": status,
        "file": entry.get("file"),
        "file_type": entry.get("file_type"),
        "source": entry.get("source"),
        "theme": entry.get("theme"),
        "width": entry.get("width"),
        "height": entry.get("height"),
        "resolution": entry.get("resolution"),
        "bytes": entry.get("bytes"),
        "sha256": entry.get("sha256"),
        "phash": entry.get("phash"),
        "luma": stats.get("luma"),
        "stddev": stats.get("stddev"),
        "sat": stats.get("sat"),
        "primary_color": palette.get("primary"),
        "hue": palette.get("hue"),
        "chroma": palette.get("chroma"),
        "favorites": entry.get("favorites"),
        "views": entry.get("views"),
        "category": entry.get("category"),
        "purity": entry.get("purity"),
        "created_at": entry.get("created_at"),
        "url": entry.get("url"),
        "thumb_url": entry.get("thumb_url"),
        "tags": tags,
        "added_at": entry.get("added_at"),
    }


def db_upsert_entry(conn: sqlite3.Connection, image_id: str, entry: dict, status: str = "active") -> None:
    values = _entry_values(image_id, entry, status)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    conn.execute(
        f"INSERT INTO images ({columns}) VALUES ({placeholders}) "
        "ON CONFLICT(id) DO UPDATE SET "
        "status=excluded.status, file=excluded.file, source=excluded.source, "
        "file_type=COALESCE(excluded.file_type, images.file_type), "
        "theme=COALESCE(excluded.theme, images.theme), "
        "width=COALESCE(excluded.width, images.width), height=COALESCE(excluded.height, images.height), "
        "resolution=COALESCE(excluded.resolution, images.resolution), "
        "bytes=COALESCE(excluded.bytes, images.bytes), "
        "sha256=COALESCE(excluded.sha256, images.sha256), "
        "phash=COALESCE(excluded.phash, images.phash), "
        "luma=COALESCE(excluded.luma, images.luma), "
        "stddev=COALESCE(excluded.stddev, images.stddev), sat=COALESCE(excluded.sat, images.sat), "
        "primary_color=COALESCE(excluded.primary_color, images.primary_color), "
        "hue=COALESCE(excluded.hue, images.hue), chroma=COALESCE(excluded.chroma, images.chroma), "
        "favorites=COALESCE(excluded.favorites, images.favorites), "
        "views=COALESCE(excluded.views, images.views), "
        "category=COALESCE(excluded.category, images.category), "
        "purity=COALESCE(excluded.purity, images.purity), "
        "created_at=COALESCE(excluded.created_at, images.created_at), "
        "url=COALESCE(excluded.url, images.url), "
        "thumb_url=COALESCE(excluded.thumb_url, images.thumb_url), "
        "tags=COALESCE(excluded.tags, images.tags), "
        "added_at=COALESCE(images.added_at, excluded.added_at), "
        "first_shown_at=COALESCE(images.first_shown_at, excluded.first_shown_at), "
        "last_shown_at=COALESCE(images.last_shown_at, excluded.last_shown_at), "
        "evicted_at=CASE WHEN excluded.status = 'active' THEN NULL ELSE images.evicted_at END, "
        "evicted_reason=CASE WHEN excluded.status = 'active' THEN NULL ELSE images.evicted_reason END, "
        "trash_path=CASE WHEN excluded.status = 'active' THEN NULL ELSE images.trash_path END",
        values,
    )


def mirror_index_to_db(paths: Paths, index: dict) -> None:
    """Mirror index.json into SQLite; rows that left the index become evicted."""
    try:
        with open_db(paths) as conn:
            for image_id, entry in index["images"].items():
                db_upsert_entry(conn, image_id, entry, "active")
            active_ids = set(index["images"])
            stamp = now()
            for row in conn.execute("SELECT id FROM images WHERE status = 'active'").fetchall():
                if row["id"] not in active_ids:
                    conn.execute(
                        "UPDATE images SET status='evicted', evicted_at=?, "
                        "evicted_reason=COALESCE(evicted_reason, 'missing') WHERE id = ?",
                        (stamp, row["id"]),
                    )
            conn.commit()
    except sqlite3.Error as exc:
        warn(f"元数据库同步失败：{exc}")


def db_blockers(paths: Paths) -> tuple[set[str], dict[str, str], dict[str, str]]:
    """IDs, pHashes and SHA-256 values of images that must not be downloaded again."""
    ids: set[str] = set()
    phashes: dict[str, str] = {}
    shas: dict[str, str] = {}
    if not paths.db.exists():
        return ids, phashes, shas
    try:
        with open_db(paths) as conn:
            for row in conn.execute(
                "SELECT id, phash, sha256 FROM images WHERE status != 'active'"
            ):
                ids.add(row["id"])
                if row["phash"]:
                    phashes[row["id"]] = row["phash"]
                if row["sha256"]:
                    shas[row["sha256"]] = row["id"]
    except sqlite3.Error as exc:
        warn(f"读取元数据库失败：{exc}")
    return ids, phashes, shas


def db_set_status(
    paths: Paths,
    image_id: str,
    status: str,
    reason: str | None = None,
    trash_path: str | None = None,
) -> None:
    try:
        with open_db(paths) as conn:
            conn.execute(
                "UPDATE images SET status = ?, evicted_at = ?, evicted_reason = ?, "
                "trash_path = COALESCE(?, trash_path) WHERE id = ?",
                (status, now() if status != "active" else None, reason, trash_path, image_id),
            )
            if status == "active":
                conn.execute(
                    "UPDATE images SET evicted_at = NULL, evicted_reason = NULL, "
                    "trash_path = NULL WHERE id = ?",
                    (image_id,),
                )
            conn.commit()
    except sqlite3.Error as exc:
        warn(f"更新 {image_id} 状态失败：{exc}")


def db_record_display(paths: Paths, image_id: str, connector: str | None, path: str) -> None:
    """Best-effort hook write: raw event + shown_count.  Never raises."""
    try:
        with open_db(paths, timeout=1.0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO images (id, status, file, source) VALUES (?, 'active', ?, 'noctalia')",
                (image_id, Path(path).name),
            )
            last = conn.execute(
                "SELECT id FROM display_history WHERE connector IS ? "
                "ORDER BY seq DESC LIMIT 1",
                (connector,),
            ).fetchone()
            repeated = bool(last and last["id"] == image_id)
            conn.execute(
                "INSERT INTO display_history (id, shown_at, connector, source, path) "
                "VALUES (?, ?, ?, 'noctalia', ?)",
                (image_id, now(), connector, path),
            )
            if not repeated:
                stamp = now()
                conn.execute(
                    "UPDATE images SET shown_count = shown_count + 1, "
                    "first_shown_at = COALESCE(first_shown_at, ?), "
                    "last_shown_at = ? WHERE id = ?",
                    (stamp, stamp, image_id),
                )
            conn.commit()
    except sqlite3.Error as exc:
        warn(f"元数据库记录失败：{exc}")


def db_get(paths: Paths, image_id: str) -> dict | None:
    if not paths.db.exists():
        return None
    try:
        with open_db(paths) as conn:
            row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
            return dict(row) if row else None
    except sqlite3.Error as exc:
        warn(f"读取元数据库失败：{exc}")
        return None


def db_pinned(paths: Paths) -> set[str]:
    if not paths.db.exists():
        return set()
    try:
        with open_db(paths) as conn:
            return {
                row["id"]
                for row in conn.execute("SELECT id FROM images WHERE pinned = 1").fetchall()
            }
    except sqlite3.Error as exc:
        warn(f"读取 pin 列表失败：{exc}")
        return set()


def db_set_pinned(paths: Paths, image_id: str, pinned: bool) -> bool:
    try:
        with open_db(paths) as conn:
            cursor = conn.execute(
                "UPDATE images SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, image_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as exc:
        warn(f"更新 pin 失败：{exc}")
        return False


def db_pin_ids(paths: Paths, image_ids: set[str]) -> int:
    total = 0
    try:
        with open_db(paths) as conn:
            for image_id in image_ids:
                total += conn.execute(
                    "UPDATE images SET pinned = 1 WHERE id = ?", (image_id,)
                ).rowcount
            conn.commit()
    except sqlite3.Error as exc:
        warn(f"批量 pin 失败：{exc}")
    return total


def db_counts(paths: Paths) -> dict[str, int]:
    counts = {"active": 0, "evicted": 0, "quarantine": 0, "banned": 0, "pinned": 0}
    if not paths.db.exists():
        return counts
    try:
        with open_db(paths) as conn:
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM images GROUP BY status"):
                counts[str(row["status"])] = int(row["n"])
            counts["pinned"] = int(
                conn.execute("SELECT COUNT(*) FROM images WHERE pinned = 1").fetchone()[0]
            )
    except sqlite3.Error:
        pass
    return counts


def db_eviction_rows(paths: Paths) -> list[dict]:
    """Active rows used to choose least-recently-shown victims."""
    if not paths.db.exists():
        return []
    try:
        with open_db(paths) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id, last_shown_at, added_at, pinned FROM images WHERE status = 'active'"
                )
            ]
    except sqlite3.Error as exc:
        warn(f"读取淘汰候选失败：{exc}")
        return []


def db_recently_shown(paths: Paths, hours: float) -> set[str]:
    if hours <= 0 or not paths.db.exists():
        return set()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    try:
        with open_db(paths) as conn:
            return {
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM images WHERE last_shown_at IS NOT NULL AND last_shown_at >= ?",
                    (cutoff,),
                )
            }
    except sqlite3.Error as exc:
        warn(f"读取最近展示记录失败：{exc}")
        return set()


def db_evicted_today(paths: Paths) -> int:
    if not paths.db.exists():
        return 0
    # "Today" follows the user's local timezone; evicted_at is stored in UTC.
    local_now = datetime.now().astimezone()
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = start_local.astimezone(timezone.utc).replace(microsecond=0).isoformat()
    end = (start_local + timedelta(days=1)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
    try:
        with open_db(paths) as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM images WHERE evicted_at >= ? AND evicted_at < ? "
                    "AND evicted_reason = 'rotation'",
                    (start, end),
                ).fetchone()[0]
            )
    except sqlite3.Error:
        return 0


def db_update_tags(paths: Paths, image_id: str, tags: list[str]) -> None:
    try:
        with open_db(paths) as conn:
            conn.execute(
                "UPDATE images SET tags = ? WHERE id = ?",
                (json.dumps(tags, ensure_ascii=False), image_id),
            )
            conn.commit()
    except sqlite3.Error as exc:
        warn(f"写入 {image_id} 标签失败：{exc}")


def db_recent_evictions(paths: Paths, limit: int = 5) -> list[dict]:
    if not paths.db.exists():
        return []
    try:
        with open_db(paths) as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT id, evicted_at, evicted_reason, shown_count, favorites, views "
                    "FROM images WHERE status = 'evicted' "
                    "ORDER BY evicted_at DESC LIMIT ?",
                    (limit,),
                )
            ]
    except sqlite3.Error:
        return []


# --------------------------------------------------------------------------- #
# wallhaven API / download
# --------------------------------------------------------------------------- #


def read_key(paths: Paths) -> str:
    try:
        key = paths.key_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise SystemExit(
            f"找不到 API key：{paths.key_file}\n"
            "在 https://wallhaven.cc/settings/account 生成后写入该文件（chmod 600）。"
        ) from None
    if len(key) < 16:
        raise SystemExit(f"API key 看起来不完整：{paths.key_file}")
    return key


def retry_delay(attempt: int, retry_after: int | None = None) -> float:
    """Exponential backoff with jitter; honour Retry-After if it is longer."""
    base = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** max(0, attempt - 1)))
    delay = base * (1 + random.uniform(0.0, 0.25))
    if retry_after is not None:
        delay = max(delay, float(retry_after))
    return delay


def _retry_after(exc: urllib.error.HTTPError) -> int | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    if value and value.strip().isdigit():
        return int(value)
    return None


def api_get(key: str, endpoint: str, params: dict) -> dict:
    global _last_api_call
    url = f"{API_ROOT}/{endpoint}?" + urllib.parse.urlencode(params)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        # 用 header 而不是 ?apikey=：key 不会出现在 URL / 代理日志里。
        "X-API-Key": key,
    }
    last_error: Exception | None = None
    for attempt in range(1, API_ATTEMPTS + 1):
        # Central rate limit: every endpoint (search, w/{id}, collections) shares
        # the same 45/min budget, so the delay belongs here, not in the caller.
        wait = API_DELAY - (time.monotonic() - _last_api_call)
        if wait > 0:
            time.sleep(wait)
        _last_api_call = time.monotonic()
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            if exc.code == 429:
                wait = retry_delay(attempt, _retry_after(exc))
                warn(f"wallhaven 限流（第 {attempt}/{API_ATTEMPTS} 次），{wait:.0f}s 后重试")
                time.sleep(wait)
                last_error = exc
                continue
            if exc.code in (401, 403):
                raise SystemExit(f"wallhaven 拒绝了这个 API key（HTTP {exc.code}）：{body}") from exc
            if 400 <= exc.code < 500:
                # 404/400 重试也不会变好；让调用方决定怎么处理。
                raise RuntimeError(f"wallhaven API 返回 HTTP {exc.code}：{body}") from exc
            last_error = exc
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
            last_error = exc
        if attempt < API_ATTEMPTS:
            time.sleep(retry_delay(attempt))
    raise RuntimeError(f"wallhaven API 请求失败：{last_error}")


class TooLarge(Exception):
    pass


def download(url: str, destination: Path, max_bytes: int = MAX_DOWNLOAD_BYTES) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=120) as response, open(destination, "wb") as handle:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise TooLarge(f"{size} bytes")
            digest.update(chunk)
            handle.write(chunk)
        if size == 0:
            raise RuntimeError("下载到 0 字节，拒绝入池")
        # The caller renames this .part file only after quality checks pass.
        # fsync here keeps a power loss from leaving a truncated "complete" file
        # behind once the rename has happened.
        handle.flush()
        os.fsync(handle.fileno())
    return size, digest.hexdigest()


def download_with_retry(url: str, destination: Path) -> tuple[int, str]:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        retry_after = None
        try:
            return download(url, destination)
        except TooLarge:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise RuntimeError(f"下载被拒绝（HTTP {exc.code}）：{url}") from exc
            retry_after = _retry_after(exc)
            last_error = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as exc:
            last_error = exc
        if attempt < DOWNLOAD_ATTEMPTS:
            wait = retry_delay(attempt, retry_after)
            warn(f"下载失败（第 {attempt}/{DOWNLOAD_ATTEMPTS} 次），{wait:.0f}s 后重试：{url}")
            time.sleep(wait)
    raise RuntimeError(f"下载失败 {url}：{last_error}")


# --------------------------------------------------------------------------- #
# image quality helpers
# --------------------------------------------------------------------------- #


def run(command: list[str], timeout: int = 120) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"命令失败（{result.returncode}）：{' '.join(command)}\n{result.stderr.strip()}")
    return result.stdout


def image_stats(path: Path) -> dict:
    magick = shutil.which("magick")
    if not magick:
        raise RuntimeError("找不到 ImageMagick（magick）")
    gray = run(
        [
            magick,
            f"{path}[0]",
            "-resize",
            "256x256>",
            "-colorspace",
            "Gray",
            "-format",
            "%[fx:mean] %[fx:standard_deviation]",
            "info:",
        ]
    ).split()
    saturation = run(
        [
            magick,
            f"{path}[0]",
            "-resize",
            "256x256>",
            "-colorspace",
            "HSL",
            "-channel",
            "G",
            "-separate",
            "+channel",
            "-format",
            "%[fx:mean]",
            "info:",
        ]
    ).strip()
    return {
        "luma": round(float(gray[0]), 4),
        "stddev": round(float(gray[1]), 4),
        "sat": round(float(saturation), 4),
    }


def _dct_matrix(size: int) -> list[list[float]]:
    return [
        [
            (math.sqrt(1 / size) if u == 0 else math.sqrt(2 / size))
            * math.cos((2 * x + 1) * u * math.pi / (2 * size))
            for x in range(size)
        ]
        for u in range(size)
    ]


_DCT = _dct_matrix(PHASH_SIZE)


def phash_from_gray(data: bytes) -> str | None:
    """16x16 grayscale bytes -> 64-bit fingerprint.

    This is a 16x16 variant of pHash (top-left 8x8 DCT, DC included, upper
    median + strict greater-than).  Thresholds 3/7 are calibrated for this
    variant, not for the classic 32x32 imagehash implementation.
    """
    size = PHASH_SIZE
    if len(data) != size * size:
        return None
    rows = [list(data[i * size : (i + 1) * size]) for i in range(size)]
    # Separable 2D DCT: rows then columns.
    tmp = [
        [sum(_DCT[u][x] * rows[x][y] for x in range(size)) for y in range(size)]
        for u in range(size)
    ]
    coef = [
        [sum(tmp[u][v] * _DCT[w][v] for v in range(size)) for w in range(size)]
        for u in range(size)
    ]
    low = [coef[y][x] for y in range(PHASH_LOW) for x in range(PHASH_LOW)]
    median = sorted(low)[len(low) // 2]
    bits = 0
    for index, value in enumerate(low):
        if value > median:
            bits |= 1 << index
    return f"{bits:016x}"


def image_phash(path: Path) -> str | None:
    magick = shutil.which("magick")
    if not magick:
        raise RuntimeError("找不到 ImageMagick（magick）")
    result = subprocess.run(
        [
            magick,
            f"{path}[0]",
            "-resize",
            f"{PHASH_SIZE}x{PHASH_SIZE}!",
            "-colorspace",
            "Gray",
            "-depth",
            "8",
            "gray:-",
        ],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
    return phash_from_gray(result.stdout)


def phash_distance(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def find_phash_pairs(hashes: dict[str, str], threshold: int) -> list[tuple[int, str, str]]:
    items = sorted(hashes.items())
    pairs: list[tuple[int, str, str]] = []
    for index, (left_id, left_hash) in enumerate(items):
        for right_id, right_hash in items[index + 1 :]:
            distance = phash_distance(left_hash, right_hash)
            if distance <= threshold:
                pairs.append((distance, left_id, right_id))
    pairs.sort()
    return pairs


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def readable_text_colour(hex_value: str) -> str:
    r, g, b = hex_to_rgb(hex_value)
    luma = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
    return "#000000" if luma > 0.5 else "#ffffff"


def palette_info(path: Path) -> dict | None:
    noctalia = shutil.which("noctalia")
    if not noctalia:
        warn("找不到 noctalia，跳过调色板检查")
        return None
    with tempfile.TemporaryDirectory(prefix="wallhaven-palette-") as tmpdir:
        output = Path(tmpdir) / "palette.json"
        try:
            run([noctalia, "theme", str(path), "--scheme", SCHEME, "--dark", "-o", str(output)], timeout=120)
            data = json.loads(output.read_text(encoding="utf-8"))
        except (RuntimeError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            warn(f"调色板生成失败（{path.name}）：{exc}")
            return None
    primary = data.get("primary")
    if not isinstance(primary, str):
        return None
    red, green, blue = hex_to_rgb(primary)
    hue, lightness, saturation = colorsys.rgb_to_hls(red / 255, green / 255, blue / 255)
    return {
        "primary": primary,
        "hue": round(hue * 360) % 360,
        "chroma": round(saturation * 100),
        "lightness": round(lightness * 100),
    }


def hue_buckets(index: dict) -> list[int]:
    counts = [0] * 12
    for entry in index["images"].values():
        palette = entry.get("palette") or {}
        hue = palette.get("hue")
        if isinstance(hue, int):
            counts[hue // 30] += 1
    return counts


def muted_count(index: dict) -> int:
    total = 0
    for entry in index["images"].values():
        stats = entry.get("stats") or {}
        palette = entry.get("palette") or {}
        if stats.get("sat", 1.0) < MIN_SAT or palette.get("chroma", 100) < MIN_CHROMA:
            total += 1
    return total


def judge(
    stats: dict, palette: dict | None, index: dict, target: int, relaxed: bool = False
) -> str | None:
    """Hard filters always apply; hue/muted quotas are soft and skipped in relaxed mode."""
    if stats["luma"] > MAX_LUMA:
        return "too-bright"
    if stats["luma"] < MIN_LUMA:
        return "too-dark"
    if stats["stddev"] < MIN_STDDEV:
        return "too-flat"
    if relaxed:
        return None
    muted = stats["sat"] < MIN_SAT or (palette is not None and palette["chroma"] < MIN_CHROMA)
    if muted:
        allowed = max(1, math.ceil(target * MUTED_SHARE))
        if muted_count(index) >= allowed:
            return "muted-quota"
    if palette is not None:
        bucket = palette["hue"] // 30
        cap = max(6, math.ceil(target / 12 * HUE_CAP_FACTOR))
        if hue_buckets(index)[bucket] >= cap and len(index["images"]) < target * 0.85:
            return "hue-quota"
    return None


def extension_for(item: dict) -> str:
    suffix = Path(urllib.parse.urlparse(item.get("path", "")).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
        return suffix
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }
    return mapping.get(item.get("file_type", ""), ".jpg")


def api_reject_reason(item: dict) -> str | None:
    if item.get("purity") != "sfw":
        return "not-sfw"
    if item.get("category") != "general":
        return "not-general"
    if item.get("file_type") in {"image/gif", "image/avif"}:
        # 动图/AVIF 会拖慢轮换或需要 libheif，池子只用静态常见格式。
        return "unsupported-format"
    if int(item.get("file_size") or 0) > MAX_DOWNLOAD_BYTES:
        return "too-large"
    return None


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #


def _source_tag_list(args: argparse.Namespace) -> list[str]:
    if getattr(args, "no_tags", False):
        return []
    raw = getattr(args, "tags", ",".join(SOURCE_TAGS))
    return [tag.strip().lower() for tag in str(raw).split(",") if tag.strip()]


def build_sources(args: argparse.Namespace) -> list[dict]:
    """Build the search sources for one run, rotated by day.

    The 1M toplist alone has only ~591 usable candidates, so a rolling window
    needs more ranges and tag searches.  Rotating the order keeps every run
    from spending its API budget on the same first pages.
    """
    common = {
        "purity": "100",
        "categories": "100",
        "ratios": "16x9",
        "atleast": "1920x1080",
    }
    min_favorites = int(getattr(args, "min_favorites", 0) or 0)
    min_views = int(getattr(args, "min_views", 0) or 0)
    sources: list[dict] = []
    for toplist_range in TOPLIST_RANGES:
        sources.append(
            {
                "label": f"toplist:{toplist_range}",
                "params": {**common, "sorting": "toplist", "topRange": toplist_range},
                "min_favorites": min_favorites,
                "min_views": min_views,
            }
        )
        for tag in _source_tag_list(args):
            sources.append(
                {
                    "label": f"tag:{tag}:{toplist_range}",
                    "params": {
                        **common,
                        "sorting": "toplist",
                        "topRange": toplist_range,
                        "q": f"+{tag}",
                    },
                    "min_favorites": min_favorites,
                    "min_views": min_views,
                }
            )
    if getattr(args, "include_latest", True):
        sources.append(
            {
                "label": "latest",
                "params": {**common, "sorting": "date_added", "order": "desc"},
                "min_favorites": max(min_favorites, LATEST_MIN_FAVORITES),
                "min_views": max(min_views, LATEST_MIN_VIEWS),
            }
        )
    if sources:
        offset = datetime.now().toordinal() % len(sources)
        sources = sources[offset:] + sources[:offset]
    return sources


def iter_candidates(
    key: str,
    max_pages: int | None = None,
    sources: list[dict] | None = None,
    page_offset: int = 0,
):
    if sources is None:
        sources = build_sources(argparse.Namespace())
    for source in sources:
        page = 1 + max(0, page_offset)
        last_page = page
        while page <= last_page:
            params = dict(source["params"])
            params["page"] = str(page)
            label = source["label"]
            try:
                payload = api_get(key, "search", params)
            except RuntimeError as exc:
                # 一个榜单失败不拖垮后面的范围；下一次 sync 会重新尝试。
                warn(f"{label}: {exc}")
                break
            meta = payload.get("meta") or {}
            last_page = int(meta.get("last_page") or page)
            for item in payload.get("data") or []:
                if int(item.get("favorites") or 0) < source["min_favorites"]:
                    continue
                if int(item.get("views") or 0) < source["min_views"]:
                    continue
                yield item, f"{source['label']}:p{page}"
            if max_pages is not None and page - (1 + max(0, page_offset)) + 1 >= max_pages:
                break
            page += 1


def cleanup_incoming(paths: Paths) -> None:
    cutoff = time.time() - 24 * 3600
    for stale in paths.pool.glob(".incoming-*"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass


def prune_missing(paths: Paths, index: dict) -> list[str]:
    """Drop index entries whose file disappeared, so sync can re-download them."""
    missing = [
        image_id
        for image_id, entry in index["images"].items()
        if not (paths.pool / entry["file"]).exists()
    ]
    for image_id in missing:
        del index["images"][image_id]
    return missing


def blocked_duplicates(index: dict) -> set[str]:
    """Duplicate IDs are only skipped while their original is still in the pool."""
    images = index["images"]
    return {
        image_id
        for image_id, entry in index["duplicates"].items()
        if entry.get("of") in images
    }


def sync_locked(paths: Paths, args: argparse.Namespace) -> int:
    index = load_index(paths)
    target = args.target
    key = read_key(paths)
    dry_run = args.dry_run
    if not dry_run:
        mirror_index_to_db(paths, index)
    blocker_ids, blocker_phashes, blocker_shas = db_blockers(paths)
    sources = build_sources(args)

    if index.get("target") != target:
        index["target"] = target
        if not dry_run:
            save_index(paths, index)

    if args.retry_rejected and index["rejected"]:
        print(f"清除 {len(index['rejected'])} 条拒绝记录，本次会重新评估。", flush=True)
        index["rejected"].clear()
        if not dry_run:
            save_index(paths, index)

    if args.retry_duplicates and index["duplicates"]:
        print(f"清除 {len(index['duplicates'])} 条重复记录，本次会重新评估。", flush=True)
        index["duplicates"].clear()
        if not dry_run:
            save_index(paths, index)

    # 配额类拒绝只是“本轮不合格”，不是永久黑名单：每次同步都重新给它们机会。
    soft = [i for i, e in index["rejected"].items() if e.get("reason") in SOFT_REASONS]
    for image_id in soft:
        del index["rejected"][image_id]
    if soft:
        print(f"重新评估 {len(soft)} 张之前被配额拒绝的图。", flush=True)
        if not dry_run:
            save_index(paths, index)

    if not dry_run:
        paths.pool.mkdir(parents=True, exist_ok=True)
        cleanup_incoming(paths)

    missing = [
        image_id
        for image_id, entry in index["images"].items()
        if not (paths.pool / entry["file"]).exists()
    ]
    if missing:
        total = len(index["images"])
        if total and (len(missing) == total or len(missing) > total // 2) and not args.force_prune:
            raise SystemExit(
                f"索引有 {total} 张，但池 {paths.pool} 里缺失 {len(missing)} 张；"
                "检查挂载或 WALLHAVEN_POOL_DIR，确认后加 --force-prune 再试"
            )
        for image_id in missing:
            del index["images"][image_id]
        print(f"索引里有 {len(missing)} 个文件已丢失，已移除并会重新下载。", flush=True)
        if not dry_run:
            save_index(paths, index)

    if len(index["images"]) >= target:
        print(f"池子已满（{len(index['images'])}/{target}），不再下载。", flush=True)
        return 0

    max_bytes = int(getattr(args, "max_bytes", 0) or 0)
    used_bytes = 0
    for pool_file in pool_images(paths.pool):
        try:
            used_bytes += pool_file.stat().st_size
        except OSError:
            pass
    if max_bytes and used_bytes >= max_bytes:
        print(
            f"池子已达容量上限（{format_bytes(used_bytes)} / {format_bytes(max_bytes)}），不再下载。",
            flush=True,
        )
        return 0

    if not dry_run:
        free = shutil.disk_usage(paths.pool).free
        estimate = (target - len(index["images"])) * 6 * 1024 * 1024
        if free < estimate:
            warn(f"磁盘剩余 {format_bytes(free)}，按每张 6 MiB 估计还需要 {format_bytes(estimate)}")

    hashes = {
        entry.get("sha256"): image_id
        for image_id, entry in index["images"].items()
        if entry.get("sha256")
    }
    phashes = {
        image_id: entry["phash"]
        for image_id, entry in index["images"].items()
        if entry.get("phash")
    }
    dup_blocked = blocked_duplicates(index)
    accepted = 0
    considered = 0
    deferred = 0
    capacity_full = False

    # 第一遍按配额精挑；如果候选被配额拦得太多，第二遍忽略配额补齐。
    for relaxed in ((False,) if dry_run else (False, True)):
        if capacity_full or len(index["images"]) >= target:
            break
        if relaxed:
            if deferred == 0:
                break
            print("进入宽松模式：忽略色相/饱和度配额，继续补齐。", flush=True)

        for item, source in iter_candidates(
            key,
            args.max_pages,
            sources=sources,
            page_offset=int(getattr(args, "page_offset", 0) or 0),
        ):
            if len(index["images"]) >= target:
                break
            image_id = item.get("id")
            if not image_id:
                continue
            if (
                image_id in index["images"]
                or image_id in index["banned"]
                or image_id in index["rejected"]
                or image_id in dup_blocked
                or image_id in index.get("quarantine", {})
                or image_id in blocker_ids
            ):
                continue

            considered += 1
            reason = api_reject_reason(item)
            if reason:
                index["rejected"][image_id] = {"reason": reason, "at": now()}
                if not dry_run:
                    save_index(paths, index)
                continue

            estimated = int(item.get("file_size") or 0)
            if not dry_run and max_bytes and used_bytes + estimated > max_bytes:
                print(
                    f"池子达到容量上限（{format_bytes(used_bytes)} / {format_bytes(max_bytes)}），"
                    "停止下载。",
                    flush=True,
                )
                capacity_full = True
                break

            if dry_run:
                print(
                    f"[dry-run] {image_id} {item.get('resolution')} "
                    f"{int(item.get('file_size') or 0) // 1024} KiB "
                    f"fav={item.get('favorites')} views={item.get('views')} {source}",
                    flush=True,
                )
                if considered >= args.limit:
                    break
                continue

            temporary = paths.pool / f".incoming-{image_id}.part"
            try:
                size, digest = download_with_retry(item["path"], temporary)
            except TooLarge:
                temporary.unlink(missing_ok=True)
                index["rejected"][image_id] = {"reason": "too-large", "at": now()}
                save_index(paths, index)
                continue
            except (RuntimeError, OSError, http.client.HTTPException) as exc:
                temporary.unlink(missing_ok=True)
                warn(f"{image_id} 下载失败：{exc}")
                continue

            duplicate_of = hashes.get(digest)
            if duplicate_of:
                temporary.unlink(missing_ok=True)
                index["duplicates"][image_id] = {"of": duplicate_of, "at": now()}
                save_index(paths, index)
                continue
            archived = blocker_shas.get(digest)
            if archived:
                temporary.unlink(missing_ok=True)
                index["rejected"][image_id] = {
                    "reason": "duplicate-of-archived",
                    "of": archived,
                    "at": now(),
                }
                save_index(paths, index)
                continue

            try:
                stats = image_stats(temporary)
                palette = palette_info(temporary)
            except (
                RuntimeError,
                OSError,
                subprocess.SubprocessError,
                http.client.HTTPException,
            ) as exc:
                temporary.unlink(missing_ok=True)
                warn(f"{image_id} 无法解析：{exc}")
                continue

            try:
                phash = image_phash(temporary)
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                warn(f"{image_id} pHash 失败：{exc}")
                phash = None
            if phash:
                near_duplicate = None
                near_distance = None
                for existing_id, existing_hash in list(phashes.items()) + list(blocker_phashes.items()):
                    distance = phash_distance(phash, existing_hash)
                    if distance <= PHASH_AUTO_DUPLICATE and (
                        near_distance is None or distance < near_distance
                    ):
                        near_duplicate = existing_id
                        near_distance = distance
                if near_duplicate:
                    temporary.unlink(missing_ok=True)
                    if near_duplicate in index["images"]:
                        index["duplicates"][image_id] = {
                            "of": near_duplicate,
                            "distance": near_distance,
                            "at": now(),
                            "phash": phash,
                        }
                    else:
                        index["rejected"][image_id] = {
                            "reason": "near-duplicate-of-archived",
                            "of": near_duplicate,
                            "distance": near_distance,
                            "at": now(),
                        }
                    save_index(paths, index)
                    continue

            reason = judge(stats, palette, index, target, relaxed=relaxed)
            if reason:
                temporary.unlink(missing_ok=True)
                if reason in SOFT_REASONS:
                    deferred += 1
                else:
                    index["rejected"][image_id] = {"reason": reason, "at": now()}
                    save_index(paths, index)
                continue

            final = paths.pool / f"wallhaven-{image_id}{extension_for(item)}"
            os.replace(temporary, final)
            index["images"][image_id] = {
                **entry_metadata(item, source),
                "file": final.name,
                "added_at": now(),
                "bytes": size,
                "sha256": digest,
                "phash": phash,
                "stats": stats,
                "palette": palette,
            }
            hashes[digest] = image_id
            index["duplicates"].pop(image_id, None)
            if phash:
                phashes[image_id] = phash
            accepted += 1
            used_bytes += size
            save_index(paths, index)
            palette_text = (
                f"hue={palette['hue']:3d} chroma={palette['chroma']:3d}"
                if palette
                else "palette=skipped"
            )
            print(
                f"[{len(index['images']):3d}/{target}] {image_id} {item.get('resolution'):>9} "
                f"{stats['luma']:.2f}/{stats['sat']:.2f} {palette_text} {source}",
                flush=True,
            )
            time.sleep(DOWNLOAD_DELAY)
        if capacity_full:
            break

    if not dry_run:
        save_index(paths, index)
        mirror_index_to_db(paths, index)
    print(
        f"完成：池子 {len(index['images'])}/{target} 张，本次新增 {accepted} 张，"
        f"候选 {considered} 个，配额延后 {deferred} 张，永久拒绝 {len(index['rejected'])} 个。",
        flush=True,
    )
    if dry_run:
        print("dry-run：没有写索引、没有下载任何文件。", flush=True)
    return 0


def cmd_sync(paths: Paths, args: argparse.Namespace) -> int:
    # dry-run 也拿锁（共享锁），避免在 sync 写索引时读到半旧快照。
    with exclusive_lock(paths.lock, shared=args.dry_run) as acquired:
        if not acquired:
            print("另一个 wallhaven-sync 正在运行，跳过。", flush=True)
            return 0
        return sync_locked(paths, args)


# --------------------------------------------------------------------------- #
# rolling window: evict -> trash -> replenish
# --------------------------------------------------------------------------- #


def current_wallpaper_ids() -> set[str] | None:
    """IDs currently on screen.  None means unknown and must block eviction."""
    values: list[str] = []
    env_path = os.environ.get("NOCTALIA_WALLPAPER_PATH", "").strip()
    if env_path:
        values.append(env_path)
    noctalia = shutil.which("noctalia")
    if noctalia:
        try:
            output = run([noctalia, "msg", "wallpaper-get"], timeout=20)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            if not values:
                return None
        else:
            values.extend(line.strip() for line in output.splitlines() if line.strip())
    if not values:
        return None
    ids: set[str] = set()
    for value in values:
        image_id = pool_id_from_filename(Path(value).name)
        if image_id:
            ids.add(image_id)
    return ids


def trash_files(paths: Paths) -> list[Path]:
    if not paths.trash.is_dir():
        return []
    return sorted(
        (path for path in paths.trash.rglob("*") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )


def purge_trash(paths: Paths, days: int, dry_run: bool = False) -> list[Path]:
    """Delete trash files older than `days`; remove empty month directories."""
    removed: list[Path] = []
    if days <= 0 or not paths.trash.is_dir():
        return removed
    cutoff = time.time() - days * 86400
    for path in trash_files(paths):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            if dry_run:
                print(f"[dry-run] 将删除 {path} ({format_bytes(path.stat().st_size)})")
            else:
                size = path.stat().st_size
                path.unlink()
                print(f"清理回收站 {path.name}（{format_bytes(size)}）")
            removed.append(path)
        except OSError as exc:
            warn(f"清理 {path} 失败：{exc}")
    if not dry_run:
        for directory in sorted(paths.trash.rglob("*"), reverse=True):
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
    return removed


def select_victims(
    paths: Paths,
    index: dict,
    limit: int,
    protect: set[str],
    include_unshown: bool = False,
    protect_recent_hours: float = 2.0,
) -> list[str]:
    """Least-recently-shown active images, never the current wallpaper or pins.

    Images whose file is already gone are skipped (sync will prune them).
    Never-shown images are only considered with include_unshown: evicting them
    would mean they were never displayed at all.
    """
    if limit <= 0:
        return []
    rows = {row["id"]: row for row in db_eviction_rows(paths)}
    recent = db_recently_shown(paths, protect_recent_hours)
    history_last: dict[str, str] = {}
    if len(rows) < len(index["images"]) or any(
        not row.get("last_shown_at") for row in rows.values()
    ):
        # Partial DB / pre-1.1 history: fill last_shown_at from history.jsonl
        # for rows the hook has not touched since the DB was created.
        for event in read_history(paths):
            if event.get("in_pool") and event.get("id"):
                history_last[str(event["id"])] = str(event.get("at") or "")
        if protect_recent_hours > 0:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=protect_recent_hours)
            ).replace(microsecond=0).isoformat()
            recent |= {
                image_id for image_id, at in history_last.items() if at and at >= cutoff
            }
    shown: list[tuple[str, str]] = []
    unseen: list[tuple[str, str]] = []
    for image_id, entry in index["images"].items():
        if image_id in protect or image_id in recent:
            continue
        row = rows.get(image_id) or {}
        if row.get("pinned"):
            continue
        last_shown = row.get("last_shown_at") or history_last.get(image_id)
        if last_shown:
            shown.append((str(last_shown), image_id))
        elif include_unshown:
            unseen.append((str(entry.get("added_at") or row.get("added_at") or ""), image_id))
    shown.sort()
    unseen.sort()
    ordered = [image_id for _, image_id in shown]
    if include_unshown:
        ordered.extend(image_id for _, image_id in unseen)
    return ordered[:limit]


def evict_batch(
    paths: Paths,
    index: dict,
    victims: list[str],
    args: argparse.Namespace,
    reason: str = "rotation",
) -> list[str]:
    """Move victims to <pool>-trash/YYYYMM and mark them evicted in the DB."""
    if not victims:
        return []
    destination_dir = paths.trash / datetime.now().strftime("%Y%m")
    if args.dry_run:
        for image_id in victims:
            entry = index["images"].get(image_id) or {}
            file = entry.get("file", "?")
            size = int(entry.get("bytes") or 0)
            print(f"[dry-run] 淘汰 {image_id}  {file}  {format_bytes(size)}")
        return []
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warn(f"无法创建回收站 {destination_dir}：{exc}")
        return []
    evicted: list[str] = []
    for image_id in victims:
        entry = index["images"].get(image_id)
        if not entry:
            continue
        source = paths.pool / entry["file"]
        if not source.exists():
            warn(f"淘汰 {image_id} 失败：{source} 不存在")
            continue
        destination = destination_dir / entry["file"]
        try:
            if source.stat().st_dev != destination_dir.stat().st_dev:
                shutil.move(str(source), str(destination))
            else:
                os.replace(source, destination)
        except OSError as exc:
            warn(f"移动 {source} 到回收站失败：{exc}")
            continue
        try:
            # The image may have been in the pool for months; age the trash
            # entry from the eviction, not from the original download.
            os.utime(destination, None)
        except OSError as exc:
            warn(f"更新 {destination} 时间戳失败：{exc}")
        del index["images"][image_id]
        try:
            save_index(paths, index)
        except OSError as exc:
            index["images"][image_id] = entry
            warn(f"保存索引失败，回滚 {image_id}: {exc}")
            try:
                shutil.move(str(destination), str(source))
            except OSError as rollback:
                warn(f"回滚文件失败：{rollback}")
            continue
        db_set_status(paths, image_id, "evicted", reason=reason, trash_path=str(destination))
        evicted.append(image_id)
        print(f"淘汰 {image_id} -> {destination}")
    return evicted


def prune_orphans(paths: Paths, index: dict, min_age_hours: float = 24.0) -> list[str]:
    """Move pool files that the index does not know about into the trash.

    Only used when the hard capacity cap is exceeded; a grace period avoids
    touching a file that was just copied in manually.
    """
    known = {entry.get("file") for entry in index["images"].values()}
    cutoff = time.time() - min_age_hours * 3600
    destination_dir = paths.trash / "orphans" / datetime.now().strftime("%Y%m")
    moved: list[str] = []
    for path in pool_images(paths.pool):
        if path.name in known:
            continue
        try:
            if path.stat().st_mtime > cutoff:
                continue
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / path.name
            if path.stat().st_dev != destination_dir.stat().st_dev:
                shutil.move(str(path), str(destination))
            else:
                os.replace(path, destination)
            os.utime(destination, None)
            moved.append(path.name)
            print(f"容量保护：移走索引外文件 {path.name} -> {destination}", flush=True)
        except OSError as exc:
            warn(f"移走索引外文件 {path} 失败：{exc}")
    return moved


def enforce_capacity(
    paths: Paths,
    index: dict,
    args: argparse.Namespace,
    protect: set[str],
    protection_known: bool = False,
) -> list[str]:
    """Hard cap: active count <= target and pool bytes <= max_bytes.

    This is deliberately independent of the daily quota.  It only kicks in when
    the pool is already over the cap, and it still protects the current
    wallpaper, pins and recently shown images.
    """
    max_bytes = int(getattr(args, "max_bytes", 0) or 0)
    target = int(args.target)
    if max_bytes <= 0 and len(index["images"]) <= target:
        return []
    if not protection_known:
        return []
    removed: list[str] = []
    for _ in range(50):
        count, used = pool_usage(paths.pool)
        over_count = max(0, len(index["images"]) - target)
        over_bytes = max(0, used - max_bytes) if max_bytes else 0
        if over_count <= 0 and over_bytes <= 0 and count <= target:
            break
        limit = min(50, max(over_count, 1 if over_bytes else 0))
        victims = select_victims(
            paths,
            index,
            limit,
            protect,
            include_unshown=True,
            protect_recent_hours=float(getattr(args, "protect_recent_hours", 2.0)),
        )
        if victims:
            batch = evict_batch(paths, index, victims, args, reason="capacity")
            if batch:
                removed.extend(batch)
                print(f"容量保护：额外淘汰 {len(batch)} 张。", flush=True)
                continue
        # No indexed image can be evicted (all pinned/current/recent); try
        # stale orphan files before giving up.
        orphans = prune_orphans(paths, index)
        if not orphans:
            warn(
                f"容量仍超限（{format_bytes(used)} / {format_bytes(max_bytes)}，"
                f"{count} 张），但没有可淘汰的图"
            )
            break
        removed.extend(orphans)
    return removed


def enrich_evicted_tags(paths: Paths, key: str, image_ids: list[str]) -> None:
    """Search results omit tags; fetch each evicted image once (≤24/day)."""
    for image_id in image_ids:
        if not re.fullmatch(r"[a-z0-9]{6}", image_id):
            # capacity enforcement may report orphan file names, not IDs
            continue
        try:
            item = fetch_item(key, image_id)
        except (RuntimeError, SystemExit) as exc:
            warn(f"补全 {image_id} 标签失败：{exc}")
            continue
        tags = [str(tag.get("name")) for tag in item.get("tags") or [] if tag.get("name")]
        if tags:
            db_update_tags(paths, image_id, tags)


def set_index_target(paths: Paths, target: int) -> dict:
    index = load_index(paths)
    if index.get("target") != target:
        index["target"] = target
        save_index(paths, index)
    return index


def cmd_replenish(paths: Paths, args: argparse.Namespace) -> int:
    """Daily rolling window: plan victims, top up, then move victims to trash."""
    with exclusive_lock(paths.lock) as acquired:
        if not acquired:
            print("另一个 wallhaven-sync 正在运行，跳过。", flush=True)
            return 0
        index = load_index(paths)
        if not args.dry_run:
            mirror_index_to_db(paths, index)
            username = os.environ.get("WALLHAVEN_USERNAME", "").strip()
            if username:
                try:
                    collected = fetch_collection_ids(read_key(paths), username)
                except (RuntimeError, SystemExit) as exc:
                    warn(f"同步收藏夹保护失败：{exc}")
                else:
                    pinned = db_pin_ids(paths, collected) if collected else 0
                    if pinned:
                        print(f"收藏夹保护：{pinned} 张在库里，已标记 pin。", flush=True)
        purge_trash(paths, args.keep_trash_days, dry_run=args.dry_run)

        already = db_evicted_today(paths)
        budget = max(0, args.max_per_day - already)
        if args.max_per_day > 0 and already >= args.max_per_day:
            print(f"今天已淘汰 {already}/{args.max_per_day} 张，本次只补货。", flush=True)

        current = current_wallpaper_ids()
        protection_known = current is not None or args.force
        protect: set[str] = current or set()
        planned: list[str] = []
        if budget > 0 and not protection_known:
            warn("无法确定当前壁纸（noctalia msg wallpaper-get 失败），跳过淘汰；确认后加 --force")
            budget = 0
        if budget > 0:
            planned = select_victims(
                paths,
                index,
                budget,
                protect,
                include_unshown=args.evict_unshown,
                protect_recent_hours=args.protect_recent_hours,
            )
        if budget > 0 and not planned:
            print("没有可淘汰的旧图（未展示过的图默认不淘汰）。", flush=True)
        for image_id in planned:
            entry = index["images"].get(image_id) or {}
            print(
                f"{'[dry-run] ' if args.dry_run else ''}候选淘汰 {image_id}  "
                f"{entry.get('file', '?')}",
                flush=True,
            )

        page_offset = args.page_offset
        if page_offset is None or page_offset < 0:
            page_offset = datetime.now().toordinal() % 20
        sync_args = argparse.Namespace(
            target=args.target + len(planned),
            dry_run=args.dry_run,
            retry_rejected=False,
            retry_duplicates=False,
            force_prune=False,
            max_pages=args.max_pages,
            page_offset=page_offset,
            max_bytes=args.max_bytes,
            limit=args.limit,
            tags=args.tags,
            no_tags=args.no_tags,
            include_latest=args.include_latest,
            min_favorites=args.min_favorites,
            min_views=args.min_views,
        )
        try:
            result = sync_locked(paths, sync_args)
        except BaseException:
            if not args.dry_run:
                set_index_target(paths, args.target)
            raise
        if result != 0 or args.dry_run:
            return result

        index = set_index_target(paths, args.target)
        surplus = max(0, len(index["images"]) - args.target)
        removed: list[str] = []
        if surplus > 0 and planned:
            victims = [image_id for image_id in planned if image_id in index["images"]][:surplus]
            removed.extend(evict_batch(paths, index, victims, args))
        removed.extend(
            enforce_capacity(
                paths,
                index,
                args,
                protect,
                protection_known=protection_known,
            )
        )
        if removed:
            ids = [item for item in removed if re.fullmatch(r"[a-z0-9]{6}", item)]
            if ids:
                key = read_key(paths)
                enrich_evicted_tags(paths, key, ids)
            print(
                f"本轮淘汰 {len(removed)} 张，补货后池子 {len(index['images'])} 张。",
                flush=True,
            )
        return 0


def cmd_backfill(paths: Paths, args: argparse.Namespace) -> int:
    """Fill url/width/favorites/tags for entries downloaded before 1.1."""
    index = load_index(paths)
    targets = [
        image_id
        for image_id, entry in index["images"].items()
        if args.force or not entry.get("url") or not entry.get("tags")
    ]
    if args.limit:
        targets = targets[: args.limit]
    if not targets:
        print("所有 active 图的元数据都已完整。")
        return 0
    if args.dry_run:
        print(f"[dry-run] 需要回填 {len(targets)} 张：{' '.join(targets[:20])}")
        return 0
    key = read_key(paths)
    with exclusive_lock(paths.lock) as acquired:
        if not acquired:
            raise SystemExit("另一个 wallhaven-sync 正在运行")
        index = load_index(paths)
        updated = 0
        for image_id in targets:
            entry = index["images"].get(image_id)
            if not entry:
                continue
            try:
                item = fetch_item(key, image_id)
            except (RuntimeError, SystemExit) as exc:
                warn(f"{image_id} 回填失败：{exc}")
                continue
            source = entry.get("source") or "manual"
            patch = entry_metadata(item, source)
            patch["theme"] = entry.get("theme") or patch["theme"]
            patch["tags"] = [
                str(tag.get("name")) for tag in item.get("tags") or [] if tag.get("name")
            ]
            for name, value in patch.items():
                if value is not None:
                    entry[name] = value
            updated += 1
            if updated % 20 == 0:
                save_index(paths, index)
            print(f"[{updated}/{len(targets)}] {image_id} {entry.get('resolution')} tags={len(patch['tags'])}", flush=True)
        save_index(paths, index)
        mirror_index_to_db(paths, index)
    print(f"回填完成：{updated} 张。")
    return 0


def cmd_db_checkpoint(paths: Paths, _args: argparse.Namespace) -> int:
    """Flush the WAL so a plain copy of library.db is self-contained."""
    if not paths.db.exists():
        print("还没有 library.db，无需处理。")
        return 0
    try:
        with open_db(paths) as conn:
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            conn.commit()
    except sqlite3.Error as exc:
        warn(f"checkpoint 失败：{exc}")
        return 1
    print(f"library.db 已 checkpoint：{paths.db}（{tuple(result)}）")
    return 0


def cmd_trash(paths: Paths, args: argparse.Namespace) -> int:
    if args.purge:
        removed = purge_trash(paths, args.days, dry_run=args.dry_run)
        if args.dry_run:
            print(f"[dry-run] 将清理 {len(removed)} 个文件。")
        else:
            print(f"清理完成：删除 {len(removed)} 个文件。")
        return 0
    files = trash_files(paths)
    if not files:
        print(f"回收站是空的（{paths.trash}）。")
        return 0
    total = 0
    for path in files:
        try:
            size = path.stat().st_size
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            continue
        total += size
        print(f"{stamp}  {size:>10}  {path.relative_to(paths.trash)}")
    print(f"合计 {len(files)} 个文件，{format_bytes(total)}；{args.days} 天后自动清理。")


def cmd_pin(paths: Paths, args: argparse.Namespace) -> int:
    with exclusive_lock(paths.lock) as acquired:
        if not acquired:
            raise SystemExit("另一个 wallhaven-sync 正在运行")
        index = load_index(paths)
        image_id = parse_wallpaper_id(args.id) or args.id
        if image_id not in index["images"]:
            row = db_get(paths, image_id)
            if row and row.get("status") != "active":
                raise SystemExit(f"{image_id} 已被淘汰，先执行 wallhaven-sync restore {image_id}")
            raise SystemExit(f"{image_id} 不在池子里")
        mirror_index_to_db(paths, index)
        if not db_set_pinned(paths, image_id, True):
            raise SystemExit(f"无法 pin {image_id}（元数据库里没有记录）")
        print(f"已 pin {image_id}（永不自动淘汰）")
    return 0


def cmd_unpin(paths: Paths, args: argparse.Namespace) -> int:
    image_id = parse_wallpaper_id(args.id) or args.id
    if not db_set_pinned(paths, image_id, False):
        raise SystemExit(f"元数据库里没有 {image_id}")
    print(f"已取消 pin {image_id}")
    return 0


def cmd_pins(paths: Paths, _args: argparse.Namespace) -> int:
    if not paths.db.exists():
        print("还没有元数据库记录。")
        return 0
    with open_db(paths) as conn:
        rows = conn.execute(
            "SELECT id, status, file, last_shown_at FROM images WHERE pinned = 1 ORDER BY id"
        ).fetchall()
    if not rows:
        print("没有 pin 的图。")
        return 0
    for row in rows:
        print(f"{row['id']}  {row['status']:<9} {row['file'] or '?':<28} 最近展示 {row['last_shown_at'] or '从未'}")
    return 0


def db_row_to_entry(row: dict) -> dict:
    stats = {}
    for key in ("luma", "stddev", "sat"):
        if row.get(key) is not None:
            stats[key] = row[key]
    palette = None
    if row.get("primary_color"):
        palette = {
            "primary": row["primary_color"],
            "hue": row.get("hue"),
            "chroma": row.get("chroma"),
        }
    tags = None
    if row.get("tags"):
        try:
            tags = json.loads(row["tags"])
        except json.JSONDecodeError:
            tags = None
    return {
        "file": row.get("file"),
        "source": row.get("source") or "restored",
        "theme": row.get("theme") or "restored",
        "width": row.get("width"),
        "height": row.get("height"),
        "resolution": row.get("resolution"),
        "bytes": row.get("bytes"),
        "sha256": row.get("sha256"),
        "phash": row.get("phash"),
        "stats": stats,
        "palette": palette,
        "file_type": row.get("file_type"),
        "favorites": row.get("favorites"),
        "views": row.get("views"),
        "category": row.get("category"),
        "purity": row.get("purity"),
        "created_at": row.get("created_at"),
        "url": row.get("url"),
        "thumb_url": row.get("thumb_url"),
        "tags": tags,
        "added_at": row.get("added_at") or now(),
    }


def cmd_restore(paths: Paths, args: argparse.Namespace) -> int:
    image_id = parse_wallpaper_id(args.id) or args.id
    with exclusive_lock(paths.lock) as acquired:
        if not acquired:
            raise SystemExit("另一个 wallhaven-sync 正在运行")
        index = load_index(paths)
        if image_id in index["images"]:
            print(f"{image_id} 已经在池子里。")
            return 0
        row = db_get(paths, image_id)
        if row and row.get("status") == "banned":
            raise SystemExit(f"{image_id} 已被拉黑；先手动编辑索引里的 banned 再恢复")
        if row and row.get("status") == "quarantine":
            raise SystemExit(
                f"{image_id} 在隔离区；用 wallhaven-sync dedupe --restore {image_id}"
            )
        if row and row.get("file") and (paths.pool / row["file"]).exists():
            # The file was put back manually; just re-attach the metadata.
            entry = db_row_to_entry(row)
            index["images"][image_id] = entry
            save_index(paths, index)
            db_set_status(paths, image_id, "active")
            print(f"{image_id} 的文件已在池中，已重新挂到索引。")
            return 0
        source = Path(row["trash_path"]) if row and row.get("trash_path") else None
        if source and source.exists():
            paths.pool.mkdir(parents=True, exist_ok=True)
            destination = paths.pool / (row.get("file") or source.name)
            try:
                if source.stat().st_dev != paths.pool.stat().st_dev:
                    shutil.move(str(source), str(destination))
                else:
                    os.replace(source, destination)
            except OSError as exc:
                raise SystemExit(f"从回收站恢复 {source} 失败：{exc}") from exc
            entry = db_row_to_entry(row)
            entry["file"] = destination.name
            index["images"][image_id] = entry
            save_index(paths, index)
            db_set_status(paths, image_id, "active")
            print(f"已从回收站恢复 {image_id} -> {destination}")
            return 0
        key = read_key(paths)
        item = fetch_item(key, image_id)
        if not add_item(
            paths, index, item, "restored", args.target, force=True, allow_archived=True
        ):
            raise SystemExit(f"重新下载 {image_id} 失败")
        db_set_status(paths, image_id, "active")
        print(f"已重新下载 {image_id}")
    return 0


def fetch_collection_ids(key: str, username: str) -> set[str]:
    """Read every item in the user's collections (API is read-only)."""
    collected: set[str] = set()
    listing = api_get(key, f"collections/{username}", {})
    for collection in listing.get("data") or []:
        collection_id = collection.get("id")
        if collection_id is None:
            continue
        page = 1
        last_page = 1
        while page <= last_page:
            try:
                payload = api_get(
                    key,
                    f"collections/{username}/{collection_id}",
                    {"page": str(page)},
                )
            except RuntimeError as exc:
                warn(f"读取收藏夹 {collection_id} 第 {page} 页失败：{exc}")
                break
            meta = payload.get("meta") or {}
            last_page = int(meta.get("last_page") or page)
            for item in payload.get("data") or []:
                if item.get("id"):
                    collected.add(str(item["id"]))
            page += 1
    return collected


def cmd_collections(paths: Paths, args: argparse.Namespace) -> int:
    """Pin every wallpaper in the user's wallhaven collections (read-only).

    Wallhaven's API has no favourites endpoint (404); collections are the only
    readable list.  Item listings need the account username, which the API does
    not expose, so it must come from --username or WALLHAVEN_USERNAME.
    """
    username = args.username or os.environ.get("WALLHAVEN_USERNAME", "").strip()
    if not username:
        raise SystemExit(
            "需要 wallhaven 用户名（API 不返回账号名）："
            "--username <名字> 或设置 WALLHAVEN_USERNAME"
        )
    key = read_key(paths)
    try:
        collected = fetch_collection_ids(key, username)
    except RuntimeError as exc:
        raise SystemExit(f"读取 {username} 的收藏夹失败：{exc}") from exc
    if not collected:
        print("收藏夹是空的，或用户名不对。")
        return 0
    with exclusive_lock(paths.lock) as acquired:
        if not acquired:
            raise SystemExit("另一个 wallhaven-sync 正在运行")
        index = load_index(paths)
        mirror_index_to_db(paths, index)
        pinned = db_pin_ids(paths, collected)
    print(f"收藏夹共 {len(collected)} 张，其中 {pinned} 张在库里，已标记 pin。")
    return 0


# --------------------------------------------------------------------------- #
# status / doctor
# --------------------------------------------------------------------------- #


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif"}


def pool_images(pool: Path) -> list[Path]:
    if not pool.is_dir():
        return []
    return [p for p in pool.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]


def pool_usage(pool: Path) -> tuple[int, int]:
    """(image count, bytes) of everything currently in the pool directory."""
    files = pool_images(pool)
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return len(files), total


def format_bytes(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def pool_id_from_filename(name: str) -> str | None:
    match = re.match(r"wallhaven-([a-z0-9]{6})\.[a-z0-9]+$", name, re.IGNORECASE)
    return match.group(1).lower() if match else None


def append_history(paths: Paths, event: dict) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n"
    with open(paths.history, "a", encoding="utf-8") as handle:
        # Never block a Noctalia hook on another writer; O_APPEND keeps the
        # line atomic and flock is only a best-effort serialisation.
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # NFS and some FUSE filesystems reject flock; O_APPEND is still safe
            # for a single small line.
            pass
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass


def read_history(paths: Paths) -> list[dict]:
    try:
        lines = paths.history.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    events: list[dict] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def shuffle_state_path() -> Path:
    state_home = _xdg("XDG_STATE_HOME", ".local/state")
    return state_home / "noctalia" / "wallpaper_shuffle.json"


def rotation_progress(pool: Path) -> list[tuple[str, str, int, int]]:
    path = shuffle_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    results: list[tuple[str, str, int, int]] = []
    pool_str = str(pool)
    for scope, sources in (data.get("scopes") or {}).items():
        if not isinstance(sources, dict):
            continue
        for source, seen in sources.items():
            if not str(source).startswith(pool_str):
                continue
            total = len(pool_images(pool))
            results.append((scope, source, len(seen or []), total))
    return results


def cmd_status(paths: Paths, args: argparse.Namespace) -> int:
    index = load_index(paths)
    images = index["images"]
    target = index.get("target") or _target_default()
    files = pool_images(paths.pool)
    disk = 0
    for path in files:
        try:
            disk += path.stat().st_size
        except OSError:
            pass
    if images and not files:
        warn(f"索引有 {len(images)} 张，但池目录 {paths.pool} 没有图片；检查 WALLHAVEN_POOL_DIR")
    events = read_history(paths)
    pool_events = [e for e in events if e.get("in_pool") and e.get("id")]
    shown = {str(e["id"]) for e in pool_events} & set(images)
    phash_ready = sum(1 for entry in images.values() if entry.get("phash"))
    library = db_counts(paths)
    evicted_today = db_evicted_today(paths)
    max_bytes = _int_env("WALLHAVEN_MAX_BYTES", DEFAULT_MAX_BYTES)
    trash = trash_files(paths)
    trash_bytes = 0
    for path in trash:
        try:
            trash_bytes += path.stat().st_size
        except OSError:
            pass

    if args.json:
        payload = {
            "pool": str(paths.pool),
            "count": len(images),
            "files": len(files),
            "target": target,
            "bytes": disk,
            "max_bytes": max_bytes,
            "hue_buckets": hue_buckets(index),
            "rejected": len(index["rejected"]),
            "duplicates": len(index["duplicates"]),
            "banned": len(index["banned"]),
            "quarantine": len(index.get("quarantine", {})),
            "phash": phash_ready,
            "shown": len(shown),
            "history_events": len(pool_events),
            "library": library,
            "trash": {"files": len(trash), "bytes": trash_bytes},
            "evicted_today": evicted_today,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"池子      {paths.pool}")
    print(f"数量      {len(images)}/{target} 张（磁盘 {format_bytes(disk)}，文件 {len(files)}）")
    print(
        f"容量      {format_bytes(disk)} / {format_bytes(max_bytes)}"
        f"{'  ⚠ 超限' if max_bytes and disk > max_bytes else ''}"
    )
    print(f"拒绝      {len(index['rejected'])} 个    重复 {len(index['duplicates'])} 个    拉黑 {len(index['banned'])} 个")
    print(f"指纹      pHash {phash_ready}/{len(images)} 张    隔离 {len(index.get('quarantine', {}))} 张")
    print(f"展示      已看过 {len(shown)} 张 / 从未 {max(0, len(images) - len(shown))} 张（历史 {len(pool_events)} 条）")
    print(
        f"库        已淘汰 {library.get('evicted', 0)} 张    pin {library.get('pinned', 0)} 张"
        f"    今日淘汰 {evicted_today} 张"
    )
    print(f"回收站    {len(trash)} 个文件 / {format_bytes(trash_bytes)}（{paths.trash}）")

    buckets = hue_buckets(index)
    print("色相分布  " + " ".join(f"{i * 30:3d}°:{count:2d}" for i, count in enumerate(buckets)))

    reasons: dict[str, int] = {}
    for entry in index["rejected"].values():
        reason = entry.get("reason", "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    if reasons:
        print("拒绝原因  " + "  ".join(f"{k}={v}" for k, v in sorted(reasons.items())))

    progress = rotation_progress(paths.pool)
    if progress:
        for scope, _source, seen, total in progress:
            print(f"轮换进度  {scope}: {seen}/{total}（本轮已看过）")
    else:
        print("轮换进度  尚未开始（Noctalia 轮换一次后才会生成 shuffle 状态）")
    return 0


def _target_default() -> int:
    try:
        return int(os.environ.get("WALLHAVEN_TARGET", DEFAULT_TARGET))
    except ValueError:
        return DEFAULT_TARGET


def recent_template_failures(minutes: int = 15) -> list[str]:
    log = Path.home() / ".cache/noctalia/noctalia.log"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-800:]
    except FileNotFoundError:
        return []
    cutoff = datetime.now().timestamp() - minutes * 60
    failures: list[str] = []
    for line in lines:
        match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if not match:
            continue
        try:
            stamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            continue
        if stamp < cutoff:
            continue
        if "hook failed" in line or "failed to apply" in line:
            failures.append(line.strip())
    return failures


def cmd_doctor(paths: Paths, _args: argparse.Namespace) -> int:
    problems = 0
    warnings = 0

    def ok(message: str) -> None:
        print(f"  ok    {message}")

    def bad(message: str) -> None:
        nonlocal problems
        problems += 1
        print(f"  FAIL  {message}")

    def warn_check(message: str) -> None:
        nonlocal warnings
        warnings += 1
        print(f"  warn  {message}")

    print("key")
    if paths.key_file.exists():
        mode = paths.key_file.stat().st_mode & 0o777
        if mode & 0o077:
            bad(f"{paths.key_file} 权限是 {oct(mode)}，应为 600")
        else:
            ok(f"{paths.key_file}（600）")
        try:
            key = read_key(paths)
            payload = api_get(key, "search", {"purity": "100", "page": "1"})
            if payload.get("data"):
                ok("wallhaven API 可以正常访问")
            else:
                warn_check("wallhaven API 返回了空列表，检查 key 是否被限制")
        except SystemExit as exc:
            bad(str(exc))
        except Exception as exc:  # noqa: BLE001 - doctor should keep going
            bad(f"API 检查失败：{exc}")
    else:
        bad(f"缺少 {paths.key_file}")

    print("pool")
    if paths.pool.is_dir():
        ok(f"{paths.pool}")
        if not os.access(paths.pool, os.W_OK):
            bad("pool 目录不可写")
    else:
        warn_check(f"{paths.pool} 还不存在（首次 sync 会创建）")
    try:
        index = load_index(paths)
        ok(f"索引 {len(index['images'])} 张，拒绝 {len(index['rejected'])} 个")
        missing = [entry["file"] for entry in index["images"].values() if not (paths.pool / entry["file"]).exists()]
        if missing:
            bad(f"索引里有 {len(missing)} 个文件已丢失（例如 {missing[0]}）")
        known = {entry["file"] for entry in index["images"].values()}
        orphans = [p.name for p in pool_images(paths.pool) if p.name not in known]
        if orphans:
            warn_check(f"pool 里有 {len(orphans)} 个索引外的文件（例如 {orphans[0]}）")
    except SystemExit as exc:
        bad(str(exc))

    print("library")
    if paths.db.exists():
        try:
            with open_db(paths) as conn:
                check = conn.execute("PRAGMA quick_check").fetchone()[0]
            counts = db_counts(paths)
            if check == "ok":
                ok(
                    f"{paths.db}（active {counts.get('active', 0)} / "
                    f"evicted {counts.get('evicted', 0)} / pin {counts.get('pinned', 0)}）"
                )
            else:
                bad(f"元数据库完整性检查失败：{check}")
        except sqlite3.Error as exc:
            bad(f"元数据库无法打开：{exc}")
    else:
        warn_check("还没有 library.db（首次 sync/replenish 后生成）")
    if paths.trash.is_dir():
        files = trash_files(paths)
        size = 0
        for path in files:
            try:
                size += path.stat().st_size
            except OSError:
                pass
        ok(f"回收站 {len(files)} 个文件 / {format_bytes(size)}（{paths.trash}）")
    else:
        ok("回收站还不存在（还没有淘汰过）")
    if shutil.which("noctalia"):
        try:
            hooks = run(["noctalia", "msg", "config-get", "hooks"], timeout=20)
            if "wallpaper_changed" in hooks and "wallhaven-sync record" in hooks:
                ok("wallpaper_changed 钩子已接到 wallhaven-sync record")
            else:
                bad("wallpaper_changed 钩子没有指向 wallhaven-sync record")
        except Exception as exc:  # noqa: BLE001
            warn_check(f"无法读取 hooks 配置：{exc}")

    print("tools")
    for binary in ("magick", "noctalia"):
        if shutil.which(binary):
            ok(binary)
        else:
            warn_check(f"找不到 {binary}，对应的检查会被跳过")

    print("noctalia")
    if shutil.which("noctalia"):
        try:
            wallpaper = run(["noctalia", "msg", "config-get", "wallpaper"], timeout=20)
            match = re.search(r"^directory\s*=\s*'([^']*)'", wallpaper, re.MULTILINE)
            if match and match.group(1):
                if Path(match.group(1)) == paths.pool:
                    ok(f"wallpaper.directory = {match.group(1)}")
                else:
                    bad(f"wallpaper.directory = {match.group(1)}（池子是 {paths.pool}）")
            else:
                bad("wallpaper.directory 为空（Noctalia 不会从池子轮换）")
            automation = re.search(
                r"\[automation\](.*?)(?:\n\[|\Z)", wallpaper, re.DOTALL
            )
            if automation:
                block = automation.group(1)
                if re.search(r"^enabled\s*=\s*true", block, re.MULTILINE):
                    ok("wallpaper.automation 已开启")
                else:
                    bad("wallpaper.automation 没有开启")
                interval = re.search(r"^interval_seconds\s*=\s*(\d+)", block, re.MULTILINE)
                if interval:
                    ok(f"轮换间隔 {int(interval.group(1)) // 60} 分钟")
            templates = run(["noctalia", "msg", "config-get", "theme.templates"], timeout=20)
            if "umbriel" in templates:
                ok("模板已启用（含 umbriel）")
            else:
                bad("模板里没看到 umbriel，边框不会跟着变色")
        except Exception as exc:  # noqa: BLE001
            warn_check(f"无法读取 Noctalia 运行状态：{exc}")
        state = shuffle_state_path()
        if state.exists():
            ok(f"shuffle 状态 {state}")
        else:
            warn_check("shuffle 状态尚未生成（轮换一次后才有）")

    print("files")
    checks = {
        "kitty include": (Path.home() / ".config/kitty/kitty.conf", "themes/noctalia.conf"),
        "gtk3 import": (Path.home() / ".config/gtk-3.0/gtk.css", "noctalia.css"),
        "gtk4 import": (Path.home() / ".config/gtk-4.0/gtk.css", "noctalia.css"),
        "umbriel include": (Path.home() / ".config/umbriel/config.toml", "noctalia.toml"),
    }
    for label, (path, needle) in checks.items():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            bad(f"{label}: 缺少 {path}（nixos-rebuild switch 后再检查）")
            continue
        if needle in text:
            ok(f"{label}")
        else:
            bad(f"{label}: {path} 里没有 {needle}")

    print("umbriel")
    if shutil.which("umbriel"):
        try:
            value = run(["umbriel", "config", "get", "colors.accent_primary"], timeout=20).strip()
            generated = Path.home() / ".config/umbriel/noctalia.toml"
            expected = None
            try:
                generated_text = generated.read_text(encoding="utf-8", errors="replace")
                match = re.search(r'^accent_primary\s*=\s*"([^"]+)"', generated_text, re.MULTILINE)
                expected = match.group(1) if match else None
            except FileNotFoundError:
                pass
            if value and expected:
                if value.lower() == expected.lower():
                    ok(f"运行中的 Umbriel 配色已生效（accent_primary={value}）")
                else:
                    bad(
                        f"Umbriel 没重载新配色（运行中 {value} / noctalia.toml {expected}）；"
                        "执行 umbriel msg config-reload 或重启 Umbriel"
                    )
            elif value:
                ok(f"运行中的 Umbriel 已读到配色（accent_primary={value}）")
            else:
                warn_check("umbriel config get 返回空值")
        except RuntimeError:
            warn_check("Umbriel 没在运行，无法确认 config-reload 是否生效")
    else:
        warn_check("找不到 umbriel 命令")

    print("recent log")
    failures = recent_template_failures()
    if failures:
        warn_check(f"最近 15 分钟有 {len(failures)} 条模板/钩子失败，例如：{failures[0]}")
    else:
        ok("最近 15 分钟没有模板/钩子失败")

    print("overrides")
    settings = Path.home() / ".local/state/noctalia/settings.toml"
    if settings.exists():
        text = settings.read_text(encoding="utf-8", errors="replace")
        overrides = [key for key in ("[wallpaper]", "[theme.templates]", "[hooks]") if key in text]
        if overrides:
            warn_check(f"settings.toml 里有覆盖段 {', '.join(overrides)}（优先级高于 config.toml）")
        else:
            ok("settings.toml 没有覆盖 wallpaper/templates/hooks")
    else:
        ok("没有 settings.toml 覆盖层")

    print()
    print(f"结果：{problems} 个问题，{warnings} 个警告")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# ban / add / preview
# --------------------------------------------------------------------------- #


def cmd_ban(paths: Paths, args: argparse.Namespace) -> int:
    with exclusive_lock(paths.lock) as acquired:
        if not acquired:
            raise SystemExit("另一个 wallhaven-sync 正在运行")
        index = load_index(paths)
        image_id = args.id
        current = current_wallpaper_ids()
        if current and image_id in current and not args.force:
            raise SystemExit(
                f"{image_id} 正在显示；先换一张壁纸，或加 --force 强制删除"
            )
        entry = index["images"].pop(image_id, None)
        index["banned"][image_id] = {"at": now(), "reason": args.reason or "manual"}
        if entry:
            (paths.pool / entry["file"]).unlink(missing_ok=True)
            print(f"已删除 {entry['file']}")
        else:
            print(f"{image_id} 不在池子里，只加入黑名单")
        save_index(paths, index)
        db_set_status(paths, image_id, "banned", reason=args.reason or "manual")
        mirror_index_to_db(paths, index)
    return 0


def fetch_item(key: str, image_id: str) -> dict:
    try:
        payload = api_get(key, f"w/{image_id}", {})
    except RuntimeError as exc:
        raise SystemExit(f"wallhaven 查不到 {image_id}：{exc}") from exc
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SystemExit(f"wallhaven 查不到 {image_id}")
    return data


def parse_wallpaper_id(raw: str) -> str | None:
    raw = raw.strip()
    patterns = (
        r"/w/([a-z0-9]{6})",           # https://wallhaven.cc/w/94x34k
        r"wallhaven-([a-z0-9]{6})",    # wallhaven-94x34k.jpg
        r"^([a-z0-9]{6})$",            # 裸 id
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            return match.group(1).lower()
    return None


def add_item(
    paths: Paths,
    index: dict,
    item: dict,
    source: str,
    target: int,
    force: bool = False,
    allow_archived: bool = False,
    max_bytes: int = 0,
) -> bool:
    image_id = item["id"]
    duplicate = index["duplicates"].get(image_id)
    if (
        image_id in index["images"]
        or image_id in index["banned"]
        or (duplicate and duplicate.get("of") in index["images"])
        or image_id in index.get("quarantine", {})
    ):
        print(f"{image_id} 已在池中、被拉黑、重复或隔离")
        return False
    blocker_ids: set[str] = set()
    blocker_phashes: dict[str, str] = {}
    blocker_shas: dict[str, str] = {}
    if not allow_archived:
        blocker_ids, blocker_phashes, blocker_shas = db_blockers(paths)
        if image_id in blocker_ids:
            print(f"{image_id} 已被淘汰或拉黑（restore 可恢复）")
            return False
    reason = api_reject_reason(item)
    if reason:
        print(f"{image_id} 未通过初筛：{reason}")
        return False
    temporary = paths.pool / f".incoming-{image_id}.part"
    try:
        size, digest = download_with_retry(item["path"], temporary)
        stats = image_stats(temporary)
        palette = palette_info(temporary)
    except (RuntimeError, TooLarge, OSError, subprocess.SubprocessError, http.client.HTTPException) as exc:
        temporary.unlink(missing_ok=True)
        print(f"{image_id} 失败：{exc}")
        return False
    for existing_id, entry in index["images"].items():
        if entry.get("sha256") == digest:
            temporary.unlink(missing_ok=True)
            index["duplicates"][image_id] = {"of": existing_id, "at": now()}
            save_index(paths, index)
            print(f"{image_id} 与 {existing_id} 内容相同，跳过")
            return False
    if archive_id := blocker_shas.get(digest):
        temporary.unlink(missing_ok=True)
        index["rejected"][image_id] = {
            "reason": "duplicate-of-archived",
            "of": archive_id,
            "at": now(),
        }
        save_index(paths, index)
        print(f"{image_id} 与已淘汰的 {archive_id} 内容相同，跳过")
        return False
    try:
        phash = image_phash(temporary)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        warn(f"{image_id} pHash 失败：{exc}")
        phash = None
    if phash:
        near_duplicate = None
        near_distance = None
        active_hashes = {
            existing_id: entry["phash"]
            for existing_id, entry in index["images"].items()
            if entry.get("phash")
        }
        for existing_id, existing_hash in list(active_hashes.items()) + list(blocker_phashes.items()):
            distance = phash_distance(phash, existing_hash)
            if distance <= PHASH_AUTO_DUPLICATE and (
                near_distance is None or distance < near_distance
            ):
                near_duplicate = existing_id
                near_distance = distance
        if near_duplicate:
            temporary.unlink(missing_ok=True)
            if near_duplicate in index["images"]:
                index["duplicates"][image_id] = {
                    "of": near_duplicate,
                    "distance": near_distance,
                    "at": now(),
                    "phash": phash,
                }
            else:
                index["rejected"][image_id] = {
                    "reason": "near-duplicate-of-archived",
                    "of": near_duplicate,
                    "distance": near_distance,
                    "at": now(),
                }
            save_index(paths, index)
            print(f"{image_id} 与 {near_duplicate} 视觉重复（距离 {near_distance}），跳过")
            return False
    reason = judge(stats, palette, index, target)
    if reason and not force:
        temporary.unlink(missing_ok=True)
        index["rejected"][image_id] = {"reason": reason, "at": now()}
        save_index(paths, index)
        print(f"{image_id} 未通过品控：{reason}（--force 可强制加入）")
        return False
    if max_bytes:
        _, used = pool_usage(paths.pool)
        if used + size > max_bytes:
            temporary.unlink(missing_ok=True)
            print(
                f"{image_id} 会超过池子容量上限"
                f"（{format_bytes(used)} + {format_bytes(size)} > {format_bytes(max_bytes)}）"
            )
            return False
    final = paths.pool / f"wallhaven-{image_id}{extension_for(item)}"
    os.replace(temporary, final)
    index["images"][image_id] = {
        **entry_metadata(item, source),
        "file": final.name,
        "added_at": now(),
        "bytes": size,
        "sha256": digest,
        "phash": phash,
        "stats": stats,
        "palette": palette,
    }
    index["duplicates"].pop(image_id, None)
    save_index(paths, index)
    mirror_index_to_db(paths, index)
    print(f"已加入 {final.name}")
    return True


def cmd_add(paths: Paths, args: argparse.Namespace) -> int:
    key = read_key(paths)
    image_id = parse_wallpaper_id(args.id_or_url)
    if not image_id:
        raise SystemExit(f"无法从 {args.id_or_url} 里解析 wallpaper id")
    item = fetch_item(key, image_id)
    paths.pool.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(paths.lock) as acquired:
        if not acquired:
            raise SystemExit("另一个 wallhaven-sync 正在运行")
        index = load_index(paths)
        add_item(
            paths,
            index,
            item,
            "manual",
            args.target,
            force=args.force,
            max_bytes=args.max_bytes,
        )
    return 0


def cmd_preview(paths: Paths, args: argparse.Namespace) -> int:
    index = load_index(paths)
    target = Path(args.path_or_id)
    if not target.exists():
        entry = index["images"].get(args.path_or_id)
        if not entry:
            raise SystemExit(f"找不到文件或 id：{args.path_or_id}")
        target = paths.pool / entry["file"]
    stats = image_stats(target)
    palette = palette_info(target)
    print(f"文件      {target}")
    print(f"亮度      {stats['luma']:.3f}（上限 {MAX_LUMA}）   层次(σ) {stats['stddev']:.3f}   饱和度 {stats['sat']:.3f}")
    if not palette:
        print("调色板    不可用（缺少 noctalia）")
        return 0
    print(f"主色      {palette['primary']}   hue={palette['hue']}°  chroma={palette['chroma']}")

    noctalia = shutil.which("noctalia")
    if noctalia:
        with tempfile.TemporaryDirectory(prefix="wallhaven-preview-") as tmpdir:
            output = Path(tmpdir) / "palette.json"
            try:
                run([noctalia, "theme", str(target), "--scheme", SCHEME, "--dark", "-o", str(output)], timeout=120)
                data = json.loads(output.read_text(encoding="utf-8"))
            except (RuntimeError, json.JSONDecodeError):
                data = {}
        roles = [
            "surface",
            "surface_container",
            "on_surface",
            "primary",
            "on_primary",
            "secondary",
            "tertiary",
            "error",
        ]
        for role in roles:
            value = data.get(role)
            if not isinstance(value, str):
                continue
            foreground = readable_text_colour(value)
            print(f"  \033[48;2;{int(value[1:3], 16)};{int(value[3:5], 16)};{int(value[5:7], 16)}m"
                  f"\033[38;2;{int(foreground[1:3], 16)};{int(foreground[3:5], 16)};{int(foreground[5:7], 16)}m"
                  f"  {role:<18} {value}  \033[0m")
    return 0


def cmd_record(paths: Paths, _args: argparse.Namespace) -> int:
    raw = os.environ.get("NOCTALIA_WALLPAPER_PATH", "").strip()
    if not raw:
        return 0
    path = Path(raw)
    connector = os.environ.get("NOCTALIA_WALLPAPER_CONNECTOR", "").strip() or None
    try:
        in_pool = path.resolve().parent == paths.pool.resolve()
    except (OSError, RuntimeError):
        in_pool = False
    image_id = pool_id_from_filename(path.name)
    if not in_pool and image_id:
        warn(f"history: {path} 不在池 {paths.pool}，按外部壁纸记录")
    try:
        append_history(
            paths,
            {
                "at": now(),
                "path": str(path),
                "pool": str(paths.pool),
                "id": image_id,
                "connector": connector,
                "in_pool": in_pool,
                "source": "noctalia",
            },
        )
    except (OSError, UnicodeError) as exc:
        # A hook must never fail the wallpaper change; log and move on.
        warn(f"history 写入失败：{exc}")
    if in_pool and image_id:
        # The DB is best-effort too; db_record_display swallows sqlite errors.
        db_record_display(paths, image_id, connector, str(path))
    return 0


def cmd_history(paths: Paths, args: argparse.Namespace) -> int:
    events = read_history(paths)
    selected = events[-args.limit :] if args.limit > 0 else []
    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
        return 0
    if not selected:
        print("还没有历史记录。确认 Noctalia 的 wallpaper_changed 钩子已启用。")
        return 0
    for event in selected:
        image_id = event.get("id") or "-"
        connector = event.get("connector") or "-"
        print(f"{event.get('at', '')}  {image_id:<8}  {connector:<8}  {event.get('path', '')}")
    return 0


def cmd_report(paths: Paths, _args: argparse.Namespace) -> int:
    index = load_index(paths)
    events = read_history(paths)
    raw_events = [e for e in events if e.get("in_pool") and e.get("id")]
    # Noctalia also fires wallpaper_changed on config reload / repeated apply;
    # count only consecutive path changes as real displays.
    pool_events: list[dict] = []
    last_path = None
    for event in raw_events:
        path = event.get("path")
        if path == last_path:
            # Noctalia also fires on config reload / repeated apply; the same
            # path in a row is not a new display.
            continue
        pool_events.append(event)
        last_path = path
    counts = Counter(str(e["id"]) for e in pool_events)
    shown = set(counts)
    never = set(index["images"]) - shown
    files = pool_images(paths.pool)
    disk = 0
    for path in files:
        try:
            disk += path.stat().st_size
        except OSError:
            pass
    if index["images"] and not files:
        warn(f"索引有 {len(index['images'])} 张，但池目录 {paths.pool} 没有图片；检查 WALLHAVEN_POOL_DIR")
    phash_ready = sum(1 for entry in index["images"].values() if entry.get("phash"))

    print(f"池子      {len(index['images'])} 张（磁盘 {format_bytes(disk)}，文件 {len(files)}）")
    print(f"指纹      pHash {phash_ready}/{len(index['images'])} 张")
    print(
        f"展示      已看过 {len(shown)} 张，从未展示 {len(never)} 张，"
        f"历史事件 {len(pool_events)} 条（原始 {len(raw_events)} 条）"
    )
    if pool_events:
        print(f"最近一次  {pool_events[-1].get('at', '')}  {pool_events[-1].get('id')}  ({pool_events[-1].get('connector') or '-'})")
    if counts:
        print("最常展示  " + "  ".join(f"{image_id}:{count}" for image_id, count in counts.most_common(8)))
    if never:
        sample = sorted(never)[:8]
        print(f"未展示样例 {', '.join(sample)}")

    by_source = Counter(entry.get("source", "?") for entry in index["images"].values())
    sources = by_source.most_common(8)
    suffix = " ..." if len(by_source) > len(sources) else ""
    print("来源      " + "  ".join(f"{source}:{count}" for source, count in sources) + suffix)
    by_theme = Counter(entry.get("theme") or "?" for entry in index["images"].values())
    print("题材      " + "  ".join(f"{theme}:{count}" for theme, count in by_theme.most_common(12)))
    buckets = hue_buckets(index)
    print("色相      " + " ".join(f"{i * 30:3d}°:{count:2d}" for i, count in enumerate(buckets)))
    library = db_counts(paths)
    trash = trash_files(paths)
    trash_bytes = 0
    for path in trash:
        try:
            trash_bytes += path.stat().st_size
        except OSError:
            pass
    print(
        f"库        已淘汰 {library.get('evicted', 0)} 张    "
        f"pin {library.get('pinned', 0)} 张    今日淘汰 {db_evicted_today(paths)} 张"
    )
    print(f"回收站    {len(trash)} 个文件 / {format_bytes(trash_bytes)}")
    for row in db_recent_evictions(paths, 5):
        print(
            f"  最近淘汰 {row.get('evicted_at') or '?'}  {row.get('id')}  "
            f"展示 {row.get('shown_count', 0)} 次  收藏 {row.get('favorites') or 0}"
        )
    if index.get("quarantine"):
        print(f"隔离区    {len(index['quarantine'])} 张（{paths.quarantine}）")
    if index["duplicates"]:
        blocked = blocked_duplicates(index)
        stale = set(index["duplicates"]) - blocked
        print(f"内容重复  {len(blocked)} 个 ID 被跳过（另有 {len(stale)} 条已失效，会重新评估）")
        for image_id, entry in list(index["duplicates"].items())[:8]:
            distance = entry.get("distance")
            if distance is None:
                distance = "?" if entry.get("phash") else "SHA256"
            state = "" if image_id in blocked else "（失效）"
            print(
                f"  {image_id} -> {entry.get('of', '?')}"
                f"（距离 {distance}）{state}"
            )
    return 0


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_entry_from_file(path: Path, source: str = "restored") -> dict:
    try:
        stats = image_stats(path)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        stats = {}
    try:
        palette = palette_info(path)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        palette = None
    try:
        phash = image_phash(path)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        phash = None
    return {
        "file": path.name,
        "source": source,
        "added_at": now(),
        "resolution": None,
        "bytes": path.stat().st_size,
        "sha256": file_sha256(path),
        "phash": phash,
        "stats": stats,
        "palette": palette,
    }


def cmd_dedupe(paths: Paths, args: argparse.Namespace) -> int:
    with exclusive_lock(paths.lock) as acquired:
        if not acquired:
            raise SystemExit("另一个 wallhaven-sync 正在运行")
        index = load_index(paths)

        if args.list_quarantine:
            entries = index.get("quarantine", {})
            if not entries:
                print("隔离区是空的。")
                return 0
            for image_id, entry in sorted(entries.items()):
                print(
                    f"{image_id}  file={entry.get('file', '?')}  "
                    f"of={entry.get('of', '?')}  distance={entry.get('distance', '?')}"
                )
            return 0

        if args.restore:
            image_id = args.restore
            entry = index.get("quarantine", {}).get(image_id)
            if not entry:
                raise SystemExit(f"{image_id} 不在隔离区")
            source = paths.quarantine / entry["file"]
            if not source.exists():
                raise SystemExit(f"隔离文件不存在：{source}")
            destination = paths.pool / entry["file"]
            if destination.exists():
                raise SystemExit(f"池子里已有同名文件：{destination}")
            restored = entry.get("entry")
            if restored:
                restored = dict(restored)
            else:
                # Rebuild metadata *before* moving, so a corrupt/unreadable
                # quarantine file cannot leave the pool and index out of sync.
                try:
                    restored = build_entry_from_file(source, source="restored")
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    raise SystemExit(f"重建 {image_id} 元数据失败：{exc}") from exc
            paths.pool.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(source), str(destination))
            except OSError as exc:
                raise SystemExit(f"移回 {source} 失败：{exc}") from exc
            restored["file"] = destination.name
            index["images"][image_id] = restored
            del index["quarantine"][image_id]
            index["duplicates"].pop(image_id, None)
            try:
                save_index(paths, index)
            except OSError as exc:
                warn(f"保存索引失败，回滚恢复 {image_id}: {exc}")
                try:
                    shutil.move(str(destination), str(source))
                except OSError as rollback:
                    warn(f"回滚文件失败：{rollback}")
                raise SystemExit(1) from exc
            db_set_status(paths, image_id, "active")
            print(f"已恢复 {image_id} -> {destination}")
            return 0

        if args.auto > args.threshold:
            raise SystemExit("--auto 不能大于 --threshold")

        computed = 0
        for image_id, entry in index["images"].items():
            if entry.get("phash"):
                continue
            file = paths.pool / entry["file"]
            if not file.exists():
                continue
            try:
                phash = image_phash(file)
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                warn(f"{image_id} pHash 失败：{exc}")
                continue
            if phash:
                entry["phash"] = phash
                computed += 1
            else:
                warn(f"{image_id} pHash 输出长度异常，跳过")
        hashes = {
            image_id: entry["phash"]
            for image_id, entry in index["images"].items()
            if entry.get("phash")
        }
        pairs = find_phash_pairs(hashes, args.threshold)
        definite = [pair for pair in pairs if pair[0] <= args.auto]
        suspect = [pair for pair in pairs if args.auto < pair[0] <= args.threshold]

        print(f"计算了 {computed} 个 pHash；距离 ≤{args.threshold} 的共 {len(pairs)} 对")
        for distance, left, right in definite:
            print(f"  {distance:2d}  {left}  {right}  -> 重复（--apply 时隔离）")
        for distance, left, right in suspect:
            print(f"  {distance:2d}  {left}  {right}  -> 待确认（不自动处理）")

        moved = 0
        if args.apply:
            current = current_wallpaper_ids() or set()
            quarantined = set(index.get("quarantine", {}))
            for distance, left, right in definite:
                if left in quarantined or right in quarantined:
                    continue
                if left not in index["images"] or right not in index["images"]:
                    continue
                left_entry = index["images"][left]
                right_entry = index["images"][right]
                if left_entry["file"] == right_entry["file"]:
                    warn(f"{left}/{right} 指向同一个文件 {left_entry['file']}，跳过")
                    continue
                left_file = paths.pool / left_entry["file"]
                right_file = paths.pool / right_entry["file"]
                left_exists = left_file.exists()
                right_exists = right_file.exists()
                if not left_exists and not right_exists:
                    warn(f"{left}/{right} 的文件都不存在，跳过")
                    continue
                if left in current or right in current:
                    if left in current and right in current:
                        warn(f"{left}/{right} 都在显示，跳过")
                        continue
                    keep, drop = (left, right) if left in current else (right, left)
                elif left_exists != right_exists:
                    # Never quarantine the only surviving file.
                    keep, drop = (left, right) if left_exists else (right, left)
                else:
                    keep, drop = (
                        (left, right)
                        if int(left_entry.get("bytes") or 0) >= int(right_entry.get("bytes") or 0)
                        else (right, left)
                    )
                keep_file = paths.pool / index["images"][keep]["file"]
                if not keep_file.exists():
                    warn(f"要保留的 {keep} 文件不存在，跳过 {left}/{right}")
                    continue
                original_entry = index["images"][drop]
                source = paths.pool / original_entry["file"]
                destination = paths.quarantine / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if source.stat().st_dev != destination.parent.stat().st_dev:
                        shutil.move(str(source), str(destination))
                    else:
                        os.replace(source, destination)
                except OSError as exc:
                    warn(f"移动 {source} 失败：{exc}")
                    if destination.exists() and source.exists():
                        try:
                            destination.unlink()
                        except OSError:
                            pass
                    continue
                index["quarantine"][drop] = {
                    "file": destination.name,
                    "of": keep,
                    "distance": distance,
                    "at": now(),
                    "entry": original_entry,
                }
                del index["images"][drop]
                try:
                    save_index(paths, index)
                except OSError as exc:
                    warn(f"保存索引失败，回滚 {drop}: {exc}")
                    index["images"][drop] = original_entry
                    del index["quarantine"][drop]
                    try:
                        if destination.exists() and not source.exists():
                            shutil.move(str(destination), str(source))
                    except OSError as rollback:
                        warn(f"回滚文件失败：{rollback}")
                    continue
                db_set_status(
                    paths,
                    drop,
                    "quarantine",
                    reason=f"pHash {distance} of {keep}",
                    trash_path=str(destination),
                )
                quarantined.add(drop)
                moved += 1
                print(f"  隔离 {drop}（保留 {keep}）-> {destination}")

        if args.apply or computed:
            save_index(paths, index)
        if args.apply:
            print(f"完成：隔离 {moved} 张，池子剩 {len(index['images'])} 张")
        else:
            print("dry-run：没有移动文件；加 --apply 只隔离距离 ≤%d 的重复" % args.auto)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr, flush=True)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tags",
        default=",".join(SOURCE_TAGS),
        help="逗号分隔的标签检索（默认八类题材）",
    )
    parser.add_argument("--no-tags", action="store_true", help="不按标签检索")
    parser.add_argument(
        "--include-latest",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="包含 Latest（需要收藏数/浏览数门槛）",
    )
    parser.add_argument("--min-favorites", type=int, default=0, help="候选最低收藏数")
    parser.add_argument("--min-views", type=int, default=0, help="候选最低浏览数")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wallhaven-sync", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="补齐壁纸池")
    sync.add_argument("--target", type=int, default=_target_default(), help="目标张数（默认 365）")
    sync.add_argument("--dry-run", action="store_true", help="只列出候选，不下载")
    sync.add_argument("--limit", type=int, default=20, help="dry-run 时最多列出多少个候选")
    sync.add_argument("--max-pages", type=int, default=None, help="每个来源最多翻多少页")
    sync.add_argument("--page-offset", type=int, default=0, help="每个来源跳过前 N 页（默认 0）")
    sync.add_argument(
        "--max-bytes",
        type=int,
        default=_int_env("WALLHAVEN_MAX_BYTES", DEFAULT_MAX_BYTES),
        help=f"池子字节上限（默认 {DEFAULT_MAX_BYTES // 1024**3} GiB，0 = 不限制）",
    )
    sync.add_argument("--retry-rejected", action="store_true", help="清空拒绝记录后重新评估")
    sync.add_argument("--retry-duplicates", action="store_true", help="清空重复记录后重新评估")
    sync.add_argument("--force-prune", action="store_true", help="池子大量文件缺失时仍继续（危险）")
    add_source_args(sync)
    sync.set_defaults(func=cmd_sync)

    replenish = sub.add_parser("replenish", help="每日滚动窗口：淘汰旧图 + 补新图")
    replenish.add_argument("--target", type=int, default=_target_default(), help="目标张数（默认 365）")
    replenish.add_argument(
        "--max-per-day",
        type=int,
        default=_int_env("WALLHAVEN_MAX_PER_DAY", DEFAULT_MAX_PER_DAY),
        help="每天最多淘汰多少张（默认 24）",
    )
    replenish.add_argument(
        "--keep-trash-days",
        type=int,
        default=_int_env("WALLHAVEN_TRASH_DAYS", DEFAULT_TRASH_DAYS),
        help="回收站保留天数（默认 7）",
    )
    replenish.add_argument(
        "--protect-recent-hours",
        type=float,
        default=2.0,
        help="最近多少小时内展示过的图不淘汰（默认 2）",
    )
    replenish.add_argument(
        "--evict-unshown",
        action="store_true",
        help="允许淘汰从未展示过的图（默认不允许）",
    )
    replenish.add_argument("--force", action="store_true", help="无法确认当前壁纸时仍淘汰（危险）")
    replenish.add_argument("--dry-run", action="store_true", help="只看计划，不移动/下载")
    replenish.add_argument("--limit", type=int, default=10, help="dry-run 时最多列出多少候选")
    replenish.add_argument("--max-pages", type=int, default=2, help="每个来源最多翻多少页")
    replenish.add_argument(
        "--page-offset",
        type=int,
        default=-1,
        help="每个来源跳过前 N 页；-1 按日期轮换（默认）",
    )
    replenish.add_argument(
        "--max-bytes",
        type=int,
        default=_int_env("WALLHAVEN_MAX_BYTES", DEFAULT_MAX_BYTES),
        help=f"池子字节硬上限（默认 {DEFAULT_MAX_BYTES // 1024**3} GiB，0 = 不限制）",
    )
    add_source_args(replenish)
    replenish.set_defaults(func=cmd_replenish)

    status = sub.add_parser("status", help="查看池子与轮换进度")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="自检 key / 池子 / Noctalia / Umbriel")
    doctor.set_defaults(func=cmd_doctor)

    backfill = sub.add_parser("backfill", help="回填旧索引缺少的 URL/收藏数/标签")
    backfill.add_argument("--limit", type=int, default=0, help="最多处理多少张（0 = 全部）")
    backfill.add_argument("--force", action="store_true", help="已有字段也重新拉取")
    backfill.add_argument("--dry-run", action="store_true")
    backfill.set_defaults(func=cmd_backfill)

    ban = sub.add_parser("ban", help="拉黑并删除一张壁纸")
    ban.add_argument("id")
    ban.add_argument("--reason", default=None)
    ban.add_argument("--force", action="store_true", help="即使正在显示也删除")
    ban.set_defaults(func=cmd_ban)

    pin = sub.add_parser("pin", help="保护一张图，永不自动淘汰")
    pin.add_argument("id")
    pin.set_defaults(func=cmd_pin)

    unpin = sub.add_parser("unpin", help="取消 pin")
    unpin.add_argument("id")
    unpin.set_defaults(func=cmd_unpin)

    pins = sub.add_parser("pins", help="列出 pin 的图")
    pins.set_defaults(func=cmd_pins)

    restore = sub.add_parser("restore", help="从回收站或 Wallhaven 恢复一张已淘汰的图")
    restore.add_argument("id")
    restore.add_argument("--target", type=int, default=_target_default())
    restore.set_defaults(func=cmd_restore)

    trash = sub.add_parser("trash", help="查看或清理淘汰回收站")
    trash.add_argument("--list", action="store_true", help="列出文件（默认行为）")
    trash.add_argument("--purge", action="store_true", help="按天数清理")
    trash.add_argument("--days", type=int, default=_int_env("WALLHAVEN_TRASH_DAYS", DEFAULT_TRASH_DAYS))
    trash.add_argument("--dry-run", action="store_true")
    trash.set_defaults(func=cmd_trash)

    checkpoint = sub.add_parser("db-checkpoint", help="合并 library.db 的 WAL（备份前用）")
    checkpoint.set_defaults(func=cmd_db_checkpoint)

    collections = sub.add_parser("collections", help="把 Wallhaven 收藏夹里的图标记为 pin（只读）")
    collections.add_argument("--username", default=None, help="Wallhaven 用户名（API 不返回）")
    collections.set_defaults(func=cmd_collections)

    add = sub.add_parser("add", help="手动加入一张壁纸")
    add.add_argument("id_or_url")
    add.add_argument("--target", type=int, default=_target_default())
    add.add_argument("--force", action="store_true", help="跳过品控强制加入")
    add.add_argument(
        "--max-bytes",
        type=int,
        default=_int_env("WALLHAVEN_MAX_BYTES", DEFAULT_MAX_BYTES),
        help="池子字节上限（0 = 不限制）",
    )
    add.set_defaults(func=cmd_add)

    preview = sub.add_parser("preview", help="预览某张壁纸的配色")
    preview.add_argument("path_or_id")
    preview.set_defaults(func=cmd_preview)

    record = sub.add_parser("record", help="记录 Noctalia wallpaper_changed（给钩子用）")
    record.set_defaults(func=cmd_record)

    history = sub.add_parser("history", help="查看展示历史")
    history.add_argument("--limit", type=int, default=30)
    history.add_argument("--json", action="store_true")
    history.set_defaults(func=cmd_history)

    report = sub.add_parser("report", help="图库统计报告")
    report.set_defaults(func=cmd_report)

    dedupe = sub.add_parser("dedupe", help="pHash 去重（默认只报告）")
    action = dedupe.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="隔离距离 ≤ --auto 的重复")
    action.add_argument("--list-quarantine", action="store_true", help="列出隔离区并退出")
    action.add_argument("--restore", metavar="ID", help="把隔离区的一张图移回池子并重建索引")
    dedupe.add_argument("--threshold", type=int, default=PHASH_SUSPECT, help="报告阈值（默认 7）")
    dedupe.add_argument("--auto", type=int, default=PHASH_AUTO_DUPLICATE, help="自动隔离阈值（默认 3）")
    dedupe.set_defaults(func=cmd_dedupe)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.resolve()
    try:
        return args.func(paths, args)
    except KeyboardInterrupt:
        print("\n中断；已写入的索引会保留。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
