import unittest
from unittest.mock import Mock, patch

from cec_ha_bridge import Bridge


class HoldAccelerationTest(unittest.TestCase):
    def setUp(self):
        with patch("cec_ha_bridge.C.CDLL", return_value=Mock()):
            self.bridge = Bridge({})
        self.bridge.apples = {8}

    def press(self, at, key=0x41, source=8):
        with patch("cec_ha_bridge.time.monotonic", return_value=at):
            self.bridge.press(source, key)

    def steps(self):
        values = []
        while not self.bridge.buttons.empty():
            values.append(self.bridge.buttons.get_nowait()[2]["steps"])
        return values

    def test_hold_accelerates_only_after_threshold(self):
        for at in (10, 10.25, 10.5, 10.75):
            self.press(at)
        self.assertEqual(self.steps(), [1, 1, 1, 2])

    def test_release_keeps_fast_individual_taps_at_one_step(self):
        values = []
        for at in (10, 10.25, 10.5, 10.75, 11):
            self.press(at)
            self.bridge.handle(8, 0, bytes.fromhex("85 45 41"))
            values.extend(self.steps())
        self.assertEqual(values, [1, 1, 1, 1, 1])

    def test_release_after_acceleration_resets_next_press(self):
        for at in (10, 10.5, 10.8):
            self.press(at)
        self.assertEqual(self.steps(), [1, 1, 2])
        self.bridge.handle(8, 0, bytes.fromhex("85 45 41"))
        self.press(11)
        self.assertEqual(self.steps(), [1])

    def test_gap_or_direction_change_resets_acceleration(self):
        self.press(10)
        self.press(10.5)
        self.press(10.8)
        self.assertEqual(self.steps(), [1, 1, 2])
        self.press(11.5)
        self.press(11.75, 0x42)
        self.assertEqual(self.steps(), [1, 1])

    def test_rate_limited_frames_preserve_hold_start(self):
        self.press(10)
        for at in (10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8):
            self.press(at)
            if at < 10.8:
                self.steps()
        self.assertEqual(self.steps(), [2])

    def test_mute_and_unknown_source_do_not_accelerate(self):
        self.press(10, 0x43)
        for at in (10.25, 10.5, 10.75, 11):
            self.press(at, 0x43)
        self.press(12, source=4)
        self.assertEqual(self.steps(), [1])

    def test_configured_step_count_is_bounded(self):
        self.bridge.config["hold_acceleration_steps"] = 99
        self.press(10)
        self.press(10.5)
        self.press(11)
        self.assertEqual(self.steps(), [1, 1, 3])


if __name__ == "__main__":
    unittest.main()
