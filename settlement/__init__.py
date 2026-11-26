"""分户补偿与争议提存后端。

模块划分：

- ``domain``：不依赖外部状态的纯规则（期间、份额、协议叠加、金额取整）。
- ``ledger``：按到达事实重建的台账，给出可付决定、提存、争议案件与预算占用。
- ``desk_client``：访问本地资料台（合成台账、分批消息、合作方回执）的客户端。
- ``service``：对住户、村级经办、财政开放的 HTTP 服务。
"""

from .domain import (
    allocate_cents,
    day_count,
    overlap_days,
    parse_fraction,
    split_claim,
)
from .ledger import Ledger

__all__ = [
    "Ledger",
    "allocate_cents",
    "day_count",
    "overlap_days",
    "parse_fraction",
    "split_claim",
]
