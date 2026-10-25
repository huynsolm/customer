"""2차 검수(Codex)에서 지적된 문제의 회귀 테스트."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import inventory as inv


def fresh(opening=90, reorder_point=0):
    conn = inv.connect()
    return conn, inv.add_item(conn, "우유", "개", reorder_point, opening)


class SameKeyDifferentPayload(unittest.TestCase):
    """#1 실사·정정은 같은 키에 다른 수량이 와도 성공 처리되던 문제."""

    def test_stocktake(self):
        conn, milk = fresh()
        inv.stocktake(conn, milk, 80, "s")
        self.assertEqual(inv.stocktake(conn, milk, 80, "s"), inv.history(conn, milk)[-1]["id"])
        with self.assertRaises(inv.InventoryError):
            inv.stocktake(conn, milk, 70, "s")
        self.assertEqual(inv.current_stock(conn, milk), 80)

    def test_correct(self):
        conn, milk = fresh()
        u = inv.record_out(conn, milk, 10, "use", "u")
        inv.correct(conn, u, 5, "k", "수정")
        inv.correct(conn, u, 5, "k", "수정")
        with self.assertRaises(inv.InventoryError):
            inv.correct(conn, u, 20, "k", "수정")

    def test_receive_and_out_still_checked(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        inv.receive(conn, po, 5, "r")
        with self.assertRaises(inv.InventoryError):
            inv.receive(conn, po, 4, "r")
        inv.record_out(conn, milk, 1, "use", "o")
        with self.assertRaises(inv.InventoryError):
            inv.record_out(conn, milk, 1, "waste", "o")


class CancelledPoCorrection(unittest.TestCase):
    """#2 잔량 취소된 발주가 하향 정정으로 다시 열리고 상향 정정도 허용되던 문제."""

    def test_stays_closed(self):
        conn, milk = fresh(0)
        po = inv.create_po(conn, milk, 10)
        r = inv.receive(conn, po, 5, "r")
        inv.cancel_remaining(conn, po, "결품")
        inv.correct(conn, r, 3, "c1", "실제 3개")
        p = inv.get_po(conn, po)
        self.assertEqual((p["received"], p["remaining"], p["status"]), (3, 0, "종결(미입고 7)"))
        with self.assertRaisesRegex(inv.InventoryError, "취소된 발주"):
            inv.correct(conn, r, 4, "c2", "실제 4개")
        self.assertEqual(inv.shortage_report(conn)[0]["incoming"], 0)


class ShortageWindow(unittest.TestCase):
    NOW = "2026-10-26 12:00:00"

    def at(self, conn, mid, when):
        conn.execute("UPDATE stock_movements SET created_at=? WHERE id=?", (when, mid))

    def test_old_record_correction_does_not_cut_recent_usage(self):
        """#3 7일 밖 기록의 정정이 최근 사용량을 깎던 문제."""
        conn, milk = fresh(100, 100)
        old = inv.record_out(conn, milk, 20, "use", "u1")
        self.at(conn, old, "2026-10-18 10:00:00")
        self.at(conn, inv.record_out(conn, milk, 10, "use", "u2"), "2026-10-25 10:00:00")
        self.at(conn, inv.correct(conn, old, 5, "c", "5개였음"), "2026-10-26 09:00:00")
        r = inv.shortage_report(conn, now=self.NOW)[0]
        self.assertEqual((r["stock"], r["daily_use"], r["days_left"], r["suggested_qty"]), (85, 1.4, 59.5, 25))

    def test_correction_after_now_ignored(self):
        conn, milk = fresh(100, 0)
        u = inv.record_out(conn, milk, 14, "use", "u")
        self.at(conn, u, "2026-10-25 10:00:00")
        self.at(conn, inv.correct(conn, u, 7, "c", "x"), "2026-10-27 10:00:00")
        self.assertEqual(inv.shortage_report(conn, now=self.NOW)[0]["daily_use"], 2.0)

    def test_integer_ceil(self):
        """#5 (29/7)*7 부동소수점 오차로 권장량이 1 늘던 문제."""
        for used in (29, 58):
            conn, milk = fresh(used, 0)
            inv.record_out(conn, milk, used, "use", "u")
            self.assertEqual(inv.shortage_report(conn)[0]["suggested_qty"], used)

    def test_snapshot_during_concurrent_receipt(self):
        """#4 보고서 계산 중 입고가 끼어들면 같은 물량을 두 번 세던 문제."""
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        a = inv.connect(path)
        milk = inv.add_item(a, "우유", "개", 15, 0)
        po = inv.create_po(a, milk, 10)
        b = inv.connect(path)
        real = inv.list_items
        fired = []

        def list_items_after_receipt(conn):
            if not fired:
                fired.append(1)
                inv.receive(b, po, 10, "r")  # 보고서가 발주 목록을 읽은 뒤 다른 연결에서 입고
            return real(conn)

        with mock.patch.object(inv, "list_items", list_items_after_receipt):
            r = inv.shortage_report(a)[0]
        # 한 시점의 스냅샷이면 (입고 전) 재고 0 + 입고 예정 10 이어야 한다.
        self.assertEqual((r["stock"], r["incoming"]), (0, 10))
        r = inv.shortage_report(a)[0]
        self.assertEqual((r["stock"], r["incoming"], r["short"], r["suggested_qty"]), (10, 0, True, 5))
        a.close()
        b.close()


class Validation(unittest.TestCase):
    def test_create_po_key(self):
        """#10 발주 생성만 빈 키·숫자 키를 허용하던 문제."""
        conn, milk = fresh()
        for key in ("", "  ", 123):
            with self.assertRaises(inv.InventoryError):
                inv.create_po(conn, milk, 1, key)
        inv.create_po(conn, milk, 1)  # 키 생략은 계속 허용

    def test_bad_ids_and_text(self):
        conn, milk = fresh()
        for bad in (10 ** 100, True, "1", None):
            with self.assertRaises(inv.InventoryError):
                inv.create_po(conn, bad, 1)
        u = inv.record_out(conn, milk, 1, "use", "u")
        with self.assertRaises(inv.InventoryError):
            inv.correct(conn, u, 0, "c", 123)


class ServerBadInput(unittest.TestCase):
    """#7 객체가 아닌 본문·큰 ID에 응답 없이 연결이 끊기던 문제."""

    @classmethod
    def setUpClass(cls):
        import server
        cls.db = os.path.join(tempfile.mkdtemp(), "s.db")
        cls.patch = mock.patch.object(server, "DB", cls.db)
        cls.patch.start()
        server.seed()
        cls.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.patch.stop()

    def post(self, path, raw):
        req = urllib.request.Request(self.base + path, data=raw.encode(), method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_bad_bodies_get_json_400(self):
        for path, raw in [("/api/pos", "[]"), ("/api/pos", "null"), ("/api/pos", "{bad"),
                          ("/api/pos", json.dumps({"item_id": 10 ** 100, "qty": 1, "key": "a"})),
                          ("/api/correct", json.dumps({"movement_id": 1, "qty": 0, "key": "c", "reason": 5}))]:
            status, body = self.post(path, raw)
            self.assertEqual(status, 400, raw)
            self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
