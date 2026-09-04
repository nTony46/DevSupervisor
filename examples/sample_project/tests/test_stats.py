import unittest

from src.stats import average, total


class StatsTests(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total([1, 2, 3]), 6)

    def test_average(self):
        self.assertEqual(average([2, 4]), 3)
