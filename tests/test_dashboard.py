import unittest

import _bootstrap  # noqa: F401
from mockingbird.ui import dashboard


class DashboardBarTests(unittest.TestCase):
    def test_bar_rounds_up_to_a_full_cell(self):
        for fraction, expected in (
            (0.97, "███▉"),
            (0.99, "████"),
            (0.49, "██░░"),
        ):
            with self.subTest(fraction=fraction):
                self.assertEqual(dashboard.bar(fraction, 4), expected)

    def test_bar_clamps_to_track_width(self):
        self.assertEqual(dashboard.bar(-0.1, 4), "░░░░")
        self.assertEqual(dashboard.bar(1.1, 4), "████")
        self.assertEqual(dashboard.bar(0.5, 0), "")
