"""Unit tests for wallhaven_sync's pure logic (no network, no image files).

Run: python3 -m unittest discover -s . -p 'test_*.py'
"""

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import wallhaven_sync as ws


def entry(sat=0.4, chroma=40, hue=10):
    return {
        "stats": {"luma": 0.3, "stddev": 0.2, "sat": sat},
        "palette": {"hue": hue, "chroma": chroma},
    }


class HueBucketsTest(unittest.TestCase):
    def test_buckets_cover_360_degrees(self):
        index = {
            "images": {
                "a": entry(hue=0),
                "b": entry(hue=29),
                "c": entry(hue=30),
                "d": entry(hue=359),
                "e": {"stats": {}, "palette": None},
            }
        }
        buckets = ws.hue_buckets(index)
        self.assertEqual(buckets[0], 2)  # 0 and 29
        self.assertEqual(buckets[1], 1)  # 30
        self.assertEqual(buckets[11], 1)  # 359
        self.assertEqual(sum(buckets), 4)


class JudgeTest(unittest.TestCase):
    def test_bright_image_is_rejected(self):
        stats = {"luma": 0.8, "stddev": 0.2, "sat": 0.4}
        self.assertEqual(ws.judge(stats, None, {"images": {}}, 365), "too-bright")

    def test_dark_image_is_rejected(self):
        stats = {"luma": 0.01, "stddev": 0.2, "sat": 0.4}
        self.assertEqual(ws.judge(stats, None, {"images": {}}, 365), "too-dark")

    def test_flat_image_is_rejected(self):
        stats = {"luma": 0.3, "stddev": 0.01, "sat": 0.4}
        self.assertEqual(ws.judge(stats, None, {"images": {}}, 365), "too-flat")

    def test_first_muted_image_is_allowed(self):
        stats = {"luma": 0.3, "stddev": 0.2, "sat": 0.0}
        self.assertIsNone(ws.judge(stats, {"chroma": 5, "hue": 10}, {"images": {}}, 365))

    def test_muted_quota_is_enforced(self):
        index = {"images": {str(i): entry(sat=0.0, chroma=5) for i in range(37)}}
        stats = {"luma": 0.3, "stddev": 0.2, "sat": 0.0}
        self.assertEqual(ws.judge(stats, {"chroma": 5, "hue": 10}, index, 365), "muted-quota")

    def test_hue_quota_is_enforced_before_eighty_five_percent(self):
        index = {"images": {str(i): entry(hue=10) for i in range(50)}}
        stats = {"luma": 0.3, "stddev": 0.2, "sat": 0.4}
        self.assertEqual(ws.judge(stats, {"chroma": 40, "hue": 10}, index, 365), "hue-quota")

    def test_hue_quota_relaxes_near_the_end(self):
        index = {"images": {str(i): entry(hue=10) for i in range(320)}}
        stats = {"luma": 0.3, "stddev": 0.2, "sat": 0.4}
        self.assertIsNone(ws.judge(stats, {"chroma": 40, "hue": 10}, index, 365))

    def test_relaxed_mode_ignores_hue_and_muted_quotas(self):
        index = {"images": {str(i): entry(hue=10) for i in range(50)}}
        stats = {"luma": 0.3, "stddev": 0.2, "sat": 0.4}
        self.assertIsNone(ws.judge(stats, {"chroma": 40, "hue": 10}, index, 365, relaxed=True))
        muted = {"luma": 0.3, "stddev": 0.2, "sat": 0.0}
        self.assertIsNone(ws.judge(muted, {"chroma": 5, "hue": 10}, index, 365, relaxed=True))
        # 硬过滤在宽松模式仍然生效
        bright = {"luma": 0.9, "stddev": 0.2, "sat": 0.4}
        self.assertEqual(ws.judge(bright, None, index, 365, relaxed=True), "too-bright")


class ParseIdTest(unittest.TestCase):
    def test_url_filename_and_bare_id(self):
        self.assertEqual(ws.parse_wallpaper_id("https://wallhaven.cc/w/94x34k"), "94x34k")
        self.assertEqual(ws.parse_wallpaper_id("wallhaven-94x34k.jpg"), "94x34k")
        self.assertEqual(ws.parse_wallpaper_id("94x34k"), "94x34k")
        self.assertIsNone(ws.parse_wallpaper_id("https://wallhaven.cc/search?q=x"))


