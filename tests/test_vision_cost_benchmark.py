"""The vision cost benchmark: synthetic images only, exact ground truth, stops on a rejected key."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import vision_cost  # noqa: E402
from clickwarden.providers import Settings  # noqa: E402


class SyntheticImageTests(unittest.TestCase):
    def test_button_is_drawn_exactly_where_the_ground_truth_says(self):
        window = vision_cost.draw_window()
        l, t, r, b = vision_cost.BUTTON
        self.assertEqual(window.size, vision_cost.WINDOW)
        self.assertEqual(window.getpixel((l + 20, (t + b) // 2)), (0xdd, 0xe6, 0xf5))
        self.assertEqual(window.getpixel((l - 10, (t + b) // 2)), (255, 255, 255))
        desk, at = vision_cost.draw_fullscreen(window)
        self.assertEqual(desk.size, (1920, 1080))
        self.assertEqual(desk.getpixel((at[0] + l + 20, at[1] + (t + b) // 2)), (0xdd, 0xe6, 0xf5))

    def test_frames_map_image_pixels_one_to_one(self):
        frame = vision_cost.frame_of(vision_cost.draw_window())
        self.assertEqual(frame.to_screen(210, 220), (210, 220))
        self.assertTrue(frame.jpeg.startswith(b"\xff\xd8"))


class FatalErrorTests(unittest.TestCase):
    def test_rejected_keys_stop_the_run(self):
        self.assertTrue(vision_cost.is_fatal('HTTP 401: {"error":{"message":"API key expired."}}'))
        self.assertTrue(vision_cost.is_fatal("HTTP 403: forbidden"))
        for transient in ("HTTP 429: rate limited", "timeout", None, ""):
            self.assertFalse(vision_cost.is_fatal(transient))

    def test_a_401_ends_the_benchmark_after_one_request_and_saves_nothing(self):
        settings = Settings(base_url="https://vision.example.com/v1/chat/completions", model="m", api_key="k")
        calls = []

        def fake_measure(client, kind, frame, box):
            calls.append(kind)
            return {"ok": False, "error": "HTTP 401: expired", "ms": 1, "prompt": 0, "completion": 0,
                    "rate_limited": False}
        with patch.object(vision_cost.providers, "resolve", return_value=settings), \
                patch.object(vision_cost, "measure", side_effect=fake_measure), \
                patch.object(vision_cost.Path, "write_text") as write, \
                patch.object(sys, "argv", ["vision_cost.py", "--quick", "--pause", "0"]):
            code = vision_cost.main()
        self.assertEqual((code, len(calls)), (3, 1))
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
