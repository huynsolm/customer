"""scenarios.json의 업무 사례를 그대로 재생해 검증한다. 사례를 추가하면 테스트도 늘어난다."""
import json
import os
import unittest

import inventory as inv

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenarios.json"), encoding="utf-8") as f:
    SCENARIOS = json.load(f)


def run(test, sc):
    conn = inv.connect()
    items = {i["name"]: inv.add_item(conn, i["name"], i["unit"], i["reorder_point"], i["opening"])
             for i in sc["items"]}
    refs = {}
    for n, s in enumerate(sc["steps"], 1):
        where = "%s / %d단계 %s" % (sc["name"], n, s["op"])
        item = items.get(s.get("item"))
        po = refs.get(s.get("po"))
        call = {
            "create_po": lambda: inv.create_po(conn, item, s["qty"], s["key"]),
            "receive": lambda: inv.receive(conn, po, s["qty"], s["key"]),
            "cancel_remaining": lambda: inv.cancel_remaining(conn, po, s["reason"]),
            "record_out": lambda: inv.record_out(conn, item, s["qty"], s["kind"], s["key"]),
            "stocktake": lambda: inv.stocktake(conn, item, s["counted"], s["key"]),
            "correct": lambda: inv.correct(conn, refs[s["movement"]], s["qty"], s["key"], s["reason"]),
        }.get(s["op"])
        if call:
            if "error" in s:
                with test.assertRaises(inv.InventoryError, msg=where) as cm:
                    call()
                test.assertIn(s["error"], str(cm.exception), where)
            else:
                result = call()
                if "as" in s:
                    refs[s["as"]] = result
            continue
        # op == "check"
        if "stock" in s:
            test.assertEqual(inv.current_stock(conn, item), s["stock"], where)
        if "low" in s:
            low = next(i["low"] for i in inv.list_items(conn) if i["id"] == item)
            test.assertEqual(low, s["low"], where)
        if "status" in s:
            test.assertEqual(inv.get_po(conn, po)["status"], s["status"], where)
        if "po_count" in s:
            test.assertEqual(len(inv.list_pos(conn)), s["po_count"], where)
        if "history_kinds" in s:
            test.assertEqual([h["kind"] for h in inv.history(conn, item)], s["history_kinds"], where)
        if "last_delta" in s:
            test.assertEqual(inv.history(conn, item)[-1]["delta"], s["last_delta"], where)


class Scenarios(unittest.TestCase):
    def test_all(self):
        for sc in SCENARIOS:
            with self.subTest(sc["name"]):
                run(self, sc)


if __name__ == "__main__":
    unittest.main()
