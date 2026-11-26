"""台账业务规则测试。

覆盖叙述中的关键规则：
- 一块地多个权利份额，已明份额先付，待证份额提存，不牵连无争议住户；
- 两份协议按限制类别、有效期与可叠加性核对，共同款项不重复支付；
- 预算封存时按约定决定可否先行承担，未决资金不被视作已花出或另许；
- 晚到确权证明区分未发出/已提交未查清/已确认入账三种款项；
- 身份更正只改变今后受款，回执不设定权益，到账差额另办追补/退回；
- 住户、经办、财政三类视图各取所需。
"""

import unittest

from settlement.ledger import Ledger


REGISTER = {
    "land": "L-50",
    "share": [
        {"subject": "hh-1", "fraction": "1/2", "verified": True},
        {"subject": "hh-2", "fraction": "1/2", "verified": False},
    ],
    "agreements": [
        {"id": "aid-A", "restriction": "no_clearcut", "span": [1, 30],
         "stack": "shared", "fund": "A"},
        {"id": "aid-B", "restriction": "no_clearcut", "span": [12, 30],
         "stack": "shared", "fund": "B"},
        {"id": "aid-C", "restriction": "retain_buffer", "span": [4, 28],
         "stack": "independent", "fund": "C"},
    ],
    "funds": {"A": 23000, "B": 16000, "C": 9800},
    "allocation": {"A_B": [3, 2]},
    "interim_funding": {
        "A": {"can_pay_own_undisputed_share": True,
              "can_advance_for_B": False},
    },
    "claims": [
        {"id": "claim-18", "land": "L-50",
         "restriction": "no_clearcut", "period": [12, 30],
         "gross_cents": 18000},
    ],
}


def build_ledger(register=None):
    import copy
    ledger = Ledger()
    ledger.load_register(copy.deepcopy(register or REGISTER))
    return ledger


def by_key(decisions, **criteria):
    return [d for d in decisions
            if all(d.get(k) == v for k, v in criteria.items())]


class VerifiedFirstDisputedEscrowTest(unittest.TestCase):
    def setUp(self):
        self.ledger = build_ledger()

    def test_verified_share_payable_undisputed_not_withheld(self):
        decisions, _ = self.ledger.decisions()
        holder = by_key(decisions, kind="holder", subject="hh-1")
        self.assertTrue(holder)
        for line in holder:
            self.assertEqual(line["status"], "payable")
        # 已明 1/2 份额的钱不被扣留去补待证份额的差额。
        self.assertEqual(sum(d["cents"] for d in holder), 9000)

    def test_unverified_share_goes_to_escrow(self):
        decisions, _ = self.ledger.decisions()
        escrow = by_key(decisions, kind="escrow")
        self.assertEqual(sum(d["cents"] for d in escrow), 9000)
        self.assertTrue(all(d["status"] == "escrow" for d in escrow))
        # 提存与发放合计仍是完整毛额。
        self.assertEqual(
            sum(d["cents"] for d in decisions), 18000)

    def test_escrow_does_not_occupy_household_payment(self):
        view = self.ledger.household_view("hh-1")
        self.assertEqual(view["entitled_cents"], 9000)
        self.assertEqual(view["payable_unsubmitted_cents"], 9000)
        self.assertEqual(view["escrowed_cents"], 0)

    def test_disputed_shortfall_never_docked_from_other_household(self):
        # 即使提存账户实际少钱（7600 < 应提存 9000），也不得从 hh-1
        # 这一无争议住户的应付款里扣留 1400 差额。
        self.ledger.record_escrow("L-50", 7600, "held")
        decisions, _ = self.ledger.decisions()
        hh1 = by_key(decisions, kind="holder", subject="hh-1")
        self.assertEqual(sum(d["cents"] for d in hh1), 9000)
        self.assertTrue(all(d["status"] == "payable" for d in hh1))
        # 差额只作为提存对账争议存在。
        diff = [c for c in self.ledger.cases()
                if c["type"] == "escrow_reconcile"][0]
        self.assertEqual(diff["difference_cents"], -1400)

    def test_late_register_proof_releases_escrow_to_new_holder(self):
        # seq2：hh-3 替换 hh-2，生效日 6，晚于申请期间开始但仍确权。
        self.ledger.apply_fact(
            "register",
            {"subject": "hh-3", "fraction": "1/2",
             "replaces": "hh-2", "valid_from": 6},
            sequence=2, received_at=26)
        decisions, _ = self.ledger.decisions()
        escrow = by_key(decisions, kind="escrow")
        self.assertEqual(escrow, [])
        hh3 = by_key(decisions, kind="holder", subject="hh-3")
        self.assertEqual(sum(d["cents"] for d in hh3), 9000)
        # hh-2 在申请期间内已无任何权益。
        self.assertEqual(by_key(decisions, subject="hh-2"), [])

    def test_household_claim_remains_unverified_and_escrowed(self):
        # 住户自述的份额不能自行确权。
        self.ledger.apply_fact(
            "household",
            {"kind": "share", "subject": "hh-9",
             "replaces": "hh-2", "fraction": "1/2", "valid_from": 6},
            sequence=9, received_at=27)
        decisions, _ = self.ledger.decisions()
        self.assertEqual(by_key(decisions, subject="hh-9"), [])
        escrow = by_key(decisions, kind="escrow")
        self.assertEqual(sum(d["cents"] for d in escrow), 9000)
        cases = {c["type"] for c in self.ledger.cases()}
        self.assertIn("share_pending", cases)


