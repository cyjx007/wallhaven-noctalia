"""Unit tests for wallhaven_sync's pure logic (no network, no image files).

Run: python3 -m unittest discover -s . -p 'test_*.py'
"""

import unittest

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
