from __future__ import annotations

import argparse
from datetime import datetime

from .api import api_get
from .config import LATEST_MIN_FAVORITES, LATEST_MIN_VIEWS, SOURCE_TAGS, TOPLIST_RANGES
from .util import warn


def _source_tag_list(args: argparse.Namespace) -> list[str]:
    if getattr(args, "no_tags", False):
        return []
    raw = getattr(args, "tags", ",".join(SOURCE_TAGS))
    return [tag.strip().lower() for tag in str(raw).split(",") if tag.strip()]


def build_sources(args: argparse.Namespace) -> list[dict]:
    """Build the search sources for one run, rotated by day.

    The 1M toplist alone has only ~591 usable candidates, so a rolling window
    needs more ranges and tag searches.  Rotating the order keeps every run
    from spending its API budget on the same first pages.
    """
    common = {
        "purity": "100",
        "categories": "100",
        "ratios": "16x9",
        "atleast": "1920x1080",
    }
    min_favorites = int(getattr(args, "min_favorites", 0) or 0)
    min_views = int(getattr(args, "min_views", 0) or 0)
    sources: list[dict] = []
    for toplist_range in TOPLIST_RANGES:
        sources.append(
            {
                "label": f"toplist:{toplist_range}",
                "params": {**common, "sorting": "toplist", "topRange": toplist_range},
                "min_favorites": min_favorites,
                "min_views": min_views,
            }
        )
        for tag in _source_tag_list(args):
            sources.append(
                {
                    "label": f"tag:{tag}:{toplist_range}",
                    "params": {
                        **common,
                        "sorting": "toplist",
                        "topRange": toplist_range,
                        "q": f"+{tag}",
                    },
                    "min_favorites": min_favorites,
                    "min_views": min_views,
                }
            )
    if getattr(args, "include_latest", True):
        sources.append(
            {
                "label": "latest",
                "params": {**common, "sorting": "date_added", "order": "desc"},
                "min_favorites": max(min_favorites, LATEST_MIN_FAVORITES),
                "min_views": max(min_views, LATEST_MIN_VIEWS),
            }
        )
    if sources:
        offset = datetime.now().toordinal() % len(sources)
        sources = sources[offset:] + sources[:offset]
    return sources


def iter_candidates(
    key: str,
    max_pages: int | None = None,
    sources: list[dict] | None = None,
    page_offset: int = 0,
):
    if sources is None:
        sources = build_sources(argparse.Namespace())
    for source in sources:
        page = 1 + max(0, page_offset)
        last_page = page
        while page <= last_page:
            params = dict(source["params"])
            params["page"] = str(page)
            label = source["label"]
            try:
                payload = api_get(key, "search", params)
            except RuntimeError as exc:
                # 一个榜单失败不拖垮后面的范围；下一次 sync 会重新尝试。
                warn(f"{label}: {exc}")
                break
            meta = payload.get("meta") or {}
            last_page = int(meta.get("last_page") or page)
            for item in payload.get("data") or []:
                if int(item.get("favorites") or 0) < source["min_favorites"]:
                    continue
                if int(item.get("views") or 0) < source["min_views"]:
                    continue
                yield item, f"{source['label']}:p{page}"
            if max_pages is not None and page - (1 + max(0, page_offset)) + 1 >= max_pages:
                break
            page += 1
