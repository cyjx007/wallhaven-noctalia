from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

from .config import Paths, _xdg, load_index, save_index

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


def set_index_target(paths: Paths, target: int) -> dict:
    index = load_index(paths)
    if index.get("target") != target:
        index["target"] = target
        save_index(paths, index)
    return index


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
