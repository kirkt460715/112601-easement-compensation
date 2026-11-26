"""本地资料台客户端。

资料台提供三类只读事实：合成台账（``/register``）、按接收顺序分页的
到达事实（``/messages``）、以及合作方独立回执（``/partner``，首次可能
503）。客户端只负责如实取回事实并重试临时性失败，不做任何业务判断。
"""

import json
from http.client import HTTPConnection
from urllib.parse import urlencode


class DeskUnavailable(RuntimeError):
    """重试后合作方仍暂时不可用。"""


class DeskClient:
    def __init__(self, host, port, timeout=5, retries=3, retry_pause=0.05):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.retry_pause = retry_pause

    def _get(self, path, expect_retry=False):
        last_error = None
        attempts = self.retries if expect_retry else 1
        for attempt in range(attempts):
            connection = HTTPConnection(self.host, self.port,
                                        timeout=self.timeout)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                body = json.loads(response.read())
                if response.status == 503 and attempt + 1 < attempts:
                    last_error = body
                    continue
                return response.status, body
            finally:
                connection.close()
        raise DeskUnavailable(f"资料台暂时不可用: {path} {last_error}")

    def fetch_register(self):
        status, body = self._get("/register")
        if status != 200:
            raise DeskUnavailable(f"台账读取失败: {status}")
        return body

    def fetch_messages(self, size=30):
        """按接收顺序拉取全部消息，自动翻页。"""
        messages = []
        after = 0
        while True:
            query = urlencode({"after": after, "size": size})
            status, body = self._get(f"/messages?{query}")
            if status != 200:
                raise DeskUnavailable(f"消息读取失败: {status}")
            page = body["messages"]
            if not page:
                break
            messages.extend(page)
            after = body["cursor"]
            if len(page) < size:
                break
        return messages

    def fetch_partner_fact(self, action, desk):
        """查询合作方回执；资料台约定首次可能 503，按重试策略再取。"""
        query = urlencode({"action": action, "desk": desk})
        status, body = self._get(f"/partner?{query}", expect_retry=True)
        if status != 200:
            raise DeskUnavailable(f"合作方查询失败: {status} {body}")
        return body["fact"]
