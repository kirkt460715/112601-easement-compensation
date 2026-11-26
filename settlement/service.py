"""补偿处理后端的 HTTP 服务。

角色与端点：

- 住户（``/household/*``）：提交权益/协议/申请事实，查询本人权益区间、
  已入账、待办与争议金额。
- 村级经办（``/operator/*``）：受理全部事实、发起支付、登记回执、
  处理争议案件、触发与资料台的对账。
- 财政（``/treasury/*``）：按支付批次核对收支、预算占用与剩余。

服务把事实保存在内存台账中；所有结论按需重算，晚到的证明自然改变
当前结论。写操作全部加锁，读操作基于同一把锁取一致快照。
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock
from urllib.parse import parse_qs, urlsplit

from .desk_client import DeskUnavailable
from .ledger import Ledger


class SettlementDesk(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, port=0, desk_client=None):
        self.ledger = Ledger()
        self.desk_client = desk_client
        self.state_lock = Lock()
        self._payment_counter = 0
        self._batch_counter = 0
        super().__init__(("127.0.0.1", port), SettlementHandler)

    # ----------------------------------------------------------- 资料台引导
    def bootstrap_from_desk(self):
        """从资料台读取合成台账并按序应用全部已到达消息。

        可安全重复调用：每次以全新台账重放，避免重复受理同一事实。
        """
        if self.desk_client is None:
            raise RuntimeError("未配置资料台客户端")
        register = self.desk_client.fetch_register()
        with self.state_lock:
            self.ledger = Ledger()
            self.ledger.load_register(register)
            for message in self.desk_client.fetch_messages():
                self.ledger.apply_fact(
                    message["source"], message["facts"],
                    sequence=message["sequence"],
                    received_at=message["received_at"],
                )
        return register

    def reconcile_transfer(self, transfer_id):
        """向财政资料台查一笔支付的实际回执并登记。"""
        if self.desk_client is None:
            raise RuntimeError("未配置资料台客户端")
        fact = self.desk_client.fetch_partner_fact("read-transfer", "treasury")
        with self.state_lock:
            if fact["transfer"] != transfer_id:
                raise ValueError(
                    f"回执 {fact['transfer']} 与待查支付 {transfer_id} 不符")
            return self.ledger.confirm_transfer(
                transfer_id, fact["state"], fact["cents"])

    def reconcile_escrow(self):
        fact = self.desk_client.fetch_partner_fact("read-reserve", "escrow")
        with self.state_lock:
            return self.ledger.record_escrow(
                fact["land"], fact["cents"], fact["state"])

    def next_payment_id(self):
        self._payment_counter += 1
        return f"out-{self._payment_counter}"

    def next_batch_id(self):
        self._batch_counter += 1
        return f"batch-{self._batch_counter}"


class SettlementHandler(BaseHTTPRequestHandler):
    server: SettlementDesk

    def respond(self, status, data):
        payload = json.dumps(data, ensure_ascii=False,
                             default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # ------------------------------------------------------------- 路由总表
    def do_GET(self):
        route = urlsplit(self.path)
        query = parse_qs(route.query, keep_blank_values=True)
        try:
            if route.path == "/state":
                with self.server.state_lock:
                    self.respond(200, self.server.ledger.public_state())
            elif route.path == "/household/view":
                self._household_view(query)
            elif route.path == "/operator/cases":
                with self.server.state_lock:
                    self.respond(200, {"cases": self.server.ledger.cases()})
            elif route.path == "/operator/decisions":
                decisions, _ = self.server.ledger.decisions()
                with self.server.state_lock:
                    self.respond(200, {"decisions": decisions})
            elif route.path == "/operator/rights":
                with self.server.state_lock:
                    self.respond(200, {
                        "rights": self.server.ledger.rights_intervals()})
            elif route.path == "/operator/reconcile":
                self._reconcile(query)
            elif route.path == "/treasury/view":
                batch = query.get("batch", [None])[0]
                with self.server.state_lock:
                    self.respond(200,
                                 self.server.ledger.treasury_view(batch))
            else:
                self.respond(404, {"status": "not_found"})
        except (ValueError, KeyError) as exc:
            self.respond(400, {"status": "bad_request", "error": str(exc)})
        except DeskUnavailable as exc:
            self.respond(503, {"status": "desk_unavailable",
                               "error": str(exc)})

    def do_POST(self):
        route = urlsplit(self.path)
        body = self.read_body()
        if body is None:
            self.respond(400, {"status": "bad_json"})
            return
        try:
            if route.path == "/facts":
                self._accept_fact(body)
            elif route.path == "/claims":
                self._accept_claim(body)
            elif route.path == "/agreements":
                self._accept_agreement(body)
            elif route.path == "/operator/payments":
                self._submit_payment(body)
            elif route.path == "/operator/receipts":
                self._register_receipt(body)
            elif route.path == "/operator/bootstrap":
                self._bootstrap(body)
            else:
                self.respond(404, {"status": "not_found"})
        except (ValueError, KeyError) as exc:
            self.respond(400, {"status": "bad_request", "error": str(exc)})
        except DeskUnavailable as exc:
            self.respond(503, {"status": "desk_unavailable",
                               "error": str(exc)})

    # ------------------------------------------------------------- 住户端点
    def _household_view(self, query):
        subject = query.get("subject", [""])[0]
        if not subject:
            self.respond(400, {"status": "subject_required"})
            return
        with self.server.state_lock:
            if not _known_subject(self.server.ledger, subject):
                self.respond(404, {"status": "unknown_subject"})
                return
            self.respond(200, self.server.ledger.household_view(subject))

    def _accept_fact(self, body):
        source = body.get("source", "household")
        facts = body.get("facts")
        if not isinstance(facts, dict):
            self.respond(400, {"status": "facts_required"})
            return
        with self.server.state_lock:
            change = self.server.ledger.apply_fact(
                source, facts,
                sequence=body.get("sequence"),
                received_at=body.get("received_at"))
        self.respond(201, {"status": "accepted", "change": change})

    def _accept_claim(self, body):
        verified = body.get("source") == "register"
        with self.server.state_lock:
            claim = self.server.ledger.add_claim(
                body["claim"], verified=verified,
                source=body.get("source", "household"))
        self.respond(201, {"status": "accepted", "claim_id": claim["id"]})

    def _accept_agreement(self, body):
        verified = body.get("source") == "register"
        with self.server.state_lock:
            agreement = self.server.ledger.add_agreement(
                body["agreement"], verified=verified,
                source=body.get("source", "household"))
        self.respond(201, {"status": "accepted",
                           "agreement_id": agreement["id"]})

    # ------------------------------------------------------------- 经办端点
    def _submit_payment(self, body):
        required = ("to", "cents")
        missing = [key for key in required if key not in body]
        if missing:
            self.respond(400, {"status": "missing_fields",
                               "fields": missing})
            return
        if not isinstance(body["cents"], int) or body["cents"] < 0:
            self.respond(400, {"status": "bad_cents"})
            return
        with self.server.state_lock:
            payment_id = body.get("id") or self.server.next_payment_id()
            batch = body.get("batch") or self.server.next_batch_id()
            payment = self.server.ledger.submit_payment(
                payment_id, body["to"], body["cents"], batch,
                fund=body.get("fund"), claim_id=body.get("claim_id"))
        self.respond(201, {"status": "submitted", "payment": payment,
                           "batch": batch})

    def _register_receipt(self, body):
        # 经办直接登记外部回执；回执仅证实去向，不改变任何权益。
        with self.server.state_lock:
            payment = self.server.ledger.confirm_transfer(
                body["transfer_id"], body["state"], body["cents"])
        self.respond(200, {"status": "recorded", "payment": payment})

    def _bootstrap(self, body):
        if body.get("include_escrow"):
            register = self.server.bootstrap_from_desk()
            escrow = self.server.reconcile_escrow()
            self.respond(200, {"status": "bootstrapped",
                               "land": register["land"],
                               "escrow": escrow})
        else:
            register = self.server.bootstrap_from_desk()
            self.respond(200, {"status": "bootstrapped",
                               "land": register["land"]})

    def _reconcile(self, query):
        kind = query.get("kind", [""])[0]
        if kind == "transfer":
            transfer_id = query.get("transfer_id", [""])[0]
            payment = self.server.reconcile_transfer(transfer_id)
            self.respond(200, {"status": "recorded", "payment": payment})
        elif kind == "escrow":
            record = self.server.reconcile_escrow()
            self.respond(200, {"status": "recorded", "escrow": record})
        else:
            self.respond(400, {"status": "reconcile_kind_required"})

    def log_message(self, *args):
        return


def _known_subject(ledger, subject):
    return any(interval["subject"] == subject
               for interval in ledger.rights_intervals())


def _json_default(value):
    from fractions import Fraction
    if isinstance(value, Fraction):
        return str(value)
    raise TypeError(f"无法序列化: {type(value)!r}")