class PhashTest(unittest.TestCase):
    def test_distance_and_pairs(self):
        a = f"{0:016x}"
        b = f"{1:016x}"
        c = f"{2**64 - 1:016x}"
        self.assertEqual(ws.phash_distance(a, b), 1)
        pairs = ws.find_phash_pairs({"a": a, "b": b, "c": c}, 4)
        self.assertIn((1, "a", "b"), pairs)
        self.assertNotIn((1, "a", "c"), pairs)
        self.assertEqual(ws.find_phash_pairs({"a": a, "b": b}, 0), [])

    def test_phash_from_gray(self):
        flat = ws.phash_from_gray(b"\x00" * 256)
        self.assertEqual(flat, "0" * 16)
        gradient = bytes((i * 7) % 256 for i in range(256))
        self.assertEqual(ws.phash_from_gray(gradient), ws.phash_from_gray(gradient))
        self.assertEqual(len(ws.phash_from_gray(gradient)), 16)
        self.assertIsNone(ws.phash_from_gray(b"\x00" * 255))

    def test_filename_id(self):
        self.assertEqual(ws.pool_id_from_filename("wallhaven-94x34k.jpg"), "94x34k")
        self.assertIsNone(ws.pool_id_from_filename("94x34k.jpg"))


class HistoryTest(unittest.TestCase):
    def test_append_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ws.Paths(pool=Path(tmp) / "pool", state=Path(tmp) / "state", key_file=Path(tmp) / "key")
            ws.append_history(paths, {"id": "abc123", "at": "1", "in_pool": True})
            ws.append_history(paths, {"id": "def456", "at": "2", "in_pool": False})
            events = ws.read_history(paths)
            self.assertEqual([event["id"] for event in events], ["abc123", "def456"])

    def test_record_reads_noctalia_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            pool.mkdir()
            wallpaper = pool / "wallhaven-abc123.jpg"
            wallpaper.write_bytes(b"x")
            paths = ws.Paths(pool=pool, state=Path(tmp) / "state", key_file=Path(tmp) / "key")
            with mock.patch.dict(
                os.environ,
                {
                    "NOCTALIA_WALLPAPER_PATH": str(wallpaper),
                    "NOCTALIA_WALLPAPER_CONNECTOR": "eDP-1",
                },
            ):
                ws.cmd_record(paths, None)
            event = ws.read_history(paths)[-1]
            self.assertEqual(event["id"], "abc123")
            self.assertTrue(event["in_pool"])
            self.assertEqual(event["connector"], "eDP-1")


class DedupeApplyTest(unittest.TestCase):
    def _paths(self, tmp):
        pool = Path(tmp) / "pool"
        pool.mkdir()
        return ws.Paths(pool=pool, state=Path(tmp) / "state", key_file=Path(tmp) / "key")

    def _index(self, paths, entries):
        index = ws.new_index()
        index["images"] = entries
        ws.save_index(paths, index)
        return index

    def test_keeps_the_only_surviving_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            (paths.pool / "lost.jpg").write_bytes(b"")
            (paths.pool / "kept.jpg").write_bytes(b"")
            # "lost" has a larger byte count but its file is missing.
            self._index(paths, {
                "aaaaaa": {"file": "gone.jpg", "bytes": 10_000_000, "phash": "0" * 16},
                "bbbbbb": {"file": "kept.jpg", "bytes": 1, "phash": "0" * 15 + "1"},
            })
            args = mock.Mock(apply=True, threshold=7, auto=3, list_quarantine=False, restore=None)
            ws.cmd_dedupe(paths, args)
            self.assertTrue((paths.pool / "kept.jpg").exists())
            index = ws.load_index(paths)
            self.assertIn("bbbbbb", index["images"])
            self.assertNotIn("bbbbbb", index["quarantine"])

    def test_apply_moves_smaller_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            (paths.pool / "big.jpg").write_bytes(b"x" * 20)
            (paths.pool / "small.jpg").write_bytes(b"x")
            self._index(paths, {
                "aaaaaa": {"file": "big.jpg", "bytes": 20, "phash": "0" * 16},
                "bbbbbb": {"file": "small.jpg", "bytes": 1, "phash": "0" * 15 + "1"},
            })
            args = mock.Mock(apply=True, threshold=7, auto=3, list_quarantine=False, restore=None)
            ws.cmd_dedupe(paths, args)
            self.assertTrue((paths.pool / "big.jpg").exists())
            self.assertFalse((paths.pool / "small.jpg").exists())
            index = ws.load_index(paths)
            self.assertIn("bbbbbb", index["quarantine"])
            self.assertEqual(index["quarantine"]["bbbbbb"]["of"], "aaaaaa")


