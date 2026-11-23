from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from .api import (
    TooLarge,
    api_get,
    download_with_retry,
    fetch_collection_ids,
    fetch_item,
    read_key,
)
from .config import (
    DEFAULT_MAX_BYTES,
    MAX_LUMA,
    SCHEME,
    Paths,
    _target_default,
    exclusive_lock,
    load_index,
    now,
    save_index,
)
from .db import (
    db_counts,
    db_evicted_today,
    db_get,
    db_pin_ids,
    db_recent_evictions,
    db_record_display,
    db_row_to_entry,
    db_set_pinned,
    db_set_status,
    entry_metadata,
    mirror_index_to_db,
    open_db,
)
from .quality import (
    extension_for,
    find_phash_pairs,
    hue_buckets,
    image_phash,
    image_stats,
    palette_info,
    readable_text_colour,
    run,
)
from .rolling import current_wallpaper_ids, trash_files
from .state import (
    append_history,
    blocked_duplicates,
    cleanup_incoming,
    pool_id_from_filename,
    pool_images,
    pool_usage,
    read_history,
    rotation_progress,
    shuffle_state_path,
)
from .sync import add_item, build_entry_from_file, sync_locked
from .util import _int_env, format_bytes, local_time, warn


def cmd_sync(paths: Paths, args: argparse.Namespace) -> int:
    # dry-run 也拿锁（共享锁），避免在 sync 写索引时读到半旧快照。
    with exclusive_lock(paths.lock, shared=args.dry_run) as acquired:
        if not acquired:
            print("另一个 wallhaven-sync 正在运行，跳过。", flush=True)
            return 0
        return sync_locked(paths, args)


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
            "index": str(paths.index),
            "db": str(paths.db),
            "history": str(paths.history),
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
    print(f"状态      {paths.state}（index.json / library.db / history.jsonl）")
    print(f"数量      {len(images)}/{target} 张（磁盘 {format_bytes(disk)}，文件 {len(files)}）")
    cap_text = format_bytes(max_bytes) if max_bytes else "不限"
    print(
        f"容量      {format_bytes(disk)} / {cap_text}"
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
    count, used = pool_usage(paths.pool)
    max_bytes = _int_env("WALLHAVEN_MAX_BYTES", DEFAULT_MAX_BYTES)
    cap_text = format_bytes(max_bytes) if max_bytes else "不限"
    if max_bytes and used > max_bytes:
        warn_check(
            f"池子超过容量上限（{format_bytes(used)} / {cap_text}，"
            f"{count} 张）；跑一次 replenish 或调大 maxBytes"
        )
    else:
        ok(f"容量 {format_bytes(used)} / {cap_text}（{count} 张）")
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
        shown = local_time(row["last_shown_at"]) or "从未"
        print(f"{row['id']}  {row['status']:<9} {row['file'] or '?':<28} 最近展示 {shown}")
    return 0


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
            max_bytes = int(getattr(args, "max_bytes", 0) or 0)
            force = bool(getattr(args, "force", False))
            try:
                restore_bytes = source.stat().st_size
            except OSError:
                restore_bytes = 0
            _, used = pool_usage(paths.pool)
            if max_bytes and not force and used + restore_bytes > max_bytes:
                raise SystemExit(
                    f"恢复会超过容量上限（{format_bytes(used)} + {format_bytes(restore_bytes)} "
                    f"> {format_bytes(max_bytes)}）；加 --force 强制恢复"
                )
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
            paths,
            index,
            item,
            "restored",
            args.target,
            force=True,
            allow_archived=True,
            max_bytes=0 if getattr(args, "force", False) else getattr(args, "max_bytes", 0),
        ):
            raise SystemExit(f"重新下载 {image_id} 失败")
        db_set_status(paths, image_id, "active")
        print(f"已重新下载 {image_id}")
    return 0


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


