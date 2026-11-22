from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import (
    API_ATTEMPTS,
    API_DELAY,
    API_ROOT,
    BACKOFF_BASE,
    BACKOFF_CAP,
    DOWNLOAD_ATTEMPTS,
    MAX_DOWNLOAD_BYTES,
    USER_AGENT,
    Paths,
)
from .util import warn

_last_api_call = 0.0


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


def fetch_item(key: str, image_id: str) -> dict:
    try:
        payload = api_get(key, f"w/{image_id}", {})
    except RuntimeError as exc:
        raise SystemExit(f"wallhaven 查不到 {image_id}：{exc}") from exc
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SystemExit(f"wallhaven 查不到 {image_id}")
    return data


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