class AgreementStackingTest(unittest.TestCase):
    def test_independent_agreement_pays_alongside_shared(self):
        register = {
            **REGISTER,
            "claims": [
                {"id": "c-both", "land": "L-50",
                 "restriction": "no_clearcut", "period": [12, 20],
                 "gross_cents": 9000},
                {"id": "c-buffer", "land": "L-50",
                 "restriction": "retain_buffer", "period": [12, 20],
                 "gross_cents": 4500},
            ],
        }
        ledger = build_ledger(register)
        decisions, _ = ledger.decisions()
        clearcut = by_key(decisions, claim_id="c-both")
        buffer = by_key(decisions, claim_id="c-buffer")
        # 两笔限制不同，各自独立成立：shared 的 no_clearcut 与
        # independent 的 retain_buffer 可叠加，互不冲销。
        self.assertEqual(sum(d["cents"] for d in clearcut), 9000)
        self.assertEqual(sum(d["cents"] for d in buffer), 4500)
        c_fund = [d for d in buffer if d["fund"] == "C"]
        self.assertEqual(sum(d["cents"] for d in c_fund), 4500)

    def test_uncovered_period_is_flagged_not_invented(self):
        register = {
            **REGISTER,
            "agreements": [
                dict(a) for a in REGISTER["agreements"]
                if a["id"] != "aid-B"
            ],
            "claims": [
                {"id": "c-gap", "land": "L-50",
                 "restriction": "no_clearcut", "period": [28, 30],
                 "gross_cents": 3000},
            ],
        }
        # A 覆盖到 30，故仍有预算；改为让期间落在所有协议之外。
        register["agreements"] = [
            dict(a, span=[1, 10]) if a["id"] == "aid-A" else a
            for a in register["agreements"]
        ]
        ledger = build_ledger(register)
        decisions, _ = ledger.decisions()
        unfunded = by_key(decisions, kind="uncovered") + \
            by_key(decisions, kind="uncovered_escrow")
        self.assertEqual(sum(d["cents"] for d in unfunded), 3000)
        self.assertTrue(any(c["type"] == "uncovered_period"
                            for c in ledger.cases()))

    def test_insufficient_budget_blocks_without_touching_other_fund(self):
        import copy
        register = copy.deepcopy(REGISTER)
        register["funds"]["A"] = 100
        ledger = build_ledger(register)
        decisions, _ = ledger.decisions()
        blocked = by_key(decisions, fund="A", kind="holder")
        self.assertTrue(
            all(d["status"] == "blocked_budget" for d in blocked))
        # B 预算不受 A 余额影响。
        b_lines = by_key(decisions, fund="B", kind="holder")
        self.assertTrue(all(d["status"] == "payable" for d in b_lines))

    def test_three_way_shares_sum_to_claim_exactly(self):
        import copy
        register = copy.deepcopy(REGISTER)
        register["share"] = [
            {"subject": f"hh-{i}", "fraction": "1/3", "verified": True}
            for i in range(3)]
        register["claims"][0]["gross_cents"] = 10000
        ledger = build_ledger(register)
        decisions, _ = ledger.decisions()
        self.assertEqual(sum(d["cents"] for d in decisions), 10000)
        # 每个预算内部按三户等分，取整后仍严格守恒。
        for fund in ("A", "B"):
            rows = by_key(decisions, fund=fund, kind="holder")
            self.assertEqual(len(rows), 3)

    def test_two_agreements_same_restriction_split_by_dates(self):        # 同一限制类别、有效期不同的两份 shared 协议：前段仅 A，
        # 重叠段按 A_B=3:2，任何一天都不会被两预算各付一次全额。
        register = {
            **REGISTER,
            "claims": [
                {"id": "c-span", "land": "L-50",
                 "restriction": "no_clearcut", "period": [1, 30],
                 "gross_cents": 30000},
            ],
        }
        ledger = build_ledger(register)
        decisions, _ = ledger.decisions()
        total = sum(d["cents"] for d in decisions)
        self.assertEqual(total, 30000)
        a_total = sum(d["cents"] for d in decisions if d["fund"] == "A")
        b_total = sum(d["cents"] for d in decisions if d["fund"] == "B")
        self.assertEqual(a_total + b_total, 30000)
        # 精确值：11 天全 A + 19 天 A 拿 3/5。
        self.assertAlmostEqual(a_total, 22400, delta=1)
        self.assertAlmostEqual(b_total, 7600, delta=1)


