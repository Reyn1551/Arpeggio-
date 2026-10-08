import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from calc.ops import mul


class MulTest(unittest.TestCase):
    def test_mul(self):
        self.assertEqual(mul(4, 5), 20)
