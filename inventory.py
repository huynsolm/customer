"""카페 재고 핵심 로직. 재고는 append-only 변동 이력(stock_movements)의 합계로 계산한다."""
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
    return conn


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


def _need_key(key):
    if not isinstance(key, str) or not key.strip():
        raise InventoryError("요청 키가 필요합니다")


def _replay(conn, key, **expect):
    """같은 키의 기록이 있으면 돌려준다. 키는 같은데 내용이 다르면 조용히 넘기지 않고 거부한다."""
    done = conn.execute("SELECT * FROM stock_movements WHERE idempotency_key=?", (key,)).fetchone()
    if done and any(done[k] != v for k, v in expect.items()):
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
        "INSERT INTO stock_movements(item_id,po_id,kind,delta,corrects_id,idempotency_key,actor,note)"
        " VALUES(:item_id,:po_id,:kind,:delta,:corrects_id,:key,:actor,:note)",
        {"po_id": None, "corrects_id": None, "actor": "", "note": "", **m},
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
    po = conn.execute(
        "SELECT p.*, i.name AS item_name, i.unit FROM purchase_orders p JOIN items i ON i.id=p.item_id"
        " WHERE p.id=?", (po_id,)).fetchone()
    if not po:
        raise InventoryError("발주를 찾을 수 없습니다")
    received = _po_received(conn, po_id)
    remaining = max(po["ordered_qty"] - po["cancelled_qty"] - received, 0)
    return dict(po, received=received, remaining=remaining, status=_po_status(po, received))


def list_pos(conn):
    ids = [r[0] for r in conn.execute("SELECT id FROM purchase_orders ORDER BY id DESC")]
    return [get_po(conn, i) for i in ids]


def history(conn, item_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stock_movements WHERE item_id=? ORDER BY id", (item_id,))]


def current_stock(conn, item_id):
    return _stock(conn, item_id)


# ---------- 발주·입고 ----------
def create_po(conn, item_id, qty, key=None):
    _need_positive(qty)
    with tx(conn):
        if key:
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
    _need_positive(qty)
    _need_key(key)
    with tx(conn):
        done = _replay(conn, key, kind="receipt", po_id=po_id, delta=qty)
        if done:
            return done["id"]
        po = get_po(conn, po_id)
        if qty > po["remaining"]:
            raise InventoryError("입고 수량(%d)이 주문 잔량(%d)을 초과합니다" % (qty, po["remaining"]))
        return _insert(conn, item_id=po["item_id"], po_id=po_id, kind="receipt", delta=qty,
                       key=key, actor=actor, note="발주 PO-%d 입고" % po_id)


def cancel_remaining(conn, po_id, reason):
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
    _need_positive(qty)
    _need_key(key)
    with tx(conn):
        done = _replay(conn, key, kind=kind, item_id=item_id, delta=-qty)
        if done:
            return done["id"]
        _need_item(conn, item_id)
        if qty > _stock(conn, item_id):
            raise InventoryError("재고(%d)보다 많이 기록할 수 없습니다" % _stock(conn, item_id))
        return _insert(conn, item_id=item_id, kind=kind, delta=-qty, key=key, actor=actor, note=note)


def stocktake(conn, item_id, counted, key, actor="", note=""):
    """마감. 실제 수량과 장부 차이를 조정 이력으로 남긴다."""
    _need_int(counted, 0, "실제 수량")
    _need_key(key)
    with tx(conn):
        done = _replay(conn, key, kind="adjustment", item_id=item_id)
        if done:
            return done["id"]
        _need_item(conn, item_id)
        diff = counted - _stock(conn, item_id)
        return _insert(conn, item_id=item_id, kind="adjustment", delta=diff, key=key, actor=actor,
                       note=note or "마감 실사(차이 %+d)" % diff)


# ---------- 정정 ----------
def correct(conn, movement_id, new_qty, key, reason, actor=""):
    """기록을 지우지 않고 정정 이력을 추가한다. new_qty=0이면 취소(void)."""
    _need_int(new_qty, 0, "정정 수량")
    _need_key(key)
    if not reason or not reason.strip():
        raise InventoryError("정정 사유가 필요합니다")
    with tx(conn):
        done = _replay(conn, key, kind="correction", corrects_id=movement_id)
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
        if m["po_id"]:
            po = get_po(conn, m["po_id"])
            if diff > po["remaining"]:
                if po["cancelled_qty"]:
                    raise InventoryError("잔량 %d개가 취소된 발주라 입고 수량을 늘릴 수 없습니다" % po["cancelled_qty"])
                raise InventoryError("정정하면 입고 수량이 주문 수량을 초과합니다")
        return _insert(conn, item_id=m["item_id"], po_id=m["po_id"], kind="correction", delta=diff,
                       corrects_id=movement_id, key=key, actor=actor,
                       note="#%d 정정 %d→%d: %s" % (movement_id, abs(applied), new_qty, reason))
