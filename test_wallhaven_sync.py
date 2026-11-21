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


if __name__ == "__main__":
    unittest.main()
