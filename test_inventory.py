import os
import sqlite3
import tempfile
import threading
import unittest

import inventory as inv


def fresh():
    conn = inv.connect()
    milk = inv.add_item(conn, "우유", "개", reorder_point=5, opening=3)
    return conn, milk


class PartialReceipt(unittest.TestCase):
    def test_partial_then_complete(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        self.assertEqual(inv.get_po(conn, po)["status"], "발주됨")
        inv.receive(conn, po, 8, "r1")
        self.assertEqual(inv.current_stock(conn, milk), 11)
        self.assertEqual(inv.get_po(conn, po)["status"], "부분 입고")
        inv.receive(conn, po, 2, "r2")
        self.assertEqual(inv.current_stock(conn, milk), 13)
        self.assertEqual(inv.get_po(conn, po)["status"], "입고 완료")

    def test_double_click_is_idempotent(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        a = inv.receive(conn, po, 8, "same")
        b = inv.receive(conn, po, 8, "same")
        self.assertEqual(a, b)
        self.assertEqual(inv.current_stock(conn, milk), 11)

    def test_over_receipt_rejected(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        inv.receive(conn, po, 8, "r1")
        with self.assertRaises(inv.InventoryError):
            inv.receive(conn, po, 5, "r2")
        self.assertEqual(inv.current_stock(conn, milk), 11)

    def test_cancel_remaining(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        inv.receive(conn, po, 8, "r1")
        inv.cancel_remaining(conn, po, "거래처 결품")
        self.assertEqual(inv.get_po(conn, po)["status"], "종결(미입고 2)")
        with self.assertRaises(inv.InventoryError):
            inv.receive(conn, po, 1, "r2")

    def test_po_double_click_creates_one(self):
        conn, milk = fresh()
        a = inv.create_po(conn, milk, 10, "p1")
        b = inv.create_po(conn, milk, 10, "p1")
        self.assertEqual(a, b)
        self.assertEqual(len(inv.list_pos(conn)), 1)

    def test_unknown_item_rejected(self):
        conn, _ = fresh()
        for call in (lambda: inv.create_po(conn, 99, 1),
                     lambda: inv.stocktake(conn, 99, 1, "s"),
                     lambda: inv.record_out(conn, 99, 1, "use", "u")):
            with self.assertRaises(inv.InventoryError):
                call()

    def test_invalid_qty(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        for bad in (0, -1, 1.5, "3", True):
            with self.assertRaises(inv.InventoryError):
                inv.receive(conn, po, bad, "k%s" % bad)


class Correction(unittest.TestCase):
    def test_correct_keeps_history(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        r = inv.receive(conn, po, 8, "r1")
        inv.correct(conn, r, 6, "c1", "8개로 잘못 입력")
        self.assertEqual(inv.current_stock(conn, milk), 9)
        self.assertEqual(inv.get_po(conn, po)["received"], 6)
        kinds = [h["kind"] for h in inv.history(conn, milk)]
        self.assertEqual(kinds, ["opening", "receipt", "correction"])  # 원본은 그대로

    def test_correct_twice_uses_effective_value(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        r = inv.receive(conn, po, 8, "r1")
        inv.correct(conn, r, 6, "c1", "x")
        inv.correct(conn, r, 7, "c2", "y")
        self.assertEqual(inv.current_stock(conn, milk), 10)

    def test_void(self):
        conn, milk = fresh()
        u = inv.record_out(conn, milk, 2, "use", "u1")
        inv.correct(conn, u, 0, "c1", "잘못 입력")
        self.assertEqual(inv.current_stock(conn, milk), 3)

    def test_correct_idempotent_and_needs_reason(self):
        conn, milk = fresh()
        u = inv.record_out(conn, milk, 2, "use", "u1")
        inv.correct(conn, u, 1, "c1", "수정")
        inv.correct(conn, u, 1, "c1", "수정")  # 더블클릭
        self.assertEqual(inv.current_stock(conn, milk), 2)
        with self.assertRaises(inv.InventoryError):
            inv.correct(conn, u, 0, "c2", "")

    def test_correction_cannot_exceed_po(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        r = inv.receive(conn, po, 8, "r1")
        with self.assertRaises(inv.InventoryError):
            inv.correct(conn, r, 11, "c1", "오타")


class UsageAndStocktake(unittest.TestCase):
    def test_cannot_go_negative(self):
        conn, milk = fresh()
        with self.assertRaises(inv.InventoryError):
            inv.record_out(conn, milk, 4, "use", "u1")

    def test_stocktake_records_difference(self):
        conn, milk = fresh()
        inv.record_out(conn, milk, 1, "waste", "w1")
        inv.stocktake(conn, milk, 1, "s1")  # 장부 2, 실제 1
        self.assertEqual(inv.current_stock(conn, milk), 1)
        last = inv.history(conn, milk)[-1]
        self.assertEqual((last["kind"], last["delta"]), ("adjustment", -1))

    def test_low_flag(self):
        conn, milk = fresh()
        self.assertTrue(inv.list_items(conn)[0]["low"])


class ReviewFindings(unittest.TestCase):
    """직접 검수에서 재현된 문제들의 회귀 테스트."""

    def test_same_key_other_operation_rejected(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        inv.receive(conn, po, 8, "k")
        with self.assertRaises(inv.InventoryError):
            inv.record_out(conn, milk, 2, "use", "k")  # 전에는 조용히 무시돼 사용 2가 사라졌다
        with self.assertRaises(inv.InventoryError):
            inv.receive(conn, po, 1, "k")
        self.assertEqual(inv.current_stock(conn, milk), 11)

    def test_same_key_other_po_rejected(self):
        conn, milk = fresh()
        inv.create_po(conn, milk, 10, "p")
        with self.assertRaises(inv.InventoryError):
            inv.create_po(conn, milk, 99, "p")

    def test_empty_key_rejected(self):
        conn, milk = fresh()
        for key in ("", "  ", None):
            with self.assertRaises(inv.InventoryError):
                inv.record_out(conn, milk, 1, "use", key)

    def test_correction_before_stocktake_rejected(self):
        conn = inv.connect()
        milk = inv.add_item(conn, "우유", "개", 0, 10)
        u = inv.record_out(conn, milk, 2, "use", "u")  # 실제 3개 사용, 실물 7
        inv.stocktake(conn, milk, 7, "s")
        with self.assertRaises(inv.InventoryError):
            inv.correct(conn, u, 3, "c", "사용 3개였음")  # 전에는 6이 되어 실물 7과 어긋났다
        self.assertEqual(inv.current_stock(conn, milk), 7)

    def test_correction_up_after_cancel_explains(self):
        conn, milk = fresh()
        po = inv.create_po(conn, milk, 10)
        r = inv.receive(conn, po, 8, "r")
        inv.cancel_remaining(conn, po, "결품")
        with self.assertRaisesRegex(inv.InventoryError, "취소된 발주"):
            inv.correct(conn, r, 10, "c", "실제 10개")

    def test_bad_numbers_rejected(self):
        conn, milk = fresh()
        with self.assertRaises(inv.InventoryError):
            inv.stocktake(conn, milk, True, "s")
        for opening in (-5, 1.5):
            with self.assertRaises(inv.InventoryError):
                inv.add_item(conn, "원두%s" % opening, "kg", 0, opening)
        with self.assertRaises(inv.InventoryError):
            inv.create_po(conn, milk, 2 ** 63)

    def test_failed_commit_leaves_connection_usable(self):
        conn, milk = fresh()
        real = conn.execute

        class Flaky:
            def __getattr__(self, n):
                return getattr(conn, n)

            def execute(self, sql, *a):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("database is locked")
                return real(sql, *a)

        with self.assertRaises(sqlite3.OperationalError):
            inv.record_out(Flaky(), milk, 1, "use", "b1")
        inv.record_out(conn, milk, 1, "use", "b2")  # 전에는 "transaction within a transaction"
        self.assertEqual(inv.current_stock(conn, milk), 2)


class Concurrency(unittest.TestCase):
    def test_parallel_usage_sums_correctly(self):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        conn = inv.connect(path)
        milk = inv.add_item(conn, "우유", "개", opening=100)
        errors = []

        def worker(n):
            c = inv.connect(path)
            try:
                for i in range(10):
                    inv.record_out(c, milk, 1, "use", "w%d-%d" % (n, i))
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                c.close()

        ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(inv.current_stock(conn, milk), 20)

    def test_parallel_same_key_counts_once(self):
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        conn = inv.connect(path)
        milk = inv.add_item(conn, "우유", "개", opening=0)
        po = inv.create_po(conn, milk, 10)
        errors = []

        def worker():
            c = inv.connect(path)
            try:
                inv.receive(c, po, 8, "dup")
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                c.close()

        ts = [threading.Thread(target=worker) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(inv.current_stock(conn, milk), 8)


if __name__ == "__main__":
    unittest.main()
