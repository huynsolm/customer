"""매장 구분·품목 추가·작업자 기록·이전 DB 이전(마이그레이션) 테스트."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

import inventory as inv

# 이전 버전(매장·작업자 컬럼이 없던 시절)의 구조.
OLD_SCHEMA = """
CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, unit TEXT NOT NULL,
  reorder_point INTEGER NOT NULL DEFAULT 0);
CREATE TABLE purchase_orders (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
  ordered_qty INTEGER NOT NULL CHECK (ordered_qty > 0), cancelled_qty INTEGER NOT NULL DEFAULT 0,
  cancel_reason TEXT, idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE stock_movements (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
  po_id INTEGER REFERENCES purchase_orders(id),
  kind TEXT NOT NULL CHECK (kind IN ('opening','receipt','use','waste','correction','adjustment')),
  delta INTEGER NOT NULL, corrects_id INTEGER REFERENCES stock_movements(id),
  idempotency_key TEXT NOT NULL UNIQUE, request TEXT, actor TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
"""


class AddItem(unittest.TestCase):
    def setUp(self):
        self.conn = inv.connect()

    def test_add_and_opening_stock(self):
        i = inv.add_item(self.conn, " 설탕 ", " kg ", 2, 7, key="k", actor="민수")
        self.assertEqual([(x["name"], x["unit"], x["stock"]) for x in inv.list_items(self.conn)], [("설탕", "kg", 7)])
        self.assertEqual(inv.history(self.conn, i)[0]["actor"], "민수")

    def test_duplicate_click_adds_once(self):
        a = inv.add_item(self.conn, "설탕", "kg", 2, 7, key="k")
        b = inv.add_item(self.conn, "설탕", "kg", 2, 7, key="k")
        self.assertEqual(a, b)
        self.assertEqual(inv.current_stock(self.conn, a), 7)  # 기초 재고도 한 번만
        with self.assertRaises(inv.InventoryError):
            inv.add_item(self.conn, "소금", "kg", 2, 7, key="k")  # 같은 키, 다른 내용

    def test_duplicate_name_rejected_per_store(self):
        inv.add_item(self.conn, "설탕", "kg")
        with self.assertRaises(inv.InventoryError):
            inv.add_item(self.conn, "설탕", "봉")

    def test_opening_key_cannot_collide_with_user_keys(self):
        """4차 #2: 기초 재고 기록의 키가 예측 가능하면 사용자의 요청 키와 겹쳐 저장 오류가 났다."""
        inv.add_item(self.conn, "a", "개", opening=2)
        inv.record_out(self.conn, 1, 1, "use", "opening-2")
        b = inv.add_item(self.conn, "b", "개", opening=1)
        self.assertEqual(inv.current_stock(self.conn, b), 1)

    def test_update_item(self):
        a = inv.add_item(self.conn, "설탕", "kg", 2, 7, key="k")
        b = inv.add_item(self.conn, "소금", "kg")
        self.assertEqual(inv.update_item(self.conn, a, " 백설탕 ", 5, "민수"), a)
        self.assertEqual(inv.update_item(self.conn, a, "백설탕", 5, "민수"), a)  # 재전송해도 같은 결과
        it = [x for x in inv.list_items(self.conn) if x["id"] == a][0]
        self.assertEqual((it["name"], it["unit"], it["reorder_point"], it["stock"]), ("백설탕", "kg", 5, 7))
        self.assertEqual(inv.add_item(self.conn, "설탕", "kg", 2, 7, key="k"), a)  # 수정 뒤 추가 요청 재전송은 처음 결과
        with self.assertRaises(inv.InventoryError):
            inv.add_item(self.conn, "다른", "kg", 2, 7, key="k")
        for args in ((b, "백설탕", 1), (b, "", 1), (b, "소금", -1), (b, "소금", 1.5), (999, "x", 1), (True, "x", 1)):
            with self.assertRaises(inv.InventoryError, msg=args):
                inv.update_item(self.conn, *args)
        self.assertEqual(inv.update_item(self.conn, b, "소금", 3), b)  # 자기 이름은 허용

    def test_update_name_unique_only_within_store(self):
        s2 = inv.add_store(self.conn, "강남점")
        a = inv.add_item(self.conn, "설탕", "kg")
        inv.add_item(self.conn, "소금", "kg", store_id=s2)
        self.assertEqual(inv.update_item(self.conn, a, "소금", 1), a)  # 다른 매장의 같은 이름은 괜찮다

    def test_bad_input(self):
        for kw in ({"name": ""}, {"name": "  "}, {"name": 5}, {"name": "x" * 51}, {"unit": ""}, {"unit": None},
                   {"reorder_point": -1}, {"reorder_point": 1.5}, {"opening": True}, {"opening": 10 ** 9},
                   {"store_id": 99}, {"store_id": True}, {"actor": 1}, {"key": ""}):
            args = {"name": "설탕", "unit": "kg", **kw}
            with self.assertRaises(inv.InventoryError, msg=kw):
                inv.add_item(self.conn, **args)
        self.assertEqual(inv.list_items(self.conn), [])


class Stores(unittest.TestCase):
    def setUp(self):
        self.conn = inv.connect()
        self.b = inv.add_store(self.conn, "강남점")
        self.milk1 = inv.add_item(self.conn, "우유", "개", 5, 3)
        self.milk2 = inv.add_item(self.conn, "우유", "개", 5, 50, store_id=self.b)  # 다른 매장은 같은 이름 가능

    def test_default_store_and_listing(self):
        self.assertEqual([s["name"] for s in inv.list_stores(self.conn)], ["본점", "강남점"])

    def test_items_are_scoped(self):
        self.assertEqual([i["stock"] for i in inv.list_items(self.conn, 1)], [3])
        self.assertEqual([i["stock"] for i in inv.list_items(self.conn, self.b)], [50])
        self.assertEqual(len(inv.list_items(self.conn)), 2)

    def test_orders_and_shortage_are_scoped(self):
        po = inv.create_po(self.conn, self.milk1, 10, actor="민수")
        self.assertEqual(len(inv.list_pos(self.conn, 1)), 1)  # 입고 예정 10은 본점 품목에만 잡힌다
        self.assertEqual(inv.list_pos(self.conn, self.b), [])
        self.assertEqual(inv.get_po(self.conn, po)["created_by"], "민수")
        a = inv.shortage_report(self.conn, store_id=1)
        b = inv.shortage_report(self.conn, store_id=self.b)
        self.assertEqual([(r["item_id"], r["short"], r["incoming"]) for r in a], [(self.milk1, False, 10)])
        self.assertEqual([(r["item_id"], r["short"], r["incoming"]) for r in b], [(self.milk2, False, 0)])

    def test_store_validation_and_replay(self):
        for name in ("", "  ", None, "강남점"):
            with self.assertRaises(inv.InventoryError, msg=name):
                inv.add_store(self.conn, name)
        s = inv.add_store(self.conn, "홍대점", "sk")
        self.assertEqual(inv.add_store(self.conn, "홍대점", "sk"), s)
        with self.assertRaises(inv.InventoryError):
            inv.add_store(self.conn, "신촌점", "sk")

    def test_cancel_records_actor(self):
        po = inv.create_po(self.conn, self.milk1, 10)
        inv.cancel_remaining(self.conn, po, "결품", "지아")
        self.assertEqual(inv.get_po(self.conn, po)["cancelled_by"], "지아")


class Migration(unittest.TestCase):
    def old_db(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.executescript(OLD_SCHEMA)
        c.execute("INSERT INTO items(id,name,unit,reorder_point) VALUES(1,'우유','개',5),(2,'원두','kg',2)")
        c.execute("INSERT INTO purchase_orders(id,item_id,ordered_qty,idempotency_key) VALUES(1,1,10,'p1')")
        c.execute("INSERT INTO stock_movements(item_id,po_id,kind,delta,idempotency_key)"
                  " VALUES(1,NULL,'opening',4,'o1'),(1,1,'receipt',6,'r1')")
        c.commit()
        c.close()
        return path

    def test_old_db_keeps_data_and_gets_default_store(self):
        path = self.old_db()
        c = inv.connect(path)
        self.assertEqual([(i["name"], i["store_id"], i["stock"]) for i in inv.list_items(c)],
                         [("우유", 1, 10), ("원두", 1, 0)])
        self.assertEqual(inv.get_po(c, 1)["status"], "부분 입고")
        self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
        # 이전에는 전체에서 유일했던 이름이 이제 매장별로 유일하다.
        b = inv.add_store(c, "강남점")
        inv.add_item(c, "우유", "개", store_id=b)
        self.assertEqual(inv.history(c, 1)[1]["idempotency_key"], "r1")
        self.assertEqual(inv.receive(c, 1, 4, "r2"), inv.receive(c, 1, 4, "r2"))
        c.close()
        inv.connect(path).close()  # 두 번째 열기는 아무것도 바꾸지 않는다

    def test_concurrent_open_of_old_db(self):
        path = self.old_db()
        errors = []

        def opener():
            try:
                inv.connect(path).close()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=opener) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        c = inv.connect(path)
        self.assertEqual(len(inv.list_items(c)), 2)
        c.close()


class ServerRoutes(unittest.TestCase):
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

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_flow(self):
        self.assertEqual(self.call("/api/stores"), (200, [{"id": 1, "name": "본점"}]))
        st, b = self.call("/api/stores", {"name": "강남점", "key": "s1"})
        self.assertEqual(st, 200)
        self.assertEqual(self.call("/api/stores", {"name": "강남점", "key": "s1"}), (200, b))  # 더블클릭
        self.assertEqual(self.call("/api/items?store=%d" % b["id"])[1], [])
        self.assertEqual(len(self.call("/api/items")[1]), 5)  # 샘플 품목은 기본 매장
        item = {"store_id": b["id"], "name": "우유", "unit": "개", "reorder_point": 3, "opening": 9,
                "key": "i1", "actor": "민수"}
        st, made = self.call("/api/items", item)
        self.assertEqual(self.call("/api/items", item), (200, made))
        rows = self.call("/api/items?store=%d" % b["id"])[1]
        self.assertEqual([(r["name"], r["stock"]) for r in rows], [("우유", 9)])
        self.assertEqual(self.call("/api/history/%d" % made["id"])[1][0]["actor"], "민수")
        self.assertEqual(self.call("/api/shortage?store=%d" % b["id"])[1]["rows"][0]["item_id"], made["id"])
        for path in ("/api/items?store=x", "/api/items?store=999x", "/api/items?store=999", "/api/pos?store=999",
                     "/api/shortage?store=999"):  # 4차 #3: 없는 매장은 빈 매장이 아니라 오류
            self.assertEqual(self.call(path)[0], 400, path)
        self.assertEqual(self.call("/api/items/update", {"item_id": made["id"], "name": "우유2", "reorder_point": 4, "actor": "지아"})[0], 200)
        self.assertEqual(self.call("/api/items/update", {"item_id": made["id"], "name": "", "reorder_point": 4})[0], 400)
        self.assertEqual(self.call("/api/items/update", {"item_id": made["id"]})[0], 400)
        self.assertEqual(self.call("/api/items?store=%d" % b["id"])[1][0]["name"], "우유2")
        self.assertEqual(self.call("/api/shortage/ai?store=999", {})[0], 400)
        self.assertEqual(self.call("/api/items", {"unit": "개", "key": "z"})[0], 400)  # 이름 없음
        self.assertEqual(self.call("/api/items", {"name": "빈", "unit": "개", "store_id": 77})[0], 400)


if __name__ == "__main__":
    unittest.main()
