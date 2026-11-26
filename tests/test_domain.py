"""领域纯规则测试：区间、份额取整、协议叠加与共担拆分。"""

import unittest
from fractions import Fraction

from settlement.domain import (
    allocate_cents,
    day_count,
    overlap_days,
    parse_fraction,
    split_claim,
)

AGREEMENTS = [
    {"id": "aid-A", "restriction": "no_clearcut", "span": [1, 30],
     "stack": "shared", "fund": "A"},
    {"id": "aid-B", "restriction": "no_clearcut", "span": [12, 30],
     "stack": "shared", "fund": "B"},
    {"id": "aid-C", "restriction": "retain_buffer", "span": [4, 28],
     "stack": "independent", "fund": "C"},
]
ALLOCATION = {"A_B": [3, 2]}


class CalendarTest(unittest.TestCase):
    def test_closed_spans(self):
        self.assertEqual(day_count([1, 1]), 1)
        self.assertEqual(day_count([12, 30]), 19)
        self.assertEqual(overlap_days([1, 30], [12, 30]), 19)
        self.assertEqual(overlap_days([1, 5], [6, 10]), 0)
        self.assertEqual(overlap_days([4, 28], [10, 12]), 3)

    def test_parse_fraction(self):
        self.assertEqual(parse_fraction("1/2"), Fraction(1, 2))
        self.assertEqual(parse_fraction(Fraction(1, 3)), Fraction(1, 3))


class AllocateCentsTest(unittest.TestCase):
    def test_sum_is_exact(self):
        for total in range(0, 200):
            parts = allocate_cents({"a": Fraction(1), "b": Fraction(2),
                                    "c": Fraction(3)}, total)
            self.assertEqual(sum(parts.values()), total)
            self.assertEqual(set(parts), {"a", "b", "c"})

    def test_largest_remainder_distribution(self):
        # 100 按 1:1:1 -> 34/33/33，余数补给排序靠前的键。
        parts = allocate_cents({"x": 1, "y": 1, "z": 1}, 100)
        self.assertEqual(parts, {"x": 34, "y": 33, "z": 33})

    def test_zero_weights(self):
        self.assertEqual(allocate_cents({"a": 0, "b": 0}, 0),
                         {"a": 0, "b": 0})
        with self.assertRaises(ValueError):
            allocate_cents({"a": 0}, 10)

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            allocate_cents({"a": 1}, -1)


class SplitClaimTest(unittest.TestCase):
    def test_shared_cost_paid_once_with_ratio(self):
        claim = {"id": "c1", "restriction": "no_clearcut",
                 "period": [12, 30], "gross_cents": 18000}
        result = split_claim(claim, AGREEMENTS, ALLOCATION)
        # 19 天均在 A、B 两份 shared 协议内，共担只付一次，3:2 拆分。
        self.assertEqual(result["by_fund"], {"A": 10800, "B": 7200})
        self.assertEqual(result["uncovered"], 0)
        self.assertEqual(sum(result["by_fund"].values()), 18000)

    def test_never_double_pay_same_obligation(self):
        claim = {"id": "c2", "restriction": "no_clearcut",
                 "period": [1, 30], "gross_cents": 30000}
        result = split_claim(claim, AGREEMENTS, ALLOCATION)
        # 即使两份协议同时覆盖同一段限制，两预算合计也只等于一笔毛额，
        # 绝不各付全额。
        self.assertEqual(sum(result["by_fund"].values()), 30000)
        # 1-11 日仅 A 承担；12-30 日 A:B=3:2。
        # A = 30000 * (11 + 19*3/5) / 30 = 22400
        self.assertEqual(result["by_fund"]["A"], 22400)
        self.assertEqual(result["by_fund"]["B"], 7600)

    def test_independent_stacks_on_top(self):
        claim = {"id": "c3", "restriction": "retain_buffer",
                 "period": [1, 30], "gross_cents": 9000}
        result = split_claim(claim, AGREEMENTS, ALLOCATION)
        # C 为 independent，覆盖 4-28 共 25 天，全额由 C 承担；
        # 其余日期无协议覆盖。
        self.assertEqual(result["by_fund"], {"C": 7500})
        self.assertEqual(result["uncovered"], 1500)

    def test_restriction_mismatch_ignored(self):
        claim = {"id": "c4", "restriction": "other",
                 "period": [1, 30], "gross_cents": 3000}
        result = split_claim(claim, AGREEMENTS, ALLOCATION)
        self.assertEqual(result["by_fund"], {})
        self.assertEqual(result["uncovered"], 3000)

    def test_missing_ratio_raises(self):
        claim = {"id": "c5", "restriction": "no_clearcut",
                 "period": [12, 30], "gross_cents": 1000}
        with self.assertRaises(KeyError):
            split_claim(claim, AGREEMENTS, {})

    def test_daily_detail_marks_shared_and_independent(self):
        claim = {"id": "c6", "restriction": "no_clearcut",
                 "period": [10, 13], "gross_cents": 4000}
        result = split_claim(claim, AGREEMENTS, ALLOCATION)
        days = {row["day"]: row for row in result["daily"]}
        self.assertTrue(days[10]["shared"])
        self.assertEqual([d["agreement"] for d in days[12]["shared"]],
                         ["aid-A", "aid-B"])


if __name__ == "__main__":
    unittest.main()