class HeldFundTest(unittest.TestCase):
    def setUp(self):
        self.ledger = build_ledger()
        # 两槽位均确权后再封存 B，避免提存状态干扰预算判断。
        self.ledger.apply_fact(
            "register",
            {"subject": "hh-3", "fraction": "1/2",
             "replaces": "hh-2", "valid_from": 6},
            sequence=2, received_at=26)
        self.ledger.apply_fact(
            "register",
            {"subject": "hh-5", "fraction": "1/2",
             "replaces": "hh-1", "valid_from": 1},
            sequence=8, received_at=27)
        self.ledger.apply_fact(
            "budget", {"fund": "B", "state": "held"},
            sequence=3, received_at=28)

    def test_held_fund_blocks_only_its_own_share(self):
        decisions, _ = self.ledger.decisions()
        b_lines = by_key(decisions, fund="B", kind="holder")
        a_lines = by_key(decisions, fund="A", kind="holder")
        self.assertTrue(b_lines)
        self.assertTrue(all(d["status"] == "held" for d in b_lines))
        self.assertTrue(all(d["status"] == "payable" for d in a_lines))

    def test_interim_rule_allows_own_undisputed_share(self):
        # A 若被封存，约定允许先付自身已确定份额。
        self.ledger.apply_fact(
            "budget", {"fund": "A", "state": "held"},
            sequence=5, received_at=29)
        decisions, _ = self.ledger.decisions()
        a_lines = by_key(decisions, fund="A", kind="holder")
        self.assertTrue(all(d["status"] == "payable" for d in a_lines))

    def test_advance_requires_explicit_agreement(self):
        # B 封存且 A 明确不能代垫 -> 不得垫付。
        self.assertEqual(
            self.ledger._advance_fund_for("B"), None)
        # 若另有预算明确约定可代垫，则生成 advance_payable。
        self.ledger.interim_funding["A"]["can_advance_for_B"] = True
        decisions, _ = self.ledger.decisions()
        b_lines = by_key(decisions, fund="B", kind="holder")
        self.assertTrue(
            all(d["status"] == "advance_payable"
                and d["advance_from"] == "A" for d in b_lines))

    def test_submitted_payment_occupies_but_is_not_spent(self):
        self.ledger.submit_payment("p-1", "hh-5", 1000, "batch-1",
                                   fund="A", claim_id="claim-18")
        view = self.ledger.treasury_view()
        self.assertEqual(view["funds"]["A"]["occupied_cents"], 1000)
        self.assertEqual(view["funds"]["A"]["spent_cents"], 0)
        self.assertEqual(view["funds"]["A"]["remaining_cents"], 22000)
        # 回执确认后才转为支出。
        self.ledger.confirm_transfer("p-1", "paid", 1000)
        view = self.ledger.treasury_view()
        self.assertEqual(view["funds"]["A"]["spent_cents"], 1000)
        self.assertEqual(view["funds"]["A"]["remaining_cents"], 22000)

    def test_held_money_is_not_repromised_to_other_land(self):
        decisions, _ = self.ledger.decisions()
        held_cents = sum(
            d["cents"] - d["applied_cents"]
            for d in decisions if d["status"] == "held")
        reserved = self.ledger.treasury_view()["claims_reserved"]
        self.assertEqual(reserved["held:B"], held_cents)
        self.assertTrue(any(c["type"] == "fund_held"
                            for c in self.ledger.cases()))


