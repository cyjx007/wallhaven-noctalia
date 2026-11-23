from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr, flush=True)


def local_time(value: object) -> str:
    """Render a stored UTC timestamp in the user's local timezone.

    Everything persisted (index, library.db, history.jsonl) stays in UTC with an
    explicit offset; this is only for human-facing output. Unparsable values are
    returned as-is so legacy rows never crash a listing.
    """
    if not value:
        return ""
    text = str(value)
    try:
        return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return text


def format_bytes(size: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def parse_bytes(value: str) -> int:
    """argparse type for values like 512M, 1.5GiB, 3G, 3221225472."""
    text = value.strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmgt]?i?b?)", text)
    if not match:
        raise argparse.ArgumentTypeError(f"无法解析容量：{value}（示例：512M、3G、1.5GiB）")
    factors = {
        "": 1,
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "tib": 1024**4,
    }
    return int(float(match.group(1)) * factors[match.group(2)])


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default