def cmd_rehydrate(paths: Paths, args: argparse.Namespace) -> int:
    """Re-download active images by ID, e.g. after restoring state on a new machine.

    Unlike `sync`, this keeps the existing pool (same IDs, same pins); it does
    not run quality gates and does not delete anything.
    """
    index = load_index(paths)
    missing = [
        image_id
        for image_id, entry in index["images"].items()
        if not (paths.pool / entry["file"]).exists()
    ]
    if args.limit:
        missing = missing[: args.limit]
    if not missing:
        print("所有 active 文件都在池子里，不需要 rehydrate。")
        return 0
    if args.dry_run:
        print(f"[dry-run] 需要重新下载 {len(missing)} 张：{' '.join(missing[:20])}")
        return 0
    key = read_key(paths)
    with exclusive_lock(paths.lock) as acquired:
        if not acquired:
            raise SystemExit("另一个 wallhaven-sync 正在运行")
        index = load_index(paths)
        paths.pool.mkdir(parents=True, exist_ok=True)
        cleanup_incoming(paths)
        updated = 0
        for image_id in missing:
            entry = index["images"].get(image_id)
            if not entry or (paths.pool / entry["file"]).exists():
                continue
            try:
                item = fetch_item(key, image_id)
            except (RuntimeError, SystemExit) as exc:
                warn(f"{image_id} 无法查询：{exc}")
                continue
            temporary = paths.pool / f".incoming-{image_id}.part"
            try:
                size, digest = download_with_retry(item["path"], temporary)
            except (RuntimeError, TooLarge, OSError, http.client.HTTPException) as exc:
                temporary.unlink(missing_ok=True)
                warn(f"{image_id} 下载失败：{exc}")
                continue
            old_sha = entry.get("sha256")
            if old_sha and old_sha != digest:
                warn(f"{image_id} 的 SHA-256 与旧索引不同（网站文件可能变了）")
            final = paths.pool / f"wallhaven-{image_id}{extension_for(item)}"
            os.replace(temporary, final)
            meta = entry_metadata(item, entry.get("source") or "restored")
            meta["theme"] = entry.get("theme") or meta["theme"]
            meta["tags"] = entry.get("tags")
            for name, value in meta.items():
                if value is not None:
                    entry[name] = value
            entry.update({"file": final.name, "bytes": size, "sha256": digest})
            if not entry.get("phash"):
                try:
                    entry["phash"] = image_phash(final)
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    pass
            if not entry.get("stats"):
                try:
                    entry["stats"] = image_stats(final)
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    pass
            updated += 1
            if updated % 10 == 0:
                save_index(paths, index)
            print(f"[{updated}/{len(missing)}] {image_id} {final.name}", flush=True)
        save_index(paths, index)
        mirror_index_to_db(paths, index)
    print(f"rehydrate 完成：{updated} 张。")
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
        # Local clock for humans; --json keeps the raw UTC offset for scripts.
        stamp = local_time(event.get("at")) or "-"
        print(f"{stamp:<19}  {image_id:<8}  {connector:<8}  {event.get('path', '')}")
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
        last_at = local_time(pool_events[-1].get("at")) or "-"
        print(f"最近一次  {last_at}  {pool_events[-1].get('id')}  ({pool_events[-1].get('connector') or '-'})")
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
        evicted = local_time(row.get("evicted_at")) or "?"
        print(
            f"  最近淘汰 {evicted}  {row.get('id')}  "
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
            max_bytes = _int_env("WALLHAVEN_MAX_BYTES", DEFAULT_MAX_BYTES)
            if max_bytes and not getattr(args, "force", False):
                _, used = pool_usage(paths.pool)
                if used + source.stat().st_size > max_bytes:
                    raise SystemExit(
                        f"恢复会超过容量上限（{format_bytes(used)} + "
                        f"{format_bytes(source.stat().st_size)} > {format_bytes(max_bytes)}）；"
                        "加 --force 强制恢复"
                    )
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
