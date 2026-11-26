"""纯领域规则。

时间：限制区间使用同一合同周期的相对日，区间均为闭区间 ``[start, end]``。
金额：全部以分为单位的整数计算；份额与分摊比用 :class:`fractions.Fraction`
保留精确值，只有在真正落到某一笔款项时才用最大余数法取整，保证各笔合计
严格等于应付总额，不会凭空多出或丢失一分钱。
"""

from fractions import Fraction


def parse_fraction(text):
    """解析 ``"1/2"`` 这类份额字符串为精确分数。"""
    if isinstance(text, Fraction):
        return text
    return Fraction(text)


def day_count(span):
    """闭区间的天数，``[1, 1]`` 为 1 天。"""
    start, end = span
    if end < start:
        raise ValueError("期间结束日早于开始日")
    return end - start + 1


def overlap_days(span_a, span_b):
    """两个闭区间的重叠天数，不重叠返回 0。"""
    start = max(span_a[0], span_b[0])
    end = min(span_a[1], span_b[1])
    if end < start:
        return 0
    return end - start + 1


def allocate_cents(weights, total):
    """按权重把整数 ``total`` 分拆成整数金额（最大余数法）。

    ``weights`` 为 ``{键: 非负权重}``。零权重键得 0。同余数时按键的
    字符串顺序补位，结果确定、可重复。各份之和恒等于 ``total``。
    """
    if total < 0:
        raise ValueError("分配金额不能为负")
    keys = list(weights)
    positive = [key for key in keys if weights[key] > 0]
    weight_sum = sum((weights[key] for key in positive), Fraction(0))
    if weight_sum == 0:
        if total != 0:
            raise ValueError("权重全为零时无法分配正数金额")
        return {key: 0 for key in keys}
    exact = {
        key: Fraction(total) * weights[key] / weight_sum for key in positive
    }
    floors = {key: int(exact[key]) for key in positive}  # Fraction 正值向下取整
    remainder = total - sum(floors.values())
    # 余数按小数部分从大到小补 1 分；并列时按键名排序，保证确定性。
    order = sorted(
        positive,
        key=lambda key: (-(exact[key] - floors[key]), str(key)),
    )
    for key in order[:remainder]:
        floors[key] += 1
    floors.update({key: 0 for key in keys if key not in floors})
    return floors


def _matching_agreements(claim, agreements):
    """与申请的限制类别相同、且期间有重叠的协议。"""
    return [
        agreement
        for agreement in agreements
        if agreement["restriction"] == claim["restriction"]
        and overlap_days(agreement["span"], claim["period"]) > 0
    ]


def _allocation_key(funds):
    return "_".join(sorted(funds))


def split_claim(claim, agreements, allocation):
    """把一笔补偿申请拆到各预算。

    规则：

    - 只核对限制类别相同、有效期间与申请期间重叠的协议。
    - ``independent``（可叠加）协议：覆盖的每一天都由该预算全额承担。
    - ``shared``（共担）协议：同一天若有多个 shared 协议覆盖，这笔共同
      款项只承担一次，按台账注明的分摊比在各预算间拆分，绝不让两笔
      预算各付全额。
    - 若某天没有任何协议覆盖，该日金额无法落账，计入 ``uncovered``，
      后端不得自行指定预算。
    - 跨日金额先按日均摊，再对每个预算的精确值用最大余数法取整；
      各预算金额与未覆盖金额之和恒等于申请毛额。

    返回 ``{"by_fund": {预算: 分}, "uncovered": 分, "daily": [...]}``，
    ``daily`` 逐天记录承担明细，便于经办核对。
    """
    gross = claim["gross_cents"]
    period = claim["period"]
    total_days = day_count(period)
    if total_days <= 0:
        raise ValueError("申请期间为空")
    rate = Fraction(gross, total_days)
    matched = _matching_agreements(claim, agreements)

    daily = []
    exact_by_fund = {}
    uncovered_exact = Fraction(0)
    for offset in range(total_days):
        day = period[0] + offset
        shared = [
            agreement
            for agreement in matched
            if agreement["stack"] == "shared"
            and agreement["span"][0] <= day <= agreement["span"][1]
        ]
        independent = [
            agreement
            for agreement in matched
            if agreement["stack"] == "independent"
            and agreement["span"][0] <= day <= agreement["span"][1]
        ]
        day_detail = {"day": day, "shared": [], "independent": []}
        if independent:
            # 可叠加协议各自全额承担当天限制。
            for agreement in independent:
                fund = agreement["fund"]
                exact_by_fund[fund] = exact_by_fund.get(fund, Fraction(0)) + rate
                day_detail["independent"].append(
                    {"agreement": agreement["id"], "fund": fund, "cents_weight": 1}
                )
        if shared:
            funds = sorted(agreement["fund"] for agreement in shared)
            day_detail["shared_funds"] = funds
            if len(funds) == 1:
                # 只有一份共担协议覆盖当天，由该预算全额承担。
                fund = funds[0]
                exact_by_fund[fund] = exact_by_fund.get(fund, Fraction(0)) + rate
                day_detail["shared"].append({
                    "agreement": shared[0]["id"], "fund": fund,
                    "ratio": "1",
                })
            else:
                ratios = allocation.get(_allocation_key(funds))
                if ratios is None:
                    raise KeyError(
                        f"缺少共担分摊比例: {_allocation_key(funds)}")
                if len(ratios) != len(funds):
                    raise ValueError("分摊比例与共担预算数量不一致")
                weights = {
                    fund: parse_fraction(ratio)
                    for fund, ratio in zip(funds, ratios)
                }
                weight_sum = sum(weights.values(), Fraction(0))
                for agreement in shared:
                    fund = agreement["fund"]
                    share = rate * weights[fund] / weight_sum
                    exact_by_fund[fund] = exact_by_fund.get(fund, Fraction(0)) + share
                    day_detail["shared"].append(
                        {
                            "agreement": agreement["id"],
                            "fund": fund,
                            "ratio": str(weights[fund]),
                        }
                    )
        if not shared and not independent:
            uncovered_exact += rate
            day_detail["uncovered"] = True
        daily.append(day_detail)

    funds = sorted(exact_by_fund)
    # 预算金额与未覆盖金额一起做最大余数法，保证分毫不差。
    weights = {**{f"fund:{fund}": exact_by_fund[fund] for fund in funds},
               "uncovered": uncovered_exact}
    allocated = allocate_cents(weights, gross)
    return {
        "by_fund": {fund: allocated[f"fund:{fund}"] for fund in funds},
        "uncovered": allocated["uncovered"],
        "daily": daily,
    }
