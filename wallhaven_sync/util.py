from __future__ import annotations

import argparse
import os
import re
import sys


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr, flush=True)


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
