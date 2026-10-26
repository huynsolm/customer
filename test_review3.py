"""3차 검수(Codex)에서 지적된 문제의 회귀 테스트. AI 요약(#4·#8)은 test_summary.py, 화면(#2·#3·#7)은 test_client.mjs."""
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import inventory as inv


def legacy_row(conn, item_id, kind, delta, key, po_id=None):
    """이전 버전처럼 request 없이 남은 기록을 만든다."""
    return conn.execute(
        "INSERT INTO stock_movements(item_id,po_id,kind,delta,idempotency_key) VALUES(?,?,?,?,?)",
        (item_id, po_id, kind, delta, key)).lastrowid


class LegacyNullRequest(unittest.TestCase):
    """#1 request가 NULL인 기존 기록이면 다른 작업의 키도 성공 처리되던 문제."""

    def setUp(self):
        self.conn = inv.connect()
        self.milk = inv.add_item(self.conn, "우유", "개", 0, 10)
        self.old_use = legacy_row(self.conn, self.milk, "use", -2, "old-use")

    def test_other_operation_rejected(self):
        po = inv.create_po(self.conn, self.milk, 5)
        with self.assertRaises(inv.InventoryError):
            inv.receive(self.conn, po, 5, "old-use")
        self.assertEqual(inv.get_po(self.conn, po)["received"], 0)

    def test_same_operation_still_replays(self):
        self.assertEqual(inv.record_out(self.conn, self.milk, 2, "use", "old-use"), self.old_use)
        with self.assertRaises(inv.InventoryError):
            inv.record_out(self.conn, self.milk, 3, "use", "old-use")
        self.assertEqual(inv.current_stock(self.conn, self.milk), 8)

    def test_opening_key_cannot_be_reused(self):
        with self.assertRaises(inv.InventoryError):
            inv.record_out(self.conn, self.milk, 1, "use", inv.history(self.conn, self.milk)[0]["idempotency_key"])

    def test_unrecoverable_legacy_stocktake_rejected(self):
        legacy_row(self.conn, self.milk, "adjustment", 0, "old-count")
        with self.assertRaisesRegex(inv.InventoryError, "확인할 수 없"):
            inv.stocktake(self.conn, self.milk, 8, "old-count")


class ReportPerformance(unittest.TestCase):
    """#5 사용량 집계가 기록마다 정정 전체를 다시 읽어 제곱에 가깝게 느려지던 문제."""

    def test_8000_records_fast(self):
        conn = inv.connect()
        milk = inv.add_item(conn, "우유", "개", 0, 10 ** 6)
        conn.executemany(
            "INSERT INTO stock_movements(item_id,kind,delta,idempotency_key) VALUES(?,?,?,?)",
            [(milk, "use", -1, "u%d" % i) for i in range(8000)])
        start = time.perf_counter()
        r = inv.shortage_report(conn)[0]
        self.assertLess(time.perf_counter() - start, 0.3)
        self.assertEqual(r["daily_use"], round(8000 / 7, 1))
        plan = " ".join(str(tuple(row)) for row in conn.execute(
            "EXPLAIN QUERY PLAN " + inv.USAGE_SQL, ("9999", "0000", "9999")))
        self.assertNotIn("CORRELATED", plan)


class ServerBodyLength(unittest.TestCase):
    """#6 잘못된 Content-Length·미완료 본문에 요청 스레드가 계속 대기하던 문제."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.patches = [mock.patch.object(server, "DB", os.path.join(tempfile.mkdtemp(), "s.db")),
                       mock.patch.object(server.Handler, "timeout", 0.5)]
        for p in cls.patches:
            p.start()
        server.seed()
        cls.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()

    def raw(self, length, body):
        """연결을 열어 둔 채 요청을 보내고, 응답 첫 줄이 오는 데 걸린 시간을 잰다."""
        s = socket.create_connection(("127.0.0.1", self.port))
        s.settimeout(3)
        s.sendall(("POST /api/pos HTTP/1.1\r\nHost: x\r\nContent-Length: %s\r\n\r\n%s" % (length, body)).encode())
        start = time.perf_counter()
        try:
            first = s.recv(64).split(b"\r\n")[0].decode()
        except socket.timeout:
            first = "timeout"
        s.close()
        return first, time.perf_counter() - start

    def test_negative_length_400_now(self):
        first, took = self.raw(-1, "{}")
        self.assertIn(" 400 ", first)
        self.assertLess(took, 0.3)

    def test_too_large_413_now(self):
        first, took = self.raw(1_000_000, "{}")
        self.assertIn(" 413 ", first)
        self.assertLess(took, 0.3)

    def test_incomplete_body_times_out(self):
        first, took = self.raw(10, "{}")
        self.assertNotEqual(first, "timeout")  # 서버가 제한 시간 뒤 응답하거나 연결을 닫는다
        self.assertLess(took, 2)


if __name__ == "__main__":
    unittest.main()
