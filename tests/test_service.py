"""HTTP 服务与资料台联调测试。"""

from http.client import HTTPConnection
import json
from threading import Thread
import unittest
from urllib.parse import urlencode

from evidence_desk import EvidenceDesk
from settlement.desk_client import DeskClient, DeskUnavailable
from settlement.service import SettlementDesk

FIXTURE_REGISTER = {
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
    ],
    "funds": {"A": 23000, "B": 16000},
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


class HttpServerTestBase(unittest.TestCase):
    server_cls = SettlementDesk

    def start_server(self, server):
        thread = Thread(target=server.serve_forever,
                        kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server

    def request(self, port, method, path, payload=None):
        connection = HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            body = None
            headers = {}
            if payload is not None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
        finally:
            connection.close()


class SettlementHttpTest(HttpServerTestBase):
    def setUp(self):
        self.server = self.start_server(SettlementDesk(0))
        self.port = self.server.server_port
        self.server.ledger.load_register(FIXTURE_REGISTER)

    def test_state_and_decisions_endpoint(self):
        status, body = self.request(self.port, "GET", "/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["land"], "L-50")
        status, body = self.request(self.port, "GET", "/operator/decisions")
        self.assertEqual(status, 200)
        self.assertTrue(body["decisions"])

    def test_household_submit_and_view(self):
        # 住户提交待证份额主张。
        status, body = self.request(self.port, "POST", "/facts", {
            "source": "household", "received_at": 27,
            "facts": {"kind": "share", "subject": "hh-9",
                      "replaces": "hh-2", "fraction": "1/2",
                      "valid_from": 6},
        })
        self.assertEqual(status, 201)
        status, view = self.request(
            self.port, "GET",
            "/household/view?" + urlencode({"subject": "hh-1"}))
        self.assertEqual(status, 200)
        self.assertEqual(view["entitled_cents"], 9000)
        self.assertEqual(view["payable_unsubmitted_cents"], 9000)
        # 未知住户返回 404。
        status, _ = self.request(
            self.port, "GET",
            "/household/view?" + urlencode({"subject": "nobody"}))
        self.assertEqual(status, 404)

    def test_register_fact_releases_escrow(self):
        status, _ = self.request(self.port, "POST", "/facts", {
            "source": "register", "sequence": 2, "received_at": 26,
            "facts": {"subject": "hh-3", "fraction": "1/2",
                      "replaces": "hh-2", "valid_from": 6},
        })
        self.assertEqual(status, 201)
        status, view = self.request(
            self.port, "GET",
            "/household/view?" + urlencode({"subject": "hh-3"}))
        self.assertEqual(status, 200)
        self.assertEqual(view["entitled_cents"], 9000)
        self.assertEqual(view["escrowed_cents"], 0)

    def test_payment_lifecycle_and_treasury_view(self):
        status, body = self.request(self.port, "POST",
                                    "/operator/payments",
                                    {"to": "hh-1", "cents": 5000,
                                     "fund": "A",
                                     "claim_id": "claim-18"})
        self.assertEqual(status, 201)
        payment_id = body["payment"]["id"]
        batch = body["batch"]
        # 提交后只占用预算，未支出。
        status, view = self.request(
            self.port, "GET",
            "/treasury/view?" + urlencode({"batch": batch}))
        self.assertEqual(status, 200)
        self.assertEqual(view["funds"]["A"]["occupied_cents"], 5000)
        self.assertEqual(view["funds"]["A"]["spent_cents"], 0)
        # 登记回执后转为支出；回执本身不携带权益信息。
        status, body = self.request(self.port, "POST",
                                    "/operator/receipts",
                                    {"transfer_id": payment_id,
                                     "state": "paid", "cents": 5000})
        self.assertEqual(status, 200)
        status, view = self.request(self.port, "GET", "/treasury/view")
        self.assertEqual(view["funds"]["A"]["spent_cents"], 5000)

    def test_bad_requests(self):
        status, body = self.request(self.port, "POST",
                                    "/operator/payments",
                                    {"to": "hh-1", "cents": -1})
        self.assertEqual(status, 400)
        status, body = self.request(self.port, "POST", "/facts",
                                    {"source": "household"})
        self.assertEqual(status, 400)
        status, _ = self.request(self.port, "GET", "/nope")
        self.assertEqual(status, 404)

    def test_budget_hold_fact_blocks_b_share(self):
        self.request(self.port, "POST", "/facts", {
            "source": "budget", "sequence": 3, "received_at": 28,
            "facts": {"fund": "B", "state": "held"},
        })
        status, body = self.request(self.port, "GET",
                                    "/operator/decisions")
        b = [d for d in body["decisions"] if d["fund"] == "B"
             and d["kind"] == "holder"]
        self.assertTrue(b)
        self.assertTrue(all(d["status"] == "held" for d in b))


class DeskIntegrationTest(HttpServerTestBase):
    def setUp(self):
        self.desk = self.start_server(EvidenceDesk(0))
        self.client = DeskClient("127.0.0.1", self.desk.server_port,
                                 retries=3, retry_pause=0.01)
        self.server = self.start_server(
            SettlementDesk(0, desk_client=self.client))

    def test_client_pagination_and_register(self):
        register = self.client.fetch_register()
        self.assertEqual(register["land"], "L-50")
        messages = self.client.fetch_messages(size=2)
        self.assertEqual([m["sequence"] for m in messages], [1, 2, 3, 4])

    def test_partner_retry_on_first_503(self):
        fact = self.client.fetch_partner_fact("read-transfer", "treasury")
        self.assertEqual(fact["state"], "paid")
        # 第二次起稳定返回。
        again = self.client.fetch_partner_fact("read-transfer", "treasury")
        self.assertEqual(again, fact)
        with self.assertRaises(DeskUnavailable):
            self.client.fetch_partner_fact("missing", "treasury")

    def test_bootstrap_replays_messages_in_order(self):
        self.server.bootstrap_from_desk()
        ledger = self.server.ledger
        self.assertEqual(ledger.land, "L-50")
        self.assertEqual([e["sequence"] for e in ledger.events],
                         [1, 2, 3, 4])
        # seq3 已封存 B；seq2 已把 hh-2 槽位确权给 hh-3。
        decisions, _ = ledger.decisions()
        self.assertTrue(any(d["status"] == "held"
                            for d in decisions if d["fund"] == "B"))
        self.assertTrue(any(d.get("subject") == "hh-3"
                            for d in decisions))
        self.assertFalse(any(d["kind"] == "escrow"
                             for d in decisions))

    def test_reconcile_transfer_via_partner_desk(self):
        self.server.bootstrap_from_desk()
        # out-4 已由消息进入台账（submitted），向资料台取回执确认。
        payment = self.server.reconcile_transfer("out-4")
        self.assertEqual(payment["state"], "paid")
        self.assertTrue(payment["confirmed"])
        view = self.server.ledger.treasury_view()
        self.assertGreaterEqual(view["funds"]["A"]["spent_cents"], 0)
        # 回执确认不改变权益：hh-4 仍是 seq4 确立的权益人。
        subjects = {r["subject"]
                    for r in self.server.ledger.rights_intervals()}
        self.assertIn("hh-4", subjects)

    def test_reconcile_escrow_record(self):
        record = self.server.reconcile_escrow()
        self.assertEqual(record["state"], "held")
        self.assertEqual(record["cents"], 7600)


if __name__ == "__main__":
    unittest.main()
