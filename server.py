"""표준 라이브러리만 쓰는 로컬 서버. 실행: python server.py  →  http://localhost:8000"""
import json
import os
import sqlite3
import traceback
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import inventory as inv
import summary

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("CAFE_DB") or os.path.join(HERE, "cafe.db")
PORT = int(os.environ.get("PORT", "8000"))
MAX_BODY = 64 * 1024  # 이 앱의 요청 본문은 수백 바이트면 충분하다
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/client.js": ("client.js", "text/javascript; charset=utf-8"),
}
SEED = [("우유", "개", 5, 3), ("원두", "kg", 2, 4), ("컵(12oz)", "개", 50, 120),
        ("시럽(바닐라)", "병", 1, 2), ("빨대", "봉", 2, 6)]


def seed():
    c = inv.connect(DB)
    if not c.execute("SELECT 1 FROM items").fetchone():
        for n, u, r, o in SEED:
            inv.add_item(c, n, u, r, o)


def store_of(conn, query):
    """?store=번호. 없으면 기본 매장. 없는 매장이면 빈 매장으로 보이지 않게 오류로 답한다."""
    v = (query.get("store") or [str(inv.DEFAULT_STORE)])[0]
    if not v.isdigit():
        raise inv.InventoryError("매장 번호가 올바르지 않습니다")
    inv.need_store(conn, int(v))
    return int(v)


def route(conn, method, path, body, query=None):
    query = query or {}
    parts = path.strip("/").split("/")
    if method == "GET":
        if parts == ["api", "stores"]:
            return inv.list_stores(conn)
        if parts == ["api", "items"]:
            return inv.list_items(conn, store_of(conn, query), archived=query.get("archived") == ["1"])
        if parts == ["api", "pos"]:
            return inv.list_pos(conn, store_of(conn, query))
        if parts[:2] == ["api", "history"]:
            return inv.history(conn, int(parts[2]))
        if parts[:2] == ["api", "item-changes"]:
            return inv.item_changes(conn, int(parts[2]))
        if parts == ["api", "shortage"]:
            rows = inv.shortage_report(conn, store_id=store_of(conn, query))
            return {"rows": rows, "summary": summary.template_summary(rows)}
    elif method == "POST":
        a = body.get("actor", "")
        if parts == ["api", "shortage", "ai"]:
            text, source, note = summary.ai_summary(inv.shortage_report(conn, store_id=store_of(conn, query)))
            return {"summary": text, "source": source, "note": note}
        if parts == ["api", "stores"]:
            return {"id": inv.add_store(conn, body["name"], body.get("key"))}
        if parts == ["api", "items"]:
            return {"id": inv.add_item(conn, body["name"], body["unit"], body.get("reorder_point", 0),
                                       body.get("opening", 0), body.get("key"),
                                       body.get("store_id", inv.DEFAULT_STORE), a)}
        if parts == ["api", "stores", "rename"]:
            return {"id": inv.update_store(conn, body["store_id"], body["name"])}
        if parts == ["api", "items", "archive"]:
            return {"id": inv.set_archived(conn, body["item_id"], body["archived"], a)}
        if parts == ["api", "items", "update"]:
            return {"id": inv.update_item(conn, body["item_id"], body["name"], body["reorder_point"], a)}
        if parts == ["api", "pos"]:
            return {"id": inv.create_po(conn, body["item_id"], body["qty"], body.get("key"), a)}
        if parts[:2] == ["api", "pos"] and parts[3] == "receive":
            return {"id": inv.receive(conn, int(parts[2]), body["qty"], body["key"], a)}
        if parts[:2] == ["api", "pos"] and parts[3] == "cancel":
            return {"cancelled": inv.cancel_remaining(conn, int(parts[2]), body.get("reason", ""), a)}
        if parts == ["api", "out"]:
            return {"id": inv.record_out(conn, body["item_id"], body["qty"], body["kind"], body["key"], a, body.get("note", ""))}
        if parts == ["api", "stocktake"]:
            return {"id": inv.stocktake(conn, body["item_id"], body["counted"], body["key"], a)}
        if parts == ["api", "correct"]:
            return {"id": inv.correct(conn, body["movement_id"], body["qty"], body["key"], body.get("reason", ""), a)}
    raise inv.InventoryError("알 수 없는 요청")


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # 소켓 읽기 제한 시간(초). 본문이 다 오지 않아도 스레드가 계속 붙잡혀 있지 않게 한다.

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlsplit(self.path)
        static = STATIC.get(url.path) if method == "GET" else None
        if static:
            name, ctype = static
            with open(os.path.join(HERE, name), "rb") as f:
                return self._send(200, f.read(), ctype)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > MAX_BODY:
                self.close_connection = True  # 읽지 않은 본문이 다음 요청으로 해석되지 않게 연결을 닫는다
                if n < 0:
                    raise inv.InventoryError("Content-Length가 올바르지 않습니다")
                return self._send(413, {"error": "요청 본문이 너무 큽니다"})
            raw = self.rfile.read(n)  # 다 오지 않으면 Handler.timeout 뒤 TimeoutError
            if len(raw) != n:
                raise inv.InventoryError("요청 본문이 Content-Length보다 짧습니다")
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise inv.InventoryError("요청 본문은 JSON 객체여야 합니다")
            conn = inv.connect(DB)
            try:
                self._send(200, route(conn, method, url.path, body, parse_qs(url.query)))
            finally:
                conn.close()
        except inv.InventoryError as e:
            self._send(400, {"error": str(e)})
        except (KeyError, ValueError, IndexError, TypeError, OverflowError):
            self._send(400, {"error": "잘못된 요청입니다"})
        except TimeoutError:
            self.close_connection = True
            self._send(408, {"error": "요청 본문을 제한 시간 안에 받지 못했습니다"})
        except sqlite3.Error:
            self._send(500, {"error": "저장 중 오류가 발생했습니다. 다시 시도해 주세요"})
        except Exception:  # noqa: BLE001 — 응답 없이 연결이 끊기지 않도록 마지막에 JSON으로 답한다
            self.log_error("처리되지 않은 오류:\n%s", traceback.format_exc())
            self._send(500, {"error": "서버 오류가 발생했습니다"})

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