class PayeeChangeTest(unittest.TestCase):
    def _ledger_with_change(self):
        ledger = build_ledger()
        ledger.apply_fact(
            "register",
            {"subject": "hh-4", "fraction": "1/2",
             "replaces": "hh-1", "valid_from": 15},
            sequence=4, received_at=34)
        return ledger

    def test_unissued_money_follows_new_holder_automatically(self):
        ledger = self._ledger_with_change()
        decisions, _ = ledger.decisions()
        hh4 = by_key(decisions, subject="hh-4", kind="holder")
        hh1 = by_key(decisions, subject="hh-1", kind="holder")
        # hh-1 只保留 12-14 三天权益；hh-4 取得 15-30 共 16 天。
        self.assertGreater(sum(d["cents"] for d in hh4),
                           sum(d["cents"] for d in hh1))
        subtypes = {c.get("subtype") for c in ledger.cases()
                    if c["type"] == "payee_change"}
        self.assertIn("unissued_reassigned", subtypes)

    def test_submitted_uncleared_money_is_traced_not_repaid(self):
        ledger = self._ledger_with_change()
        ledger.submit_payment("out-x", "hh-1", 9000, "batch-9",
                              fund="A", claim_id="claim-18")
        cases = ledger.cases()
        trace = [c for c in cases if c.get("subtype") == "trace_pending"]
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["payments"], ["out-x"])
        # 未查清前不得把同一笔钱再许给 hh-4（hh-4 的 A 份额仍挂起）。
        decisions, _ = ledger.decisions()
        hh4_a = by_key(decisions, subject="hh-4", fund="A")
        # 已提交款占用了 hh-1 名下匹配行；hh-4 是不同主体，其款仍可付，
        # 但针对 out-x 本身没有第二笔重复支付。
        self.assertEqual(
            [d for d in hh4_a if d["applied_cents"]], [])

    def test_confirmed_receipt_drives_recover_and_topup(self):
        ledger = self._ledger_with_change()
        # 模拟资料台事实：9100 已提交，随后回执确认入账。
        ledger.apply_fact(
            "treasury",
            {"id": "out-4", "to": "hh-1", "cents": 9100,
             "submitted": True, "fund": "A",
             "claim_id": "claim-18", "batch": "batch-1"},
            sequence=1, received_at=22)
        ledger.confirm_transfer("out-4", "paid", 9100)
        cases = ledger.cases()
        recover = [c for c in cases
                   if c.get("subtype") == "recover_return"]
        topup = [c for c in cases if c.get("subtype") == "topup"]
        self.assertTrue(recover and topup)
        # 超出当前权益匹配的部分进入退回案件；追补为新权益人当前
        # 即可发放的未付份额（本场景 B 未封存，故 A、B 均计入）。
        decisions, usage = ledger.decisions()
        recover_cents = usage["out-4"]["unapplied"]
        self.assertEqual(recover[0]["cents"], recover_cents)
        payable_topup = sum(
            d["cents"] - d["applied_cents"] for d in decisions
            if d["kind"] == "holder" and d["subject"] == "hh-4"
            and d["status"] in ("payable", "advance_payable"))
        self.assertEqual(topup[0]["cents"], payable_topup)
        self.assertGreater(topup[0]["cents"], 0)

    def test_receipt_never_changes_rights(self):
        ledger = self._ledger_with_change()
        ledger.apply_fact(
            "treasury",
            {"id": "out-9", "to": "hh-1", "cents": 5000,
             "submitted": True},
            sequence=7, received_at=40)
        ledger.confirm_transfer("out-9", "paid", 5000)
        subjects = {r["subject"] for r in ledger.rights_intervals()}
        # 回执不会创造或修改任何权益人。
        self.assertNotIn("out-9", subjects)
        self.assertIn("hh-4", subjects)


