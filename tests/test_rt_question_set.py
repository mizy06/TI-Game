import re
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
HEADER = PROJECT_DIR / "rt_question_set.h"


class RtQuestionSetTests(unittest.TestCase):
    def test_exact_case_counts_and_order(self):
        text = HEADER.read_text(encoding="utf-8")
        rows = re.findall(
            r"^\s*\{\s*([^}]+)\}\s*,\s*/\*\s*(\d{3})\s*\*/$",
            text,
            flags=re.MULTILINE,
        )
        self.assertEqual(296, len(rows))
        self.assertEqual(list(range(1, 297)), [int(label) for _, label in rows])

        groups = []
        for fields, _ in rows:
            values = [int(value) for value in re.findall(r"(\d+)U", fields)]
            self.assertEqual(11, len(values))
            groups.append(values[6])

        self.assertEqual([0] * 200, groups[:200])
        self.assertEqual([1] * 96, groups[200:])

    def test_public_count_macros(self):
        text = HEADER.read_text(encoding="utf-8")
        self.assertRegex(text, r"#define RT_CUSTOM_CASE_COUNT\s+\(200U\)")
        self.assertRegex(text, r"#define RT_CHALLENGE_CASE_COUNT\s+\(96U\)")


if __name__ == "__main__":
    unittest.main()
