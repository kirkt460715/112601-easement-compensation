"""按到达事实重建的补偿台账。

台账只保存事实（权益材料、预算状态、支付提交与回执、提存记录、申请），
所有“当前可付多少、谁在争议中、预算还剩多少”的结论都由这些事实重新计算，
因此晚到的证明可以安全地改变结论，而不会留下互相矛盾的中间状态。

关键边界：

- 支付回执只记录钱去了哪里，**永不**修改权益份额；份额只由确权材料改变。
- 已提交但无回执的钱只是“占用预算”，不是“已经花出”；封存预算下的未决
  资金也不会被重新许给另一块地。
- 待证份额的钱进入提存，确定份额的款项照常发放，不会从无争议住户的
  款项里扣留差额。
"""

from collections import defaultdict
from copy import deepcopy
from fractions import Fraction

from .domain import (
    allocate_cents,
    day_count,
    overlap_days,
    parse_fraction,
    split_claim,
)

DAY_ONE = 1
PAYMENT_STATES = ("submitted", "paid", "failed")


class Ledger:
    def __init__(self):
        self.land = None
        self.funds = {}
        self.allocation = {}
        self.interim_funding = {}
        self.agreements = []
        self.claims = []
        self.events = []
        # 权益“槽位”：初始份额及其后的替换链。
        self._slots = []
        self._fund_state = {}          # fund -> {"state": open/held, "since": ...}
        self.payments = {}             # payment id -> 支付事实
        self.payment_order = []
        self.escrow_records = []       # 提存账户方的独立记录
        self._next_seq = 1

    # ------------------------------------------------------------------ 载入
    def load_register(self, register):
        """载入合成台账中的初始登记材料。"""
        # 深拷贝避免台账与外部对象共享可变结构（测试间数据隔离也依赖它）。
        register = deepcopy(register)
        self.land = register["land"]
        self.funds = dict(register["funds"])
        self.allocation = dict(register.get("allocation", {}))
        self.interim_funding = dict(register.get("interim_funding", {}))
        self.agreements = [dict(a) for a in register["agreements"]]
        for share in register["share"]:
            slot = {
                "id": f"slot-{share['subject']}",
                "chain": [
                    {
                        "subject": share["subject"],
                        "fraction": parse_fraction(share["fraction"]),
                        "valid_from": DAY_ONE,
                        "verified": bool(share.get("verified", False)),
                        "source": "register",
                        "sequence": 0,
                    }
                ],
            }
            self._slots.append(slot)
        for claim in register.get("claims", []):
            self.add_claim(claim, verified=True, source="register")

    def add_claim(self, claim, verified=True, source="household"):
        """受理一笔补偿申请。住户提交且未经台账确认的标记为待证。"""
        claim = dict(claim)
        claim.setdefault("id", f"claim-{len(self.claims) + 1}")
        claim["verified"] = verified
        claim["source"] = source
        if claim.get("land") != self.land:
            raise ValueError("申请土地与台账不一致")
        if any(c["id"] == claim["id"] for c in self.claims):
            raise ValueError(f"申请重复: {claim['id']}")
        self.claims.append(claim)
        return claim

    def add_agreement(self, agreement, verified=False, source="household"):
        agreement = dict(agreement)
        agreement["verified"] = verified
        agreement["source"] = source
        self.agreements.append(agreement)
        return agreement

    # -------------------------------------------------------------- 事实接入
    def apply_fact(self, source, facts, sequence=None, received_at=None):
        """按来源路由一条到达事实。返回该事实产生的台账变更说明。"""
        seq = sequence if sequence is not None else self._next_seq
        self._next_seq = max(self._next_seq, seq + 1)
        event = {
            "sequence": seq,
            "received_at": received_at,
            "source": source,
            "facts": facts,
        }
        self.events.append(event)
        if source == "register":
            return self._apply_register_fact(facts, seq, received_at)
        if source == "budget":
            return self._apply_budget_fact(facts, seq, received_at)
        if source == "treasury":
            return self._apply_treasury_fact(facts, seq, received_at)
        if source == "escrow":
            return self._apply_escrow_fact(facts, seq, received_at)
        if source == "household":
            return self._apply_household_fact(facts, seq, received_at)
        raise ValueError(f"未知事实来源: {source}")

    def _apply_register_fact(self, facts, seq, received_at):
        # 确权材料：权益更正/确认，verified=True，按生效日截断原权益人。
        if "replaces" in facts:
            entry = {
                "subject": facts["subject"],
                "fraction": parse_fraction(facts["fraction"]),
                "valid_from": facts["valid_from"],
                "verified": True,
                "source": "register",
                "sequence": seq,
            }
            slot = self._find_slot(facts["replaces"])
            slot["chain"].append(entry)
            return {"kind": "share_replacement", "slot": slot["id"], **entry}
        # 也可能是对现有待证份额的直接确认（无替换）。
        slot = self._find_slot(facts["subject"])
        slot["chain"].append(
            {
                "subject": facts["subject"],
                "fraction": parse_fraction(facts["fraction"]),
                "valid_from": facts.get("valid_from", DAY_ONE),
                "verified": True,
                "source": "register",
                "sequence": seq,
            }
        )
        return {"kind": "share_confirmation", "slot": slot["id"]}

    def _apply_budget_fact(self, facts, seq, received_at):
        fund = facts["fund"]
        if fund not in self.funds:
            raise ValueError(f"未知预算: {fund}")
        self._fund_state[fund] = {
            "state": facts["state"],
            "since": received_at,
            "sequence": seq,
        }
        return {"kind": "fund_state", "fund": fund, "state": facts["state"]}

    def _apply_treasury_fact(self, facts, seq, received_at):
        payment_id = facts["id"]
        if payment_id in self.payments:
            raise ValueError(f"支付编号重复: {payment_id}")
        payment = {
            "id": payment_id,
            "to": facts["to"],
            "cents": facts["cents"],
            "state": "submitted",          # 已提交，去向尚未查清
            "confirmed": False,
            "batch": facts.get("batch", f"external-{seq}"),
            "fund": facts.get("fund"),
            "claim_id": facts.get("claim_id"),
            "source": "treasury",
            "sequence": seq,
            "received_at": received_at,
        }
        self.payments[payment_id] = payment
        self.payment_order.append(payment_id)
        return {"kind": "payment_submitted", "payment": payment_id}

    def _apply_escrow_fact(self, facts, seq, received_at):
        record = dict(facts)
        record.update({"sequence": seq, "received_at": received_at})
        self.escrow_records.append(record)
        return {"kind": "escrow_record", "record": record}

    def _apply_household_fact(self, facts, seq, received_at):
        # 住户自述的权益/协议先受理，但在确权材料确认前一律 unverified：
        # 只产生待证标记，不能改变现有权益人的区间。
        kind = facts.get("kind")
        if kind == "share":
            entry = {
                "subject": facts["subject"],
                "fraction": parse_fraction(facts["fraction"]),
                "valid_from": facts["valid_from"],
                "verified": False,
                "source": "household",
                "sequence": seq,
                "replaces": facts.get("replaces"),
            }
            if entry["replaces"]:
                slot = self._find_slot(entry["replaces"])
            else:
                slot = self._find_slot(entry["subject"])
            slot["chain"].append(entry)
            return {"kind": "share_claim_pending", "slot": slot["id"]}
        if kind == "agreement":
            return {"kind": "agreement_pending",
                    "agreement": self.add_agreement(facts["agreement"],
                                                    verified=False,
                                                    source="household")["id"]}
        if kind == "claim":
            claim = self.add_claim(facts["claim"], verified=False,
                                   source="household")
            return {"kind": "claim_pending", "claim": claim["id"]}
        raise ValueError("住户事实需注明 kind: share/agreement/claim")

    def _find_slot(self, subject):
        for slot in self._slots:
            if any(entry["subject"] == subject for entry in slot["chain"]):
                return slot
        raise ValueError(f"找不到权益人所在份额槽位: {subject}")

    # ------------------------------------------------------------ 支付与回执
    def submit_payment(self, payment_id, to, cents, batch, fund=None,
                       claim_id=None):
        """经办端发起一笔支付：提交即占用预算，回执前不算支出。"""
        if payment_id in self.payments:
            raise ValueError(f"支付编号重复: {payment_id}")
        payment = {
            "id": payment_id,
            "to": to,
            "cents": cents,
            "state": "submitted",
            "confirmed": False,
            "batch": batch,
            "fund": fund,
            "claim_id": claim_id,
            "source": "operator",
            "sequence": self._next_seq,
        }
        self._next_seq += 1
        self.payments[payment_id] = payment
        self.payment_order.append(payment_id)
        return payment

    def confirm_transfer(self, transfer_id, state, cents):
        """登记合作方（财政）回执。回执只证实钱的去向，不设定权益。"""
        payment = self.payments.get(transfer_id)
        if payment is None:
            raise ValueError(f"回执找不到对应支付: {transfer_id}")
        if state not in PAYMENT_STATES:
            raise ValueError(f"未知支付状态: {state}")
        if cents != payment["cents"]:
            raise ValueError("回执金额与提交金额不一致")
        payment["state"] = state
        payment["confirmed"] = state == "paid"
        return payment

    def record_escrow(self, land, cents, state, sequence=None,
                      received_at=None):
        record = {"land": land, "cents": cents, "state": state,
                  "sequence": sequence, "received_at": received_at}
        self.escrow_records.append(record)
        return record

    # ------------------------------------------------------------- 权益区间
    def rights_intervals(self, subject=None):
        """当前权益区间。verified=False 的条目是待证主张，不截断原权益人。"""
        result = []
        for slot in self._slots:
            verified_chain = [e for e in slot["chain"] if e["verified"]]
            for index, entry in enumerate(verified_chain):
                end = self._slot_end(verified_chain, index)
                if subject is None or entry["subject"] == subject:
                    result.append({
                        "slot": slot["id"],
                        "subject": entry["subject"],
                        "fraction": str(entry["fraction"]),
                        "span": [entry["valid_from"], end],
                        "verified": True,
                        "source": entry["source"],
                    })
            for entry in slot["chain"]:
                if not entry["verified"] and (subject is None or
                                              entry["subject"] == subject):
                    result.append({
                        "slot": slot["id"],
                        "subject": entry["subject"],
                        "fraction": str(entry["fraction"]),
                        "span": [entry["valid_from"], None],
                        "verified": False,
                        "source": entry["source"],
                    })
        return result

    @staticmethod
    def _slot_end(verified_chain, index):
        if index + 1 < len(verified_chain):
            return verified_chain[index + 1]["valid_from"] - 1
        return None  # 开放区间

    def _holder_at(self, slot, day):
        """某日该槽位的已确认权益人；无已确认条目时返回 None。"""
        holder = None
        for entry in slot["chain"]:
            if entry["verified"] and entry["valid_from"] <= day:
                holder = entry
        return holder

    def _is_disputed(self, slot, day):
        """该槽位在某日是否处于待证状态。

        两种情形：当日没有任何已确认权益人（如初始未证份额），或存在
        一份在当日已生效、但尚未被确权材料证实、且未被更晚的确权条目
        覆盖的住户主张。已被后续确权覆盖的旧待证条目不再构成争议。
        """
        holder = self._holder_at(slot, day)
        if holder is None:
            return True
        latest_verified_from = max(
            entry["valid_from"] for entry in slot["chain"]
            if entry["verified"] and entry["valid_from"] <= day)
        return any(
            not entry["verified"]
            and latest_verified_from <= entry["valid_from"] <= day
            for entry in slot["chain"]
        )

    # ------------------------------------------------------------- 应付计算
    def claim_split(self, claim):
        verified_agreements = [a for a in self.agreements if a.get("verified", True)]
        return split_claim(claim, verified_agreements, self.allocation)

    def entitlement_lines(self, claim):
        """把一笔申请拆成逐权益人/逐预算的明细行与提存行。"""
        split = self.claim_split(claim)
        period = claim["period"]
        total_days = day_count(period)

        # 逐日聚合权重：holder_weights[(槽位, 主体)] / escrow_weight[槽位]，
        # 均为精确分数。保留槽位以便住户视图只显示与本人相关的提存。
        holder_weights = defaultdict(Fraction)
        escrow_weights = defaultdict(Fraction)
        day_notes = []
        for offset in range(total_days):
            day = period[0] + offset
            for slot in self._slots:
                fraction = self._slot_fraction(slot, day)
                weight = fraction / total_days
                if self._is_disputed(slot, day):
                    escrow_weights[slot["id"]] += weight
                    day_notes.append({"day": day, "slot": slot["id"],
                                      "outcome": "escrow"})
                else:
                    holder = self._holder_at(slot, day)
                    holder_weights[(slot["id"], holder["subject"])] += weight
                    day_notes.append({"day": day, "slot": slot["id"],
                                      "outcome": holder["subject"]})

        keys = sorted(holder_weights)
        weights = {**{k: holder_weights[k] for k in keys},
                   **{("__escrow__", slot_id): weight
                      for slot_id, weight in escrow_weights.items()}}
        lines = []
        for fund, cents in sorted(split["by_fund"].items()):
            allocated = allocate_cents(weights, cents)
            for slot_id, subject in keys:
                if allocated[(slot_id, subject)]:
                    lines.append({
                        "claim_id": claim["id"],
                        "fund": fund,
                        "slot": slot_id,
                        "subject": subject,
                        "cents": allocated[(slot_id, subject)],
                        "kind": "holder",
                    })
            for slot_id, weight in escrow_weights.items():
                key = ("__escrow__", slot_id)
                if allocated[key]:
                    lines.append({
                        "claim_id": claim["id"],
                        "fund": fund,
                        "slot": slot_id,
                        "subject": None,
                        "cents": allocated[key],
                        "kind": "escrow",
                    })
        # 无协议覆盖的金额同样按权益/提存拆分，但没有预算可付。
        if split["uncovered"]:
            allocated = allocate_cents(weights, split["uncovered"])
            for slot_id, subject in keys:
                if allocated[(slot_id, subject)]:
                    lines.append({
                        "claim_id": claim["id"], "fund": None,
                        "slot": slot_id, "subject": subject,
                        "cents": allocated[(slot_id, subject)],
                        "kind": "uncovered",
                    })
            for slot_id, weight in escrow_weights.items():
                key = ("__escrow__", slot_id)
                if allocated[key]:
                    lines.append({
                        "claim_id": claim["id"], "fund": None,
                        "slot": slot_id, "subject": None,
                        "cents": allocated[key],
                        "kind": "uncovered_escrow",
                    })
        return lines, split, day_notes

    def _slot_fraction(self, slot, day):
        fraction = slot["chain"][0]["fraction"]
        for entry in slot["chain"]:
            if entry["valid_from"] <= day:
                fraction = entry["fraction"]
        return fraction

    def all_lines(self):
        lines = []
        for claim in self.claims:
            claim_lines, _, _ = self.entitlement_lines(claim)
            lines.extend(claim_lines)
        return lines

    # ------------------------------------------------------------- 支付匹配
    def _apply_payments(self, lines):
        """把支付按权益人+预算 FIFO 匹配到明细行，重算每次快照都重做。"""
        applied = defaultdict(int)       # 行索引 -> 已匹配金额
        payment_usage = {pid: {"applied": 0, "lines": []}
                         for pid in self.payment_order}
        open_lines = [dict(line, index=i) for i, line in enumerate(lines)
                      if line["kind"] == "holder"]
        for pid in self.payment_order:
            payment = self.payments[pid]
            remaining = payment["cents"]
            candidates = [
                line for line in open_lines
                if line["subject"] == payment["to"]
                and (not payment.get("fund") or line["fund"] == payment["fund"])
                and (not payment.get("claim_id")
                     or line["claim_id"] == payment["claim_id"])
            ]
            for line in candidates:
                if remaining <= 0:
                    break
                line_total = line["cents"]
                already = applied[line["index"]]
                room = line_total - already
                if room <= 0:
                    continue
                take = min(room, remaining)
                applied[line["index"]] += take
                payment_usage[pid]["applied"] += take
                payment_usage[pid]["lines"].append(
                    {"claim_id": line["claim_id"], "fund": line["fund"],
                     "subject": line["subject"], "cents": take})
                remaining -= take
            payment_usage[pid]["unapplied"] = remaining
        return applied, payment_usage

    def decisions(self):
        """当前可付决定：每行的状态、可付额与原因。"""
        lines = self.all_lines()
        applied, payment_usage = self._apply_payments(lines)
        occupied = self._occupied_budget(payment_usage)
        result = []
        for index, line in enumerate(lines):
            decision = dict(line)
            decision["applied_cents"] = applied[index]
            if line["kind"] in ("escrow", "uncovered_escrow"):
                decision["status"] = "escrow"
                decision["reason"] = "份额待证，款项提存"
            elif line["kind"] == "uncovered":
                decision["status"] = "unfunded"
                decision["reason"] = "申请期间无有效协议覆盖，无预算承担"
            else:
                self._decide_funded_line(decision, occupied)
            result.append(decision)
        return result, payment_usage

    def _decide_funded_line(self, decision, occupied):
        fund = decision["fund"]
        state = self._fund_state.get(fund, {}).get("state", "open")
        rule = self.interim_funding.get(fund, {})
        remaining = self.funds[fund] - occupied.get(fund, 0)
        unpaid = decision["cents"] - decision["applied_cents"]
        decision["budget_remaining_before"] = remaining
        if state == "held":
            own_ok = rule.get("can_pay_own_undisputed_share", False)
            advance_fund = self._advance_fund_for(fund)
            if own_ok:
                decision["status"], decision["reason"] = (
                    "payable", "预算封存但约定可先付本预算已确定份额")
            elif advance_fund is not None:
                decision["status"], decision["reason"] = (
                    "advance_payable",
                    f"预算封存，由 {advance_fund} 依约定先行垫付")
                decision["advance_from"] = advance_fund
                return
            else:
                decision["status"], decision["reason"] = (
                    "held", "预算封存且无先行承担约定，未决资金保留不另作他用")
                return
        if remaining < unpaid:
            decision["status"] = "blocked_budget"
            decision["reason"] = "预算余额不足"
        else:
            decision.setdefault("status", "payable")
            decision.setdefault("reason", "权益与预算均已确定，可发放")

    def _advance_fund_for(self, held_fund):
        for fund, rule in self.interim_funding.items():
            if fund == held_fund:
                continue
            target = rule.get(f"can_advance_for_{held_fund}")
            if target:
                return fund
        return None

    def _occupied_budget(self, payment_usage):
        """已提交（含未回执）支付所占用的预算，按匹配到的预算归集。"""
        occupied = defaultdict(int)
        for pid, usage in payment_usage.items():
            payment = self.payments[pid]
            for line in usage["lines"]:
                if line["fund"]:
                    occupied[line["fund"]] += line["cents"]
            if usage["unapplied"] and payment.get("fund"):
                occupied[payment["fund"]] += usage["unapplied"]
        return dict(occupied)

    # ----------------------------------------------------------------- 争议
    def cases(self):
        """村级经办需要继续处理的全部争议案件。"""
        cases = []
        decisions, usage = self.decisions()
        cases.extend(self._share_change_cases(decisions, usage))
        cases.extend(self._escrow_cases(decisions))
        cases.extend(self._unapplied_cases(usage))
        cases.extend(self._held_fund_cases(decisions))
        cases.extend(self._unfunded_cases(decisions))
        cases.sort(key=lambda c: (c.get("sequence", 0), c["type"]))
        return cases

    def _share_change_cases(self, decisions, usage):
        cases = []
        # 找到所有“替换”事实，区分未发出、已提交未查清、已确认入账。
        for slot in self._slots:
            replacements = [e for e in slot["chain"]
                            if e["source"] in ("register", "household")
                            and e is not slot["chain"][0]]
            for entry in replacements:
                if not entry["verified"]:
                    cases.append({
                        "type": "share_pending",
                        "slot": slot["id"],
                        "claimant": entry["subject"],
                        "valid_from": entry["valid_from"],
                        "sequence": entry["sequence"],
                        "detail": "住户权益主张待确权材料证实，对应份额提存",
                    })
                    continue
                predecessor = self._predecessor(slot, entry)
                related_payments = [
                    pid for pid in self.payment_order
                    if self.payments[pid]["to"] == predecessor
                ]
                unissued, tracing, confirmed = [], [], []
                for pid in related_payments:
                    payment = self.payments[pid]
                    if not payment["confirmed"]:
                        if payment["state"] == "submitted":
                            tracing.append(pid)
                        else:
                            unissued.append(pid)
                    else:
                        confirmed.append(pid)
                base = {
                    "type": "payee_change",
                    "slot": slot["id"],
                    "predecessor": predecessor,
                    "successor": entry["subject"],
                    "valid_from": entry["valid_from"],
                    "sequence": entry["sequence"],
                }
                if unissued:
                    cases.append({**base, "subtype": "unissued_reassigned",
                                  "payments": unissued,
                                  "detail": "款未实际发出，停止向原权益人支付，"
                                            "按新权益人重新办理"})
                if tracing:
                    cases.append({**base, "subtype": "trace_pending",
                                  "payments": tracing,
                                  "detail": "款已提交但去向未查清，先追查再划转"})
                if confirmed:
                    # 已确认入账的钱：按当前权益能匹配上的部分留账，
                    # 匹配不上的部分（含原权益人已不再享有的期间）
                    # 由经办查原款另办退回。
                    recover_cents = sum(
                        usage[pid]["unapplied"] for pid in confirmed)
                    if recover_cents:
                        cases.append({**base, "subtype": "recover_return",
                                      "payments": confirmed,
                                      "cents": recover_cents,
                                      "detail": "已确认入账且超出当前权益，"
                                                "由经办查原款另办退回"})
                    topup = self._successor_topup(decisions, entry)
                    if topup:
                        cases.append({**base, "subtype": "topup",
                                      "cents": topup,
                                      "detail": "新权益人已确定但款项尚未补足，"
                                                "另办追补"})
                if not related_payments:
                    # 未发出的款无需追款，台账已自动改判给新权益人。
                    cases.append({**base, "subtype": "unissued_reassigned",
                                  "detail": "对应款项尚未发出，已按新权益人办理"})
        return cases

    def _predecessor(self, slot, entry):
        idx = slot["chain"].index(entry)
        for earlier in reversed(slot["chain"][:idx]):
            if earlier["verified"]:
                return earlier["subject"]
        return slot["chain"][0]["subject"]

    def _successor_topup(self, decisions, entry):
        # 立即追补只含当前即可发放的部分；封存/余额不足的份额已由
        # fund_held 等案件单独跟踪，不能把未决资金视作已可重付。
        return sum(
            line["cents"] - line["applied_cents"]
            for line in decisions
            if line["kind"] == "holder"
            and line["subject"] == entry["subject"]
            and line["status"] in ("payable", "advance_payable")
        )

    def _escrow_cases(self, decisions):
        wanted = defaultdict(int)
        for line in decisions:
            if line["kind"] in ("escrow", "uncovered_escrow"):
                wanted[self.land] += line["cents"]
        recorded = defaultdict(int)
        for record in self.escrow_records:
            if record.get("state") == "held":
                recorded[record["land"]] += record["cents"]
        cases = []
        # 按土地只生成一个提存案件，多槽位提存先聚合。
        for land in sorted(set(wanted) | set(recorded)):
            cents = wanted.get(land, 0)
            if cents:
                cases.append({
                    "type": "escrow_held", "land": land, "cents": cents,
                    "detail": "待证份额对应款项应提存",
                })
            diff = recorded.get(land, 0) - cents
            if diff != 0:
                cases.append({
                    "type": "escrow_reconcile", "land": land,
                    "recorded_cents": recorded.get(land, 0),
                    "expected_cents": cents,
                    "difference_cents": diff,
                    "detail": "提存账户记录与台账应提存金额不一致，需经办核对",
                })
        return cases

    def _unapplied_cases(self, usage):
        cases = []
        for pid, info in usage.items():
            if info["unapplied"]:
                payment = self.payments[pid]
                cases.append({
                    "type": "unapplied_payment", "payment": pid,
                    "subject": payment["to"],
                    "cents": info["unapplied"],
                    "confirmed": payment["confirmed"],
                    "detail": "到账款超出当前权益（或权益人已变更），"
                              "回执不改变权益，由经办另办退回/追补",
                })
        return cases

    def _held_fund_cases(self, decisions):
        seen = set()
        cases = []
        for line in decisions:
            if line["status"] == "held":
                key = (line["fund"], line["claim_id"], line["subject"])
                if key in seen:
                    continue
                seen.add(key)
                cents = line["cents"] - line["applied_cents"]
                if cents <= 0:
                    continue
                cases.append({
                    "type": "fund_held", "fund": line["fund"],
                    "claim_id": line["claim_id"], "subject": line["subject"],
                    "cents": cents,
                    "detail": "预算封存，已确定份额保留待解封，不重新许给他人",
                })
        return cases

    def _unfunded_cases(self, decisions):
        cases = []
        for line in decisions:
            if line["kind"] in ("uncovered", "uncovered_escrow"):
                cases.append({
                    "type": "uncovered_period",
                    "claim_id": line["claim_id"],
                    "subject": line["subject"],
                    "cents": line["cents"],
                    "escrow": line["kind"] == "uncovered_escrow",
                    "detail": "申请期间无有效协议覆盖，需经办补录协议",
                })
        return cases

    # ------------------------------------------------------------------ 视图
    def household_view(self, subject):
        """住户自查：权益区间、已入账、等待办理、提存、争议。"""
        decisions, usage = self.decisions()
        view = {
            "subject": subject,
            "rights": self.rights_intervals(subject),
            "entitled_cents": 0,
            "confirmed_cents": 0,
            "submitted_pending_cents": 0,
            "payable_unsubmitted_cents": 0,
            "escrowed_cents": 0,
            "lines": [],
            "cases": self._cases_for_subject(subject),
        }
        for line in decisions:
            if line["kind"] == "holder" and line["subject"] == subject:
                view["entitled_cents"] += line["cents"]
                applied = line["applied_cents"]
                confirmed = self._confirmed_part(line, usage)
                view["confirmed_cents"] += confirmed
                view["submitted_pending_cents"] += applied - confirmed
                if line["status"] in ("payable", "advance_payable"):
                    view["payable_unsubmitted_cents"] += (
                        line["cents"] - applied)
                view["lines"].append(line)
            if line["kind"] in ("escrow", "uncovered_escrow"):
                # 提存行不属名，但让争议涉及的住户能看到争议金额。
                if self._slot_touches_subject(line, subject):
                    view["escrowed_cents"] += line["cents"]
                    view["lines"].append(line)
        for pid, info in usage.items():
            payment = self.payments[pid]
            if payment["to"] == subject and info["unapplied"]:
                view.setdefault("unapplied_payments", []).append({
                    "payment": pid, "cents": info["unapplied"],
                    "confirmed": payment["confirmed"],
                })
        return view

    def _confirmed_part(self, line, usage):
        total = 0
        for pid, info in usage.items():
            if not self.payments[pid]["confirmed"]:
                continue
            for used in info["lines"]:
                if (used["claim_id"] == line["claim_id"]
                        and used["fund"] == line["fund"]
                        and used["subject"] == line["subject"]):
                    total += used["cents"]
        return total

    def _cases_for_subject(self, subject):
        decisions, _ = self.decisions()
        escrow_lines = [line for line in decisions
                        if line["kind"] in ("escrow", "uncovered_escrow")]
        touches_escrow = any(
            self._slot_touches_subject(line, subject)
            for line in escrow_lines)
        result = []
        for case in self.cases():
            if (case.get("successor") == subject
                    or case.get("predecessor") == subject
                    or case.get("claimant") == subject
                    or case.get("subject") == subject):
                result.append(case)
            elif touches_escrow and case["type"] in (
                    "escrow_held", "escrow_reconcile"):
                result.append(case)
        return result

    def _slot_touches_subject(self, line, subject):
        for slot in self._slots:
            if slot["id"] != line.get("slot"):
                continue
            if not any(e["subject"] == subject for e in slot["chain"]):
                continue
            period = next(c["period"] for c in self.claims
                          if c["id"] == line["claim_id"])
            if any(overlap_days((e["valid_from"], period[1]), period)
                   for e in slot["chain"]):
                return True
        return False

    def treasury_view(self, batch=None):
        """财政沿支付批次核对收支、占用与剩余预算。"""
        decisions, usage = self.decisions()
        occupied = self._occupied_budget(usage)
        spent = defaultdict(int)
        overspent = defaultdict(int)
        unassigned_spent = 0
        for pid, info in usage.items():
            payment = self.payments[pid]
            if not payment["confirmed"]:
                continue
            # 已确认入账即财政支出。优先按付款标注的预算归集；
            # 外部回执未注明预算时，按匹配到的权益明细行归集。
            if payment.get("fund"):
                spent[payment["fund"]] += payment["cents"]
                overspent[payment["fund"]] += info["unapplied"]
            else:
                matched = defaultdict(int)
                for used in info["lines"]:
                    if used["fund"]:
                        matched[used["fund"]] += used["cents"]
                for fund, cents in matched.items():
                    spent[fund] += cents
                if info["unapplied"]:
                    # 无预算归属的已到账款，财政仍需在批次中核对。
                    unassigned_spent += info["unapplied"]
        funds_view = {}
        for fund, opening in self.funds.items():
            funds_view[fund] = {
                "opening_cents": opening,
                "spent_cents": spent.get(fund, 0),
                "overspent_cents": overspent.get(fund, 0),
                "occupied_cents": occupied.get(fund, 0),
                "remaining_cents": opening - occupied.get(fund, 0),
                "state": self._fund_state.get(fund, {}).get("state", "open"),
            }
        batches = defaultdict(list)
        for pid in self.payment_order:
            payment = self.payments[pid]
            if batch and payment["batch"] != batch:
                continue
            batches[payment["batch"]].append({
                "payment": pid, "to": payment["to"],
                "cents": payment["cents"], "state": payment["state"],
                "confirmed": payment["confirmed"],
                "fund": payment.get("fund"),
                "claim_id": payment.get("claim_id"),
                "application": usage[pid]["lines"],
                "unapplied_cents": usage[pid]["unapplied"],
            })
        escrow = [dict(r) for r in self.escrow_records]
        return {
            "funds": funds_view,
            "unassigned_spent_cents": unassigned_spent,
            "batches": dict(batches),
            "escrow_records": escrow,
            "claims_reserved": self._reservation_view(decisions),
        }

    def _reservation_view(self, decisions):
        reserved = defaultdict(int)
        for line in decisions:
            if line["kind"] in ("escrow", "uncovered_escrow"):
                reserved["escrow"] += line["cents"]
            elif line["status"] == "held":
                reserved[f"held:{line['fund']}"] += (
                    line["cents"] - line["applied_cents"])
        return dict(reserved)

    def operator_view(self):
        decisions, usage = self.decisions()
        return {
            "land": self.land,
            "rights": self.rights_intervals(),
            "decisions": decisions,
            "cases": self.cases(),
            "payments": [self.payments[pid] for pid in self.payment_order],
            "escrow_records": [dict(r) for r in self.escrow_records],
            "fund_states": dict(self._fund_state),
        }

    # ---------------------------------------------------------------- 序列化
    def public_state(self):
        decisions, usage = self.decisions()
        return {
            "land": self.land,
            "agreements": self.agreements,
            "claims": self.claims,
            "rights": self.rights_intervals(),
            "decisions": decisions,
            "cases": self.cases(),
            "events": self.events,
        }
