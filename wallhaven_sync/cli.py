"""wallhaven-sync - maintain a de-duplicated wallhaven pool for Noctalia.

The pool is a plain directory of image files.  Noctalia owns everything after
that: wallpaper rotation, Material You palette generation and app templates.
This tool decides which images enter the pool, when they are rotated out and
keeps the metadata in SQLite.
"""

from __future__ import annotations

import argparse
import sys

from .commands import (
    cmd_add,
    cmd_backfill,
    cmd_ban,
    cmd_collections,
    cmd_db_checkpoint,
    cmd_dedupe,
    cmd_doctor,
    cmd_history,
    cmd_pin,
    cmd_pins,
    cmd_preview,
    cmd_record,
    cmd_rehydrate,
    cmd_report,
    cmd_restore,
    cmd_status,
    cmd_sync,
    cmd_unpin,
)
from .config import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_PER_DAY,
    DEFAULT_TRASH_DAYS,
    PHASH_AUTO_DUPLICATE,
    PHASH_SUSPECT,
    SOURCE_TAGS,
    Paths,
    _target_default,
)
from .rolling import cmd_replenish, cmd_trash
from .util import _int_env, parse_bytes


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
        type=parse_bytes,
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
        type=parse_bytes,
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

    rehydrate = sub.add_parser("rehydrate", help="换机/池子丢失：按索引 ID 重新下载 active 图")
    rehydrate.add_argument("--limit", type=int, default=0, help="最多处理多少张（0 = 全部）")
    rehydrate.add_argument("--dry-run", action="store_true")
    rehydrate.set_defaults(func=cmd_rehydrate)

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
    restore.add_argument(
        "--max-bytes",
        type=parse_bytes,
        default=_int_env("WALLHAVEN_MAX_BYTES", DEFAULT_MAX_BYTES),
        help="容量上限；超限时拒绝恢复",
    )
    restore.add_argument("--force", action="store_true", help="超过容量也恢复")
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
        type=parse_bytes,
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
    dedupe.add_argument("--force", action="store_true", help="恢复隔离图时忽略容量上限")
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
