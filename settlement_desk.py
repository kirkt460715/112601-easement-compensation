"""补偿处理后端入口。

默认不依赖资料台独立运行（内存台账，可通过 HTTP 受理事实）；
传入 ``--desk-port`` 时启动即从资料台拉取合成台账与已到达消息。

示例：

    python3 evidence_desk.py --port 8765 &
    python3 settlement_desk.py --port 8770 --desk-port 8765 --bootstrap
"""

import argparse
from http.client import HTTPConnection
import json

from settlement.desk_client import DeskClient
from settlement.service import SettlementDesk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--desk-port", type=int, default=None,
                        help="本地资料台端口；不填则仅使用内存台账")
    parser.add_argument("--desk-host", default="127.0.0.1")
    parser.add_argument("--bootstrap", action="store_true",
                        help="启动时从资料台载入台账与全部消息")
    parser.add_argument("--seed", type=argparse.FileType("r", encoding="utf-8"),
                        help="启动时载入的初始台账 JSON 文件")
    args = parser.parse_args()

    desk_client = None
    if args.desk_port is not None:
        desk_client = DeskClient(args.desk_host, args.desk_port)
    server = SettlementDesk(args.port, desk_client=desk_client)

    if args.seed is not None:
        server.ledger.load_register(json.load(args.seed))
    if args.bootstrap:
        if desk_client is None:
            parser.error("--bootstrap 需要同时指定 --desk-port")
        server.bootstrap_from_desk()

    print(f"补偿后端 http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def post(server_port, path, payload):
    """便捷的本地 POST 辅助，供脚本/手工联调使用。"""
    connection = HTTPConnection("127.0.0.1", server_port, timeout=5)
    try:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        connection.request("POST", path, raw,
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


if __name__ == "__main__":
    main()
