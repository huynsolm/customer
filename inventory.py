"""카페 재고 핵심 로직. 재고는 append-only 변동 이력(stock_movements)의 합계로 계산한다."""
import contextlib
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, unit TEXT NOT NULL,
  reorder_point INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS purchase_orders (
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
  ordered_qty INTEGER NOT NULL CHECK (ordered_qty > 0),
  cancelled_qty INTEGER NOT NULL DEFAULT 0,
  cancel_reason TEXT,
  idempotency_key TEXT UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS stock_movements (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  po_id INTEGER REFERENCES purchase_orders(id),
  kind TEXT NOT NULL CHECK (kind IN ('opening','receipt','use','waste','correction','adjustment')),
  delta INTEGER NOT NULL,
  corrects_id INTEGER REFERENCES stock_movements(id),
  idempotency_key TEXT NOT NULL UNIQUE,
  request TEXT,
  actor TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


class InventoryError(Exception):
    pass


def connect(path=":memory:"):
    # isolation_level=None: 트랜잭션은 직접 BEGIN IMMEDIATE로 관리한다.
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # 이전 버전 DB에는 request 컬럼이 없다. 기존 기록은 request가 NULL로 남는다.
    if "request" not in {r["name"] for r in conn.execute("PRAGMA table_info(stock_movements)")}:
        conn.execute("ALTER TABLE stock_movements ADD COLUMN request TEXT")
    return conn


@contextlib.contextmanager
def snapshot(conn):
    """여러 번 읽어도 한 시점의 데이터를 보도록 읽기 트랜잭션으로 묶는다. 이미 트랜잭션 안이면 그대로 쓴다."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")


class tx:
    """쓰기 트랜잭션. BEGIN IMMEDIATE로 쓰기를 직렬화해 동시 기록에도 합계가 맞는다."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.execute("ROLLBACK")
            return
        try:
            self.conn.execute("COMMIT")
        except sqlite3.Error:
            # COMMIT이 실패하면 트랜잭션이 열린 채 남아 이 연결의 다음 BEGIN이 모두 실패한다.
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise


MAX_QTY = 1_000_000


def _need_int(v, minimum, label):
    if not isinstance(v, int) or isinstance(v, bool) or not minimum <= v <= MAX_QTY:
        raise InventoryError("%s은 %d 이상 %d 이하의 정수여야 합니다" % (label, minimum, MAX_QTY))


MAX_ID = 2 ** 63 - 1


def _need_id(v, label):
    if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= MAX_ID:
        raise InventoryError("%s 번호가 올바르지 않습니다" % label)


def _need_text(v, label, required=False):
    if not isinstance(v, str) or (required and not v.strip()):
        raise InventoryError("%s%s" % (label, "이 필요합니다" if required else "은 문자열이어야 합니다"))


def _need_key(key):
    if not isinstance(key, str) or not key.strip():
        raise InventoryError("요청 키가 필요합니다")


def _request(**kw):
    """요청 본문의 지문. 같은 키로 다시 온 요청이 처음과 같은지 비교하는 데 쓴다."""
    return json.dumps(kw, sort_keys=True, ensure_ascii=False)


def _replay(conn, key, request):
    """같은 키의 기록이 있으면 돌려준다. 키는 같은데 내용이 다르면 조용히 넘기지 않고 거부한다."""
    done = conn.execute("SELECT * FROM stock_movements WHERE idempotency_key=?", (key,)).fetchone()
    if done and done["request"] is not None and done["request"] != request:
        raise InventoryError("같은 요청 키로 다른 내용이 이미 기록되어 있습니다")
    return done


def _stock(conn, item_id):
    return conn.execute(
        "SELECT COALESCE(SUM(delta),0) FROM stock_movements WHERE item_id=?", (item_id,)
    ).fetchone()[0]


def _po_received(conn, po_id):
    return conn.execute(
        "SELECT COALESCE(SUM(delta),0) FROM stock_movements WHERE po_id=?", (po_id,)
    ).fetchone()[0]


def _po_status(po, received):
    if received >= po["ordered_qty"] - po["cancelled_qty"]:
        return "종결(미입고 %d)" % po["cancelled_qty"] if po["cancelled_qty"] else "입고 완료"
    return "부분 입고" if received > 0 else "발주됨"


def _need_positive(qty):
    _need_int(qty, 1, "수량")


def _need_item(conn, item_id):
    if not conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
        raise InventoryError("품목을 찾을 수 없습니다")


def _insert(conn, **m):
    cur = conn.execute(
        "INSERT INTO stock_movements(item_id,po_id,kind,delta,corrects_id,idempotency_key,request,actor,note)"
        " VALUES(:item_id,:po_id,:kind,:delta,:corrects_id,:key,:request,:actor,:note)",
        {"po_id": None, "corrects_id": None, "request": None, "actor": "", "note": "", **m},
    )
    return cur.lastrowid


# ---------- 조회 ----------
def add_item(conn, name, unit, reorder_point=0, opening=0, key=None):
    _need_int(reorder_point, 0, "발주 기준")
    _need_int(opening, 0, "기초 재고")
    with tx(conn):
        item_id = conn.execute(
            "INSERT INTO items(name,unit,reorder_point) VALUES(?,?,?)", (name, unit, reorder_point)
        ).lastrowid
        if opening:
            _insert(conn, item_id=item_id, kind="opening", delta=opening,
                    key=key or "opening-%d" % item_id, note="기초 재고")
    return item_id


def list_items(conn):
    rows = conn.execute(
        "SELECT i.*, COALESCE(SUM(m.delta),0) AS stock FROM items i"
        " LEFT JOIN stock_movements m ON m.item_id=i.id GROUP BY i.id ORDER BY i.id"
    ).fetchall()
    return [dict(r, low=r["stock"] <= r["reorder_point"]) for r in rows]


def get_po(conn, po_id):
    _need_id(po_id, "발주")
    with snapshot(conn):
        po = conn.execute(
            "SELECT p.*, i.name AS item_name, i.unit FROM purchase_orders p JOIN items i ON i.id=p.item_id"
            " WHERE p.id=?", (po_id,)).fetchone()
        if not po:
            raise InventoryError("발주를 찾을 수 없습니다")
        received = _po_received(conn, po_id)
    remaining = max(po["ordered_qty"] - po["cancelled_qty"] - received, 0)
    return dict(po, received=received, remaining=remaining, status=_po_status(po, received))


def list_pos(conn):
    with snapshot(conn):
        ids = [r[0] for r in conn.execute("SELECT id FROM purchase_orders ORDER BY id DESC")]
        return [get_po(conn, i) for i in ids]


def history(conn, item_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stock_movements WHERE item_id=? ORDER BY id", (item_id,))]


def current_stock(conn, item_id):
    return _stock(conn, item_id)


USAGE_DAYS = 7   # 최근 며칠의 사용량으로 하루 평균을 낼지
COVER_DAYS = 7   # 권장 발주량이 버티게 할 일수 (가정: "일주일 쓸 만큼")


def shortage_report(conn, now=None):
    """부족 품목과 권장 발주량. 숫자는 모두 여기서 계산하고, AI는 이 결과를 문장으로만 바꾼다."""
    with snapshot(conn):
        now = now or conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
        since = conn.execute("SELECT datetime(?, ?)", (now, "-%d days" % USAGE_DAYS)).fetchone()[0]
        pos = list_pos(conn)
        items = list_items(conn)
        # 기간은 원래 사용·폐기 기록의 시각으로 고르고, 그 기록의 정정(조회 시점까지)을 더한다.
        used_by_item = dict(conn.execute(
            "SELECT m.item_id, SUM(-(m.delta + COALESCE((SELECT SUM(c.delta) FROM stock_movements c"
            "   WHERE c.corrects_id=m.id AND c.created_at<=?), 0)))"
            " FROM stock_movements m WHERE m.kind IN ('use','waste') AND m.created_at>=? AND m.created_at<=?"
            " GROUP BY m.item_id", (now, since, now)).fetchall())
    rows = []
    for it in items:
        used = max(used_by_item.get(it["id"], 0), 0)
        incoming = sum(p["remaining"] for p in pos if p["item_id"] == it["id"])
        available = it["stock"] + incoming
        # 정수 올림 나눗셈: (29/7)*7 같은 부동소수점 오차로 1개가 더 붙지 않게 한다.
        target = it["reorder_point"] + (used * COVER_DAYS + USAGE_DAYS - 1) // USAGE_DAYS
        rows.append({
            "item_id": it["id"], "name": it["name"], "unit": it["unit"],
            "stock": it["stock"], "incoming": incoming, "reorder_point": it["reorder_point"],
            "daily_use": round(used / USAGE_DAYS, 1),
            "days_left": round(it["stock"] * USAGE_DAYS / used, 1) if used else None,
            "short": available <= it["reorder_point"],
            "suggested_qty": max(target - available, 0),
        })
    return rows


# ---------- 발주·입고 ----------
def create_po(conn, item_id, qty, key=None):
    _need_id(item_id, "품목")
    _need_positive(qty)
    if key is not None:
        _need_key(key)
    with tx(conn):
        if key is not None:
            done = conn.execute("SELECT * FROM purchase_orders WHERE idempotency_key=?", (key,)).fetchone()
            if done:
                if (done["item_id"], done["ordered_qty"]) != (item_id, qty):
                    raise InventoryError("같은 요청 키로 다른 발주가 이미 등록되어 있습니다")
                return done["id"]
        _need_item(conn, item_id)
        return conn.execute(
            "INSERT INTO purchase_orders(item_id,ordered_qty,idempotency_key) VALUES(?,?,?)",
            (item_id, qty, key),
        ).lastrowid


def receive(conn, po_id, qty, key, actor=""):
    """입고. 같은 key로 다시 호출하면 이전 결과를 돌려주고 재고는 늘지 않는다."""
    _need_id(po_id, "발주")
    _need_positive(qty)
    _need_key(key)
    _need_text(actor, "작업자")
    request = _request(op="receive", po_id=po_id, qty=qty)
    with tx(conn):
        done = _replay(conn, key, request)
        if done:
            return done["id"]
        po = get_po(conn, po_id)
        if qty > po["remaining"]:
            raise InventoryError("입고 수량(%d)이 주문 잔량(%d)을 초과합니다" % (qty, po["remaining"]))
        return _insert(conn, item_id=po["item_id"], po_id=po_id, kind="receipt", delta=qty,
                       key=key, request=request, actor=actor, note="발주 PO-%d 입고" % po_id)


def cancel_remaining(conn, po_id, reason):
    _need_id(po_id, "발주")
    _need_text(reason, "취소 사유")
    with tx(conn):
        po = get_po(conn, po_id)
        if po["remaining"] == 0:
            raise InventoryError("취소할 잔량이 없습니다")
        conn.execute("UPDATE purchase_orders SET cancelled_qty=cancelled_qty+?, cancel_reason=? WHERE id=?",
                     (po["remaining"], reason, po_id))
        return po["remaining"]


# ---------- 사용·폐기·마감 ----------
def record_out(conn, item_id, qty, kind, key, actor="", note=""):
    """사용(use)·폐기(waste). 재고가 음수가 되면 거부한다."""
    if kind not in ("use", "waste"):
        raise InventoryError("kind는 use 또는 waste")
    _need_id(item_id, "품목")
    _need_positive(qty)
    _need_key(key)
    _need_text(actor, "작업자")
    _need_text(note, "비고")
    request = _request(op=kind, item_id=item_id, qty=qty)
    with tx(conn):
        done = _replay(conn, key, request)
        if done:
            return done["id"]
        _need_item(conn, item_id)
        if qty > _stock(conn, item_id):
            raise InventoryError("재고(%d)보다 많이 기록할 수 없습니다" % _stock(conn, item_id))
        return _insert(conn, item_id=item_id, kind=kind, delta=-qty, key=key, request=request,
                       actor=actor, note=note)


def stocktake(conn, item_id, counted, key, actor="", note=""):
    """마감. 실제 수량과 장부 차이를 조정 이력으로 남긴다."""
    _need_id(item_id, "품목")
    _need_int(counted, 0, "실제 수량")
    _need_key(key)
    _need_text(actor, "작업자")
    _need_text(note, "비고")
    # 재요청 시점의 재고로 역산하면 그 사이 거래 때문에 틀리므로, 처음 받은 실사 수량을 그대로 비교한다.
    request = _request(op="stocktake", item_id=item_id, counted=counted)
    with tx(conn):
        done = _replay(conn, key, request)
        if done:
            return done["id"]
        _need_item(conn, item_id)
        diff = counted - _stock(conn, item_id)
        return _insert(conn, item_id=item_id, kind="adjustment", delta=diff, key=key, request=request,
                       actor=actor, note=note or "마감 실사(차이 %+d)" % diff)


# ---------- 정정 ----------
def correct(conn, movement_id, new_qty, key, reason, actor=""):
    """기록을 지우지 않고 정정 이력을 추가한다. new_qty=0이면 취소(void)."""
    _need_id(movement_id, "기록")
    _need_int(new_qty, 0, "정정 수량")
    _need_key(key)
    _need_text(reason, "정정 사유", required=True)
    _need_text(actor, "작업자")
    request = _request(op="correct", movement_id=movement_id, qty=new_qty, reason=reason)
    with tx(conn):
        done = _replay(conn, key, request)
        if done:
            return done["id"]
        m = conn.execute("SELECT * FROM stock_movements WHERE id=?", (movement_id,)).fetchone()
        if not m or m["kind"] in ("correction", "adjustment"):
            raise InventoryError("정정할 수 없는 기록입니다")
        # 마감 실사가 이미 실물에 맞춰 차이를 조정했으므로, 그 이전 기록을 정정하면 차이가 두 번 반영된다.
        if conn.execute("SELECT 1 FROM stock_movements WHERE item_id=? AND kind='adjustment' AND id>?",
                        (m["item_id"], movement_id)).fetchone():
            raise InventoryError("마감 실사 이전 기록은 정정할 수 없습니다(차이는 이미 마감 조정에 반영됨)")
        applied = m["delta"] + conn.execute(
            "SELECT COALESCE(SUM(delta),0) FROM stock_movements WHERE corrects_id=?", (movement_id,)
        ).fetchone()[0]
        sign = 1 if m["delta"] > 0 else -1
        diff = sign * new_qty - applied
        if diff == 0:
            raise InventoryError("변경 사항이 없습니다")
        if _stock(conn, m["item_id"]) + diff < 0:
            raise InventoryError("정정하면 재고가 음수가 됩니다")
        po = get_po(conn, m["po_id"]) if m["po_id"] else None
        if po and po["cancelled_qty"] and diff > 0:
            raise InventoryError("잔량 %d개가 취소된 발주라 입고 수량을 늘릴 수 없습니다" % po["cancelled_qty"])
        if po and diff > po["remaining"]:
            raise InventoryError("정정하면 입고 수량이 주문 수량을 초과합니다")
        mid = _insert(conn, item_id=m["item_id"], po_id=m["po_id"], kind="correction", delta=diff,
                      corrects_id=movement_id, key=key, request=request, actor=actor,
                      note="#%d 정정 %d→%d: %s" % (movement_id, abs(applied), new_qty, reason))
        if po and po["cancelled_qty"] and diff < 0:
            # 취소로 종결된 발주는 다시 열지 않는다. 줄어든 입고분도 오지 않는 것으로 보고 미입고에 더한다.
            conn.execute("UPDATE purchase_orders SET cancelled_qty=cancelled_qty+? WHERE id=?", (-diff, po["id"]))
        return mid