class RestoreTest(unittest.TestCase):
    def test_restore_old_quarantine_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            paths = ws.Paths(pool=pool, state=Path(tmp) / "state", key_file=Path(tmp) / "key")
            paths.quarantine.mkdir(parents=True)
            quarantined = paths.quarantine / "wallhaven-abc123.jpg"
            quarantined.write_bytes(b"x")
            index = ws.new_index()
            index["quarantine"]["abc123"] = {
                "file": quarantined.name,
                "of": "zzzzzz",
                "distance": 2,
            }
            ws.save_index(paths, index)
            args = argparse.Namespace(
                apply=False,
                threshold=7,
                auto=3,
                list_quarantine=False,
                restore="abc123",
            )
            ws.cmd_dedupe(paths, args)
            self.assertTrue((pool / quarantined.name).exists())
            loaded = ws.load_index(paths)
            self.assertIn("abc123", loaded["images"])
            self.assertNotIn("abc123", loaded["quarantine"])


class DuplicateSkipTest(unittest.TestCase):
    def test_skip_only_while_original_present(self):
        index = {
            "images": {"aaaaaa": {}},
            "duplicates": {"bbbbbb": {"of": "aaaaaa"}, "cccccc": {"of": "gone00"}},
        }
        self.assertEqual(ws.blocked_duplicates(index), {"bbbbbb"})


class HelpersTest(unittest.TestCase):
    def test_extension_for_prefers_url_suffix(self):
        item = {"path": "https://w.wallhaven.cc/full/ab/wallhaven-abcd12.png", "file_type": "image/jpeg"}
        self.assertEqual(ws.extension_for(item), ".png")

    def test_extension_for_falls_back_to_mime_type(self):
        item = {"path": "https://example.invalid/download", "file_type": "image/webp"}
        self.assertEqual(ws.extension_for(item), ".webp")

    def test_format_bytes(self):
        self.assertEqual(ws.format_bytes(512), "512 B")
        self.assertEqual(ws.format_bytes(2048), "2.0 KiB")
        self.assertTrue(ws.format_bytes(5 * 1024**3).endswith("GiB"))

    def test_api_reject_reason(self):
        self.assertEqual(ws.api_reject_reason({"purity": "nsfw"}), "not-sfw")
        self.assertEqual(ws.api_reject_reason({"purity": "sfw", "category": "anime"}), "not-general")
        self.assertEqual(
            ws.api_reject_reason({"purity": "sfw", "category": "general", "file_size": 99 * 1024**2}),
            "too-large",
        )
        self.assertEqual(
            ws.api_reject_reason(
                {"purity": "sfw", "category": "general", "file_size": 1024, "file_type": "image/gif"}
            ),
            "unsupported-format",
        )
        self.assertIsNone(ws.api_reject_reason({"purity": "sfw", "category": "general", "file_size": 1024}))


class MetadataTest(unittest.TestCase):
    def test_entry_metadata_keeps_api_fields(self):
        item = {
            "id": "abc123",
            "url": "https://wallhaven.cc/w/abc123",
            "dimension_x": 3840,
            "dimension_y": 2160,
            "resolution": "3840x2160",
            "file_type": "image/jpeg",
            "favorites": 420,
            "views": 12000,
            "category": "general",
            "purity": "sfw",
            "created_at": "2026-01-01 00:00:00",
            "thumbs": {"large": "https://th/lg.jpg"},
        }
        meta = ws.entry_metadata(item, "tag:city:3M:p2")
        self.assertEqual(meta["theme"], "city")
        self.assertEqual(meta["width"], 3840)
        self.assertEqual(meta["thumb_url"], "https://th/lg.jpg")
        self.assertEqual(meta["favorites"], 420)
        self.assertEqual(ws.theme_for_source("latest:p1"), "latest")
        self.assertEqual(ws.theme_for_source("toplist:1M:p1"), "toplist")
        self.assertEqual(ws.theme_for_source("restored"), "restored")


