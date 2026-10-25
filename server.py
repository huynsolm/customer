"""표준 라이브러리만 쓰는 로컬 서버. 실행: python server.py  →  http://localhost:8000"""
import json
import os
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import inventory as inv

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("CAFE_DB") or os.path.join(HERE, "cafe.db")
PORT = int(os.environ.get("PORT", "8000"))
SEED = [("우유", "개", 5, 3), ("원두", "kg", 2, 4), ("컵(12oz)", "개", 50, 120),
        ("시럽(바닐라)", "병", 1, 2), ("빨대", "봉", 2, 6)]


def seed():
    c = inv.connect(DB)
    if not c.execute("SELECT 1 FROM items").fetchone():
        for n, u, r, o in SEED:
            inv.add_item(c, n, u, r, o)


def route(conn, method, path, body):
    parts = path.strip("/").split("/")
    if method == "GET":
        if parts == ["api", "items"]:
            return inv.list_items(conn)
        if parts == ["api", "pos"]:
            return inv.list_pos(conn)
        if parts[:2] == ["api", "history"]:
            return inv.history(conn, int(parts[2]))
    elif method == "POST":
        a = body.get("actor", "")
        if parts == ["api", "pos"]:
            return {"id": inv.create_po(conn, body["item_id"], body["qty"], body.get("key"))}
        if parts[:2] == ["api", "pos"] and parts[3] == "receive":
            return {"id": inv.receive(conn, int(parts[2]), body["qty"], body["key"], a)}
        if parts[:2] == ["api", "pos"] and parts[3] == "cancel":
            return {"cancelled": inv.cancel_remaining(conn, int(parts[2]), body.get("reason", ""))}
        if parts == ["api", "out"]:
            return {"id": inv.record_out(conn, body["item_id"], body["qty"], body["kind"], body["key"], a, body.get("note", ""))}
        if parts == ["api", "stocktake"]:
            return {"id": inv.stocktake(conn, body["item_id"], body["counted"], body["key"], a)}
        if parts == ["api", "correct"]:
            return {"id": inv.correct(conn, body["movement_id"], body["qty"], body["key"], body.get("reason", ""), a)}
    raise inv.InventoryError("알 수 없는 요청")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        if method == "GET" and self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            conn = inv.connect(DB)
            try:
                self._send(200, route(conn, method, self.path.split("?")[0], body))
            finally:
                conn.close()
        except inv.InventoryError as e:
            self._send(400, {"error": str(e)})
        except (KeyError, ValueError, IndexError, TypeError):
            self._send(400, {"error": "잘못된 요청입니다"})
        except sqlite3.Error:
            self._send(500, {"error": "저장 중 오류가 발생했습니다. 다시 시도해 주세요"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


class Server(ThreadingHTTPServer):
    # Windows에서 SO_REUSEADDR를 켜면 같은 포트에 서버가 둘 떠도 오류가 나지 않고 요청이 나뉜다.
    allow_reuse_address = os.name != "nt"


if __name__ == "__main__":
    seed()
    print("http://localhost:%d" % PORT)
    Server(("127.0.0.1", PORT), Handler).serve_forever()
