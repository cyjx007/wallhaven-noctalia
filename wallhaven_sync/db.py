from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DB_VERSION, Paths, now
from .util import warn

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