class SourceTest(unittest.TestCase):
    def _args(self, **overrides):
        values = {
            "tags": "landscape,city",
            "no_tags": False,
            "include_latest": True,
            "min_favorites": 5,
            "min_views": 10,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_sources_cover_ranges_tags_and_latest(self):
        sources = ws.build_sources(self._args())
        labels = {source["label"] for source in sources}
        self.assertIn("toplist:1M", labels)
        self.assertIn("toplist:1y", labels)
        self.assertIn("tag:landscape:3M", labels)
        self.assertIn("tag:city:6M", labels)
        self.assertIn("latest", labels)
        self.assertEqual(len(sources), 4 * (1 + 2) + 1)
        for source in sources:
            self.assertEqual(source["params"]["categories"], "100")
            self.assertEqual(source["params"]["purity"], "100")
        latest = next(source for source in sources if source["label"] == "latest")
        self.assertEqual(latest["min_favorites"], ws.LATEST_MIN_FAVORITES)
        self.assertEqual(latest["min_views"], ws.LATEST_MIN_VIEWS)

    def test_no_tags(self):
        labels = {source["label"] for source in ws.build_sources(self._args(no_tags=True))}
        self.assertFalse(any(label.startswith("tag:") for label in labels))

    def test_page_offset_and_page_cap(self):
        calls = []

        def fake_api(_key, _endpoint, params):
            calls.append(params)
            return {"data": [], "meta": {"last_page": 10}}

        sources = [
            {
                "label": "toplist:1M",
                "params": {},
                "min_favorites": 0,
                "min_views": 0,
            }
        ]
        with mock.patch.object(ws, "api_get", side_effect=fake_api):
            list(ws.iter_candidates("key", max_pages=2, sources=sources, page_offset=3))
        self.assertEqual([call["page"] for call in calls], ["4", "5"])


class DatabaseTest(unittest.TestCase):
    def _paths(self, tmp):
        pool = Path(tmp) / "pool"
        pool.mkdir()
        return ws.Paths(pool=pool, state=Path(tmp) / "state", key_file=Path(tmp) / "key")

    def _entry(self, name="wallhaven-abc123.jpg", phash="0" * 15 + "1"):
        return {
            "file": name,
            "source": "toplist:1M:p1",
            "theme": "toplist",
            "width": 1920,
            "height": 1080,
            "resolution": "1920x1080",
            "bytes": 1000,
            "sha256": "a" * 64,
            "phash": phash,
            "stats": {"luma": 0.3, "stddev": 0.2, "sat": 0.4},
            "palette": {"primary": "#336699", "hue": 210, "chroma": 40},
            "favorites": 10,
            "views": 1000,
            "category": "general",
            "purity": "sfw",
            "url": "https://wallhaven.cc/w/abc123",
            "thumb_url": "https://th/lg.jpg",
            "added_at": ws.now(),
        }

    def test_mirror_and_record_display(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            index = ws.new_index()
            index["images"]["abc123"] = self._entry()
            ws.mirror_index_to_db(paths, index)

            row = ws.db_get(paths, "abc123")
            self.assertEqual(row["status"], "active")
            self.assertEqual(row["primary_color"], "#336699")
            self.assertEqual(row["favorites"], 10)

            ws.db_record_display(paths, "abc123", "eDP-1", str(paths.pool / "wallhaven-abc123.jpg"))
            row = ws.db_get(paths, "abc123")
            self.assertEqual(row["shown_count"], 1)
            self.assertIsNotNone(row["first_shown_at"])
            # A repeated apply of the same path must not inflate shown_count.
            ws.db_record_display(paths, "abc123", "eDP-1", str(paths.pool / "wallhaven-abc123.jpg"))
            row = ws.db_get(paths, "abc123")
            self.assertEqual(row["shown_count"], 1)
            with ws.open_db(paths) as conn:
                events = conn.execute("SELECT COUNT(*) FROM display_history").fetchone()[0]
            self.assertEqual(events, 2)

    def test_evicted_rows_become_blockers_and_can_revive(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            index = ws.new_index()
            index["images"]["abc123"] = self._entry()
            ws.mirror_index_to_db(paths, index)
            ws.db_set_status(paths, "abc123", "evicted", reason="rotation", trash_path="/tmp/x")
            ids, phashes, shas = ws.db_blockers(paths)
            self.assertIn("abc123", ids)
            self.assertEqual(phashes["abc123"], "0" * 15 + "1")
            self.assertEqual(shas["a" * 64], "abc123")
            self.assertEqual(ws.db_counts(paths)["evicted"], 1)

            # Re-adding to the index clears the eviction fields.
            ws.mirror_index_to_db(paths, index)
            row = ws.db_get(paths, "abc123")
            self.assertEqual(row["status"], "active")
            self.assertIsNone(row["evicted_at"])
            self.assertIsNone(row["trash_path"])

    def test_evicted_today_only_counts_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            index = ws.new_index()
            index["images"]["abc123"] = self._entry()
            ws.mirror_index_to_db(paths, index)
            with ws.open_db(paths) as conn:
                conn.execute(
                    "UPDATE images SET status='evicted', evicted_at=?, evicted_reason='rotation' WHERE id='abc123'",
                    (ws.now(),),
                )
                conn.commit()
            self.assertEqual(ws.db_evicted_today(paths), 1)
            with ws.open_db(paths) as conn:
                conn.execute("UPDATE images SET evicted_reason='missing' WHERE id='abc123'")
                conn.commit()
            self.assertEqual(ws.db_evicted_today(paths), 0)


class VictimTest(unittest.TestCase):
    def _setup(self, tmp):
        pool = Path(tmp) / "pool"
        pool.mkdir()
        paths = ws.Paths(pool=pool, state=Path(tmp) / "state", key_file=Path(tmp) / "key")
        index = ws.new_index()
        for image_id in ("a1", "a2", "a3", "a4", "a5"):
            entry = {
                "file": f"wallhaven-{image_id}.jpg",
                "added_at": "2026-01-01T00:00:00+00:00",
                "stats": {},
                "palette": {},
            }
            (pool / entry["file"]).write_bytes(b"x")
            index["images"][image_id] = entry
        ws.mirror_index_to_db(paths, index)
        with ws.open_db(paths) as conn:
            conn.execute("UPDATE images SET last_shown_at='2026-11-11T00:00:00+00:00' WHERE id='a1'")
            conn.execute("UPDATE images SET last_shown_at='2026-11-16T00:00:00+00:00' WHERE id='a2'")
            conn.execute("UPDATE images SET last_shown_at='2026-11-20T23:59:00+00:00', pinned=1 WHERE id='a4'")
            conn.execute("UPDATE images SET last_shown_at='2026-10-01T00:00:00+00:00' WHERE id='a5'")
            conn.commit()
        return paths, index

    def test_oldest_shown_first_and_protections(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, index = self._setup(tmp)
            victims = ws.select_victims(
                paths, index, 5, protect={"a5"}, protect_recent_hours=2
            )
            # a3 was never shown, a4 is pinned, a5 is on screen.
            self.assertEqual(victims, ["a1", "a2"])

    def test_unshown_only_when_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, index = self._setup(tmp)
            victims = ws.select_victims(
                paths,
                index,
                5,
                protect=set(),
                include_unshown=True,
                protect_recent_hours=0,
            )
            self.assertEqual(victims[0], "a5")  # oldest shown first
            self.assertIn("a3", victims)  # unseen allowed
            self.assertNotIn("a4", victims)  # pinned
            self.assertIn("a1", victims)


class TrashTest(unittest.TestCase):
    def _paths(self, tmp):
        pool = Path(tmp) / "pool"
        pool.mkdir()
        return ws.Paths(pool=pool, state=Path(tmp) / "state", key_file=Path(tmp) / "key")

    def test_purge_keeps_recent_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            month = paths.trash / "202611"
            month.mkdir(parents=True)
            old = month / "old.jpg"
            new = month / "new.jpg"
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            past = ws.time.time() - 9 * 86400
            os.utime(old, (past, past))
            removed = ws.purge_trash(paths, 7)
            self.assertEqual(removed, [old])
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())

    def test_evict_moves_to_trash_and_restore_brings_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(tmp)
            index = ws.new_index()
            index["images"]["abc123"] = {
                "file": "wallhaven-abc123.jpg",
                "source": "toplist:1M:p1",
                "theme": "toplist",
                "added_at": ws.now(),
                "bytes": 1,
                "sha256": "b" * 64,
                "phash": "0" * 15 + "2",
                "stats": {"luma": 0.3, "stddev": 0.2, "sat": 0.4},
                "palette": {"primary": "#336699", "hue": 210, "chroma": 40},
            }
            (paths.pool / "wallhaven-abc123.jpg").write_bytes(b"x")
            ws.save_index(paths, index)
            ws.mirror_index_to_db(paths, index)

            args = argparse.Namespace(dry_run=False)
            evicted = ws.evict_batch(paths, index, ["abc123"], args)
            self.assertEqual(evicted, ["abc123"])
            self.assertFalse((paths.pool / "wallhaven-abc123.jpg").exists())
            row = ws.db_get(paths, "abc123")
            self.assertEqual(row["status"], "evicted")
            self.assertTrue(Path(row["trash_path"]).exists())

            ws.cmd_restore(paths, argparse.Namespace(id="abc123", target=365))
            self.assertTrue((paths.pool / "wallhaven-abc123.jpg").exists())
            self.assertEqual(ws.db_get(paths, "abc123")["status"], "active")
            self.assertIn("abc123", ws.load_index(paths)["images"])


class AddArchivedTest(unittest.TestCase):
    def test_add_refuses_archived_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = Path(tmp) / "pool"
            pool.mkdir()
            paths = ws.Paths(pool=pool, state=Path(tmp) / "state", key_file=Path(tmp) / "key")
            with ws.open_db(paths) as conn:
                conn.execute(
                    "INSERT INTO images (id, status, phash) VALUES ('abc123', 'evicted', ?)",
                    ("0" * 15 + "3",),
                )
                conn.commit()
            result = ws.add_item(
                paths,
                ws.new_index(),
                {"id": "abc123", "purity": "sfw", "category": "general"},
                "manual",
                365,
            )
            self.assertFalse(result)


class ReplenishTest(unittest.TestCase):
    def _args(self, **overrides):
        values = {
            "target": 3,
            "max_per_day": 1,
            "keep_trash_days": 7,
            "dry_run": False,
            "force": False,
            "evict_unshown": False,
            "protect_recent_hours": 2,
            "page_offset": 0,
            "max_pages": 2,
            "limit": 5,
            "tags": "",
            "no_tags": True,
            "include_latest": False,
            "min_favorites": 0,
            "min_views": 0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def _setup(self, tmp):
        pool = Path(tmp) / "pool"
        pool.mkdir()
        paths = ws.Paths(pool=pool, state=Path(tmp) / "state", key_file=Path(tmp) / "key")
        index = ws.new_index()
        for image_id in ("a1", "a2", "a3"):
            entry = {
                "file": f"wallhaven-{image_id}.jpg",
                "source": "toplist:1M:p1",
                "theme": "toplist",
                "added_at": "2026-01-01T00:00:00+00:00",
                "bytes": 10,
                "stats": {},
                "palette": {},
            }
            (pool / entry["file"]).write_bytes(b"x" * 10)
            index["images"][image_id] = entry
        ws.save_index(paths, index)
        ws.mirror_index_to_db(paths, index)
        with ws.open_db(paths) as conn:
            conn.execute("UPDATE images SET last_shown_at='2026-01-02T00:00:00+00:00' WHERE id='a1'")
            conn.execute("UPDATE images SET last_shown_at=? WHERE id='a2'", (ws.now(),))
            conn.commit()
        return paths

    def test_evicts_only_after_successful_topup(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._setup(tmp)

            def fake_sync(paths, sync_args):
                index = ws.load_index(paths)
                index["images"]["new01"] = {
                    "file": "wallhaven-new01.jpg",
                    "source": "toplist:1M:p1",
                    "theme": "toplist",
                    "added_at": ws.now(),
                    "bytes": 10,
                    "stats": {},
                    "palette": {},
                }
                (paths.pool / "wallhaven-new01.jpg").write_bytes(b"x" * 10)
                ws.save_index(paths, index)
                return 0

            with mock.patch.object(ws, "sync_locked", side_effect=fake_sync), mock.patch.object(
                ws, "current_wallpaper_ids", return_value=set()
            ), mock.patch.object(ws, "read_key", return_value="k" * 20), mock.patch.object(
                ws, "enrich_evicted_tags"
            ):
                ws.cmd_replenish(paths, self._args())

            index = ws.load_index(paths)
            self.assertEqual(index["target"], 3)
            self.assertEqual(len(index["images"]), 3)
            self.assertNotIn("a1", index["images"])  # oldest shown evicted
            self.assertIn("new01", index["images"])
            self.assertIn("a2", index["images"])  # recent
            self.assertIn("a3", index["images"])  # never shown
            self.assertEqual(ws.db_get(paths, "a1")["status"], "evicted")
            self.assertTrue(any(paths.trash.rglob("wallhaven-a1.jpg")))

    def test_daily_quota_blocks_more_evictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._setup(tmp)
            ws.db_set_status(paths, "a1", "evicted", reason="rotation")
            with mock.patch.object(ws, "sync_locked", return_value=0) as synced, mock.patch.object(
                ws, "current_wallpaper_ids", return_value=set()
            ):
                ws.cmd_replenish(paths, self._args())
            synced.assert_called_once()
            self.assertIn("a1", ws.load_index(paths)["images"])
            self.assertTrue((paths.pool / "wallhaven-a1.jpg").exists())


if __name__ == "__main__":
    unittest.main()
