from __future__ import annotations

import argparse
import http.client
import os
import shutil
import subprocess
import time
from pathlib import Path

from .api import TooLarge, download_with_retry, read_key
from .config import (
    DOWNLOAD_DELAY,
    PHASH_AUTO_DUPLICATE,
    SOFT_REASONS,
    Paths,
    load_index,
    now,
    save_index,
)
from .db import db_blockers, entry_metadata, mirror_index_to_db
from .quality import (
    api_reject_reason,
    extension_for,
    image_phash,
    image_stats,
    judge,
    palette_info,
    phash_distance,
)
from .sources import build_sources, iter_candidates
from .state import (
    blocked_duplicates,
    cleanup_incoming,
    file_sha256,
    pool_images,
    pool_usage,
)
from .util import format_bytes, warn


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