class IdentityCorrectionTest(unittest.TestCase):
    def test_correction_changes_future_payments_only(self):
        # 用初始即已证的 hh-2 构造纯粹的“确权后更正”场景。
        register = {
            **REGISTER,
            "share": [
                {"subject": "hh-1", "fraction": "1/2", "verified": True},
                {"subject": "hh-2", "fraction": "1/2", "verified": True},
            ],
        }
        ledger = build_ledger(register)
        ledger.apply_fact(
            "register",
            {"subject": "hh-3", "fraction": "1/2",
             "replaces": "hh-2", "valid_from": 20},
            sequence=2, received_at=26)
        intervals = ledger.rights_intervals("hh-2")
        self.assertEqual(intervals[0]["span"], [1, 19])
        hh3 = ledger.rights_intervals("hh-3")
        self.assertEqual(hh3[0]["span"], [20, None])

    def test_payment_before_correction_stays_but_is_flagged(self):
        ledger = build_ledger()
        ledger.submit_payment("p-old", "hh-1", 9000, "batch-1",
                              fund="A", claim_id="claim-18")
        ledger.confirm_transfer("p-old", "paid", 9000)
        ledger.apply_fact(
            "register",
            {"subject": "hh-4", "fraction": "1/2",
             "replaces": "hh-1", "valid_from": 15},
            sequence=4, received_at=34)
        # 已到账部分不自动冲账，而是生成经办追补/退回案件。
        cases = ledger.cases()
        self.assertTrue(any(c.get("subtype") == "recover_return"
                            for c in cases))
        view = ledger.treasury_view()
        # 支出记录保留，财政仍能沿批次核对。
        self.assertEqual(view["funds"]["A"]["spent_cents"], 9000)


class EscrowReconcileTest(unittest.TestCase):
    def test_partner_escrow_record_compared_to_ledger(self):
        ledger = build_ledger()
        ledger.record_escrow("L-50", 7600, "held")
        cases = ledger.cases()
        diff = [c for c in cases if c["type"] == "escrow_reconcile"]
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["difference_cents"], 7600 - 9000)

    def test_exact_escrow_record_closes_reconcile_case(self):
        ledger = build_ledger()
        ledger.record_escrow("L-50", 9000, "held")
        self.assertFalse(
            [c for c in ledger.cases()
             if c["type"] == "escrow_reconcile"])

    def test_multiple_disputed_slots_aggregate_one_escrow_case(self):
        import copy
        register = copy.deepcopy(REGISTER)
        # 两个槽位均未证：整块地 18000 都应提存，但只生成一个土地级案件。
        register["share"][0]["verified"] = False
        ledger = build_ledger(register)
        held_cases = [c for c in ledger.cases()
                      if c["type"] == "escrow_held"]
        self.assertEqual(len(held_cases), 1)
        self.assertEqual(held_cases[0]["cents"], 18000)


class ViewsTest(unittest.TestCase):
    def test_household_view_sees_only_own_affairs(self):
        ledger = build_ledger()
        view = ledger.household_view("hh-2")
        # 待证住户能看到自己涉及的争议（提存）金额。
        self.assertEqual(view["escrowed_cents"], 9000)
        self.assertEqual(view["confirmed_cents"], 0)
        self.assertTrue(any(c["type"] == "escrow_held"
                            for c in view["cases"]))

    def test_treasury_batch_groups_payments_and_budget(self):
        ledger = build_ledger()
        ledger.submit_payment("p1", "hh-1", 4000, "batch-7", fund="A")
        ledger.submit_payment("p2", "hh-1", 2000, "batch-7", fund="A")
        ledger.confirm_transfer("p1", "paid", 4000)
        view = ledger.treasury_view("batch-7")
        self.assertEqual(set(view["batches"]), {"batch-7"})
        self.assertEqual(len(view["batches"]["batch-7"]), 2)
        self.assertEqual(view["funds"]["A"]["spent_cents"], 4000)
        self.assertEqual(view["funds"]["A"]["occupied_cents"], 6000)

    def test_operator_view_contains_open_cases(self):
        ledger = build_ledger()
        view = ledger.operator_view()
        self.assertIn("decisions", view)
        self.assertTrue(any(c["type"] == "escrow_held"
                            for c in view["cases"]))

    def test_confirmed_external_payment_without_fund_still_counts(self):
        # 外部回执事实可能不注明预算：确认后按匹配明细归集支出，
        # 匹配不上的部分单列 unassigned，绝不凭空改变权益。
        ledger = build_ledger()
        ledger.apply_fact(
            "treasury",
            {"id": "out-ext", "to": "hh-1", "cents": 9100,
             "submitted": True, "batch": "batch-ext"},
            sequence=1, received_at=22)
        ledger.confirm_transfer("out-ext", "paid", 9100)
        view = ledger.treasury_view()
        self.assertGreaterEqual(view["funds"]["A"]["spent_cents"], 0)
        self.assertGreater(view["unassigned_spent_cents"], 0)
        # 9100 = 匹配到 A/B 权益行的部分 + 无预算归属部分。
        total = (sum(f["spent_cents"] for f in view["funds"].values())
                 + view["unassigned_spent_cents"])
        self.assertEqual(total, 9100)


if __name__ == "__main__":
    unittest.main()
