from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import fetch_collection_ids, fetch_item, read_key
from .config import Paths, exclusive_lock, load_index, save_index
from .db import (
    db_evicted_today,
    db_eviction_rows,
    db_pin_ids,
    db_recently_shown,
    db_set_status,
    db_update_tags,
    mirror_index_to_db,
)
from .quality import run
from .state import (
    pool_id_from_filename,
    pool_images,
    pool_usage,
    read_history,
    set_index_target,
)
from .sync import sync_locked
from .util import format_bytes, warn


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
        # Prefer removing stale files that are not part of the managed pool
        # before evicting images that are still in rotation.
        if over_bytes > 0 or count > target:
            orphans = prune_orphans(paths, index)
            if orphans:
                removed.extend(orphans)
                continue
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
        warn(
            f"容量仍超限（{format_bytes(used)} / {format_bytes(max_bytes)}，"
            f"{count} 张），但没有可淘汰的图"
        )
        break
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
