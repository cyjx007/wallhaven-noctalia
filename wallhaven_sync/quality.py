from __future__ import annotations

import colorsys
import json
import math
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from .config import (
    HUE_CAP_FACTOR,
    MAX_DOWNLOAD_BYTES,
    MAX_LUMA,
    MIN_CHROMA,
    MIN_LUMA,
    MIN_SAT,
    MIN_STDDEV,
    MUTED_SHARE,
    PHASH_LOW,
    PHASH_SIZE,
    SCHEME,
)
from .util import warn


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


_DCT = _dct_matrix(PHASH_SIZE)
