"""카페 재고 핵심 로직. 재고는 append-only 변동 이력(stock_movements)의 합계로 계산한다."""
import contextlib
import json
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stores (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, idempotency_key TEXT
);
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL DEFAULT 1 REFERENCES stores(id),
  name TEXT NOT NULL, unit TEXT NOT NULL,
  reorder_point INTEGER NOT NULL DEFAULT 0, idempotency_key TEXT, request TEXT,
  archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1))
);
CREATE TABLE IF NOT EXISTS item_changes (
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
  field TEXT NOT NULL CHECK (field IN ('name','reorder_point','archived')),
  old_value TEXT NOT NULL, new_value TEXT NOT NULL, actor TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_item_changes_item ON item_changes(item_id);
CREATE TABLE IF NOT EXISTS item_deletions (
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, store_id INTEGER NOT NULL,
  name TEXT NOT NULL, unit TEXT NOT NULL, opening_qty INTEGER NOT NULL,
  actor TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL UNIQUE,
  add_key TEXT, add_request TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_item_deletions_add_key ON item_deletions(add_key);
CREATE TABLE IF NOT EXISTS purchase_orders (
  id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id),
  ordered_qty INTEGER NOT NULL CHECK (ordered_qty > 0),
  cancelled_qty INTEGER NOT NULL DEFAULT 0,
  cancel_reason TEXT,
  created_by TEXT NOT NULL DEFAULT '',
  cancelled_by TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_movements_item ON stock_movements(item_id);
CREATE INDEX IF NOT EXISTS idx_movements_po ON stock_movements(po_id);
CREATE INDEX IF NOT EXISTS idx_movements_corrects ON stock_movements(corrects_id);
CREATE INDEX IF NOT EXISTS idx_movements_kind_time ON stock_movements(kind, created_at);
"""
# 이전 버전 DB에는 없는 컬럼에 거는 인덱스라 마이그레이션 뒤에 만든다.
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_store_name ON items(store_id, name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_key ON items(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stores_key ON stores(idempotency_key);
"""
DEFAULT_STORE = 1


class InventoryError(Exception):
    pass


def connect(path=":memory:"):
    # isolation_level=None: 트랜잭션은 직접 BEGIN IMMEDIATE로 관리한다.
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if path != ":memory:":
        _enable_wal(conn)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(INDEXES)
    return conn


def _enable_wal(conn):
    """여러 연결이 동시에 처음 열리면 WAL 전환이 busy timeout을 기다리지 않고 바로 실패할 수 있어 다시 시도한다."""
    for attempt in range(100):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if attempt == 99:
                raise
            time.sleep(0.05)


def _columns(conn, table):
    return {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def _needs_migration(conn):
    return ("request" not in _columns(conn, "stock_movements")
            or not {"created_by", "cancelled_by"} <= _columns(conn, "purchase_orders")
            or not {"store_id", "archived"} <= _columns(conn, "items")
            or not conn.execute("SELECT 1 FROM stores WHERE id=?", (DEFAULT_STORE,)).fetchone())


def _migrate(conn):
    """이전 버전 DB를 현재 구조로 올린다. 기존 기록은 지우지 않는다. 연결이 동시에 열려도 한 번만 실행한다."""
    if not _needs_migration(conn):
        return
    conn.execute("PRAGMA foreign_keys=OFF")  # 테이블을 다시 만드는 동안 참조 검사를 끈다(트랜잭션 밖에서만 바뀐다)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _needs_migration(conn):  # 기다리는 사이 다른 연결이 끝냈을 수 있다
                _migrate_locked(conn)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_locked(conn):
    if "request" not in _columns(conn, "stock_movements"):
        conn.execute("ALTER TABLE stock_movements ADD COLUMN request TEXT")  # 기존 기록은 request가 NULL
    for col in ("created_by", "cancelled_by"):
        if col not in _columns(conn, "purchase_orders"):
            conn.execute("ALTER TABLE purchase_orders ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % col)
    conn.execute("INSERT OR IGNORE INTO stores(id,name) VALUES(?, '본점')", (DEFAULT_STORE,))
    if "store_id" not in _columns(conn, "items"):
        # 이전 items는 name이 전체에서 유일(UNIQUE)이라 매장별로 같은 이름을 쓰려면 테이블을 다시 만들어야 한다.
        conn.execute("CREATE TABLE items_new (id INTEGER PRIMARY KEY,"
                     " store_id INTEGER NOT NULL DEFAULT 1 REFERENCES stores(id),"
                     " name TEXT NOT NULL, unit TEXT NOT NULL,"
                     " reorder_point INTEGER NOT NULL DEFAULT 0, idempotency_key TEXT, request TEXT,"
                     " archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1)))")
        conn.execute("INSERT INTO items_new(id,store_id,name,unit,reorder_point)"
                     " SELECT id,?,name,unit,reorder_point FROM items", (DEFAULT_STORE,))
        conn.execute("DROP TABLE items")
        conn.execute("ALTER TABLE items_new RENAME TO items")
    if "archived" not in _columns(conn, "items"):  # 매장 구분은 있고 보관 기능은 없던 버전
        conn.execute("ALTER TABLE items ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0,1))")


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


def _legacy_request(row):
    """request 없이 남은 이전 버전 기록에서 원래 요청을 복원한다. 복원할 수 없으면 None."""
    if row["kind"] == "receipt":
        return _request(op="receive", po_id=row["po_id"], qty=row["delta"])
    if row["kind"] in ("use", "waste"):
        return _request(op=row["kind"], item_id=row["item_id"], qty=-row["delta"])
    return None  # 기초 재고·실사·정정은 처음 받은 값(실사 수량, 정정 수량·사유)을 알 수 없다


def _replay(conn, key, request):
    """같은 키의 기록이 있으면 돌려준다. 키는 같은데 내용이 다르면 조용히 넘기지 않고 거부한다."""
    done = conn.execute("SELECT * FROM stock_movements WHERE idempotency_key=?", (key,)).fetchone()
    if not done:
        return None
    stored = done["request"] if done["request"] is not None else _legacy_request(done)
    if stored is None:
        raise InventoryError("이 요청 키는 이전 기록에 쓰였고 그 내용을 확인할 수 없어 다시 쓸 수 없습니다")
    if stored != request:
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


def _need_active_item(conn, item_id):
    row = conn.execute("SELECT archived FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise InventoryError("품목을 찾을 수 없습니다")
    if row["archived"]:
        raise InventoryError("보관된 품목입니다. 복원한 뒤 사용하세요")


def _insert(conn, **m):
    cur = conn.execute(
        "INSERT INTO stock_movements(item_id,po_id,kind,delta,corrects_id,idempotency_key,request,actor,note)"
        " VALUES(:item_id,:po_id,:kind,:delta,:corrects_id,:key,:request,:actor,:note)",
        {"po_id": None, "corrects_id": None, "request": None, "actor": "", "note": "", **m},
    )
    return cur.lastrowid


# ---------- 조회 ----------
MAX_NAME = 50


def _clean_name(v, label):
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > MAX_NAME:
        raise InventoryError("%s은 1~%d자의 글자여야 합니다" % (label, MAX_NAME))
    return v.strip()


def list_stores(conn):
    return [dict(r) for r in conn.execute("SELECT id, name FROM stores ORDER BY id")]


def add_store(conn, name, key=None):
    name = _clean_name(name, "매장 이름")
    if key is not None:
        _need_key(key)
    with tx(conn):
        if key is not None:
            done = conn.execute("SELECT * FROM stores WHERE idempotency_key=?", (key,)).fetchone()
            if done:
                if done["name"] != name:
                    raise InventoryError("같은 요청 키로 다른 매장이 이미 등록되어 있습니다")
                return done["id"]
        if conn.execute("SELECT 1 FROM stores WHERE name=?", (name,)).fetchone():
            raise InventoryError("같은 이름의 매장이 이미 있습니다")
        return conn.execute("INSERT INTO stores(name,idempotency_key) VALUES(?,?)", (name, key)).lastrowid


def update_store(conn, store_id, name):
    """매장 이름 변경. 같은 값으로 다시 호출해도 결과가 같다."""
    name = _clean_name(name, "매장 이름")
    with tx(conn):
        need_store(conn, store_id)
        if conn.execute("SELECT 1 FROM stores WHERE name=? AND id<>?", (name, store_id)).fetchone():
            raise InventoryError("같은 이름의 매장이 이미 있습니다")
        conn.execute("UPDATE stores SET name=? WHERE id=?", (name, store_id))
    return store_id


def need_store(conn, store_id):
    _need_id(store_id, "매장")
    if not conn.execute("SELECT 1 FROM stores WHERE id=?", (store_id,)).fetchone():
        raise InventoryError("매장을 찾을 수 없습니다")


def add_item(conn, name, unit, reorder_point=0, opening=0, key=None, store_id=DEFAULT_STORE, actor=""):
    """품목 추가. 같은 key로 다시 호출하면(더블클릭·재전송) 이전 결과를 돌려주고 품목은 늘지 않는다."""
    name = _clean_name(name, "품목 이름")
    unit = _clean_name(unit, "단위")
    _need_int(reorder_point, 0, "발주 기준")
    _need_int(opening, 0, "기초 재고")
    _need_text(actor, "작업자")
    if key is not None:
        _need_key(key)
    with tx(conn):
        return _add_item_locked(conn, name, unit, reorder_point, opening, key, store_id, actor)


def _add_item_locked(conn, name, unit, reorder_point, opening, key, store_id, actor):
    """add_item의 본문. 호출하는 쪽이 이미 쓰기 트랜잭션(tx)을 열어 둔 상태여야 하고, 입력은 검증된 값이다."""
    request = _request(op="add_item", store_id=store_id, name=name, unit=unit,
                       reorder_point=reorder_point, opening=opening)
    if key is not None:
        done = conn.execute("SELECT * FROM items WHERE idempotency_key=?", (key,)).fetchone()
        if done:
            if done["request"] != request:
                raise InventoryError("같은 요청 키로 다른 품목이 이미 등록되어 있습니다")
            return done["id"]
        # 그 요청으로 만든 품목이 나중에 삭제됐으면, 재전송이 삭제를 되돌리듯 품목을 다시 만들면 안 된다.
        gone = conn.execute("SELECT * FROM item_deletions WHERE add_key=?", (key,)).fetchone()
        if gone:
            if gone["add_request"] != request:
                raise InventoryError("같은 요청 키로 다른 품목이 이미 등록되어 있습니다")
            return gone["item_id"]
    need_store(conn, store_id)
    if conn.execute("SELECT 1 FROM items WHERE store_id=? AND name=?", (store_id, name)).fetchone():
        raise InventoryError("이 매장에 같은 이름의 품목이 이미 있습니다(보관된 품목이면 복원하세요)")
    # 품목 번호는 지운 뒤에도 다시 쓰지 않는다(SQLite는 마지막 번호를 지우면 재사용한다). 삭제 전 품목을
    # 가리키던 오래된 요청이 같은 번호의 새 품목에 적용되지 않게, 지운 품목 번호까지 포함해 다음 번호를 정한다.
    item_id = conn.execute(
        "SELECT MAX(COALESCE((SELECT MAX(id) FROM items),0), COALESCE((SELECT MAX(item_id) FROM item_deletions),0)) + 1"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO items(id,store_id,name,unit,reorder_point,idempotency_key,request) VALUES(?,?,?,?,?,?,?)",
        (item_id, store_id, name, unit, reorder_point, key, request))
    if opening:
        _insert(conn, item_id=item_id, kind="opening", delta=opening, key="opening-" + uuid.uuid4().hex,  # 사용자가 쓴 요청 키와 겹치지 않게 내부에서 만든다
                request=_request(op="opening", item_id=item_id, qty=opening), actor=actor, note="기초 재고")
    return item_id


def seed_once(conn, samples, flag="seeded"):
    """샘플 품목을 DB가 처음 만들어질 때 한 번만 넣는다. 확인·품목 추가·완료 표시가 한 트랜잭션이라
    중간에 실패하면 아무것도 남지 않고(다음 시작에 다시 시도), 동시에 시작한 서버는 한쪽만 넣는다.
    이미 품목이나 삭제 기록이 있는 DB는 샘플 없이 완료로만 표시한다. samples: (이름, 단위, 발주 기준, 기초 재고)."""
    clean = [(_clean_name(n, "품목 이름"), _clean_name(u, "단위"), r, o) for n, u, r, o in samples]
    for _, _, r, o in clean:
        _need_int(r, 0, "발주 기준")
        _need_int(o, 0, "기초 재고")
    with tx(conn):
        if conn.execute("SELECT 1 FROM meta WHERE key=?", (flag,)).fetchone():
            return False
        if not conn.execute("SELECT 1 FROM items UNION ALL SELECT 1 FROM item_deletions").fetchone():
            for n, u, r, o in clean:
                _add_item_locked(conn, n, u, r, o, None, DEFAULT_STORE, "")
        conn.execute("INSERT INTO meta(key,value) VALUES(?, '1')", (flag,))
        return True


def update_item(conn, item_id, name, reorder_point, actor=""):
    """품목 이름·발주 기준 수정. 같은 값으로 다시 호출해도 결과가 같다(재전송 안전). 단위는 기존 이력의 뜻이 바뀌므로 고치지 않는다."""
    _need_id(item_id, "품목")
    name = _clean_name(name, "품목 이름")
    _need_int(reorder_point, 0, "발주 기준")
    _need_text(actor, "작업자")
    with tx(conn):
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            raise InventoryError("품목을 찾을 수 없습니다")
        if conn.execute("SELECT 1 FROM items WHERE store_id=? AND name=? AND id<>?",
                        (item["store_id"], name, item_id)).fetchone():
            raise InventoryError("이 매장에 같은 이름의 품목이 이미 있습니다(보관된 품목이면 복원하세요)")
        # items.request는 처음 추가 요청의 지문이라 그대로 둔다. 수정 뒤에 추가 요청이 재전송돼도 처음 결과를 돌려준다.
        conn.execute("UPDATE items SET name=?, reorder_point=? WHERE id=?", (name, reorder_point, item_id))
        # 바뀐 항목만 이력에 남긴다(같은 값으로 다시 보내면 아무것도 남기지 않는다).
        for field, old, new in (("name", item["name"], name), ("reorder_point", item["reorder_point"], reorder_point)):
            if old != new:
                conn.execute("INSERT INTO item_changes(item_id,field,old_value,new_value,actor) VALUES(?,?,?,?,?)",
                             (item_id, field, str(old), str(new), actor))
    return item_id


def set_archived(conn, item_id, archived, actor=""):
    """품목 보관(삭제 대신 숨김)·복원. 이력은 그대로 남는다. 재고가 있거나 입고 예정이 남은 품목은 보관할 수 없다."""
    _need_id(item_id, "품목")
    if not isinstance(archived, bool):
        raise InventoryError("보관 여부는 true 또는 false여야 합니다")
    _need_text(actor, "작업자")
    with tx(conn):
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            raise InventoryError("품목을 찾을 수 없습니다")
        if bool(item["archived"]) == archived:
            return item_id  # 이미 그 상태(재전송·더블클릭)
        if archived:
            if _stock(conn, item_id):
                raise InventoryError("재고가 남아 있어 보관할 수 없습니다(마감 실사로 0을 맞춘 뒤 보관하세요)")
            for (po_id,) in conn.execute("SELECT id FROM purchase_orders WHERE item_id=?", (item_id,)).fetchall():
                if get_po(conn, po_id)["remaining"]:
                    raise InventoryError("입고 예정인 발주가 있어 보관할 수 없습니다(입고하거나 잔량을 취소하세요)")
        else:
            if conn.execute("SELECT 1 FROM items WHERE store_id=? AND name=? AND id<>? AND archived=0",
                            (item["store_id"], item["name"], item_id)).fetchone():
                raise InventoryError("같은 이름의 품목이 이미 있어 복원할 수 없습니다")
        conn.execute("UPDATE items SET archived=? WHERE id=?", (int(archived), item_id))
        conn.execute("INSERT INTO item_changes(item_id,field,old_value,new_value,actor) VALUES(?,?,?,?,?)",
                     (item_id, "archived", str(int(not archived)), str(int(archived)), actor))
    return item_id


def delete_item(conn, item_id, key, actor=""):
    """품목 완전 삭제. 기록을 지우지 않는 원칙이 있으므로, 입고·사용·폐기·마감·정정·발주 기록이 없는
    (기초 재고만 있는) 품목 = 실수로 만든 품목만 지울 수 있다. 그 밖에는 보관을 쓴다.
    삭제 사실은 item_deletions에 남긴다. 같은 key로 다시 호출하면(더블클릭·재전송) 처음 결과를 돌려준다.
    품목 번호는 지운 뒤에도 다시 쓰이지 않는다(add_item이 지운 번호보다 큰 번호만 준다). 재전송은 key로 알아본다."""
    _need_id(item_id, "품목")
    _need_key(key)
    _need_text(actor, "작업자")
    with tx(conn):
        done = conn.execute("SELECT * FROM item_deletions WHERE idempotency_key=?", (key,)).fetchone()
        if done:
            if done["item_id"] != item_id:
                raise InventoryError("같은 요청 키로 다른 품목 삭제가 이미 처리되었습니다")
            return item_id
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item:
            raise InventoryError("품목을 찾을 수 없습니다")
        if conn.execute("SELECT 1 FROM purchase_orders WHERE item_id=?", (item_id,)).fetchone():
            raise InventoryError("발주 기록이 있어 삭제할 수 없습니다. 보관을 사용하세요")
        moves = conn.execute("SELECT kind, delta FROM stock_movements WHERE item_id=?", (item_id,)).fetchall()
        if any(m["kind"] != "opening" for m in moves):
            raise InventoryError("입고·사용·폐기·마감·정정 기록이 있어 삭제할 수 없습니다. 보관을 사용하세요")
        # 삭제된 품목을 만든 추가 요청의 키·지문을 남겨, 그 요청이 나중에 재전송돼도 품목이 되살아나지 않게 한다.
        conn.execute("INSERT INTO item_deletions(item_id,store_id,name,unit,opening_qty,actor,idempotency_key,add_key,add_request)"
                     " VALUES(?,?,?,?,?,?,?,?,?)",
                     (item_id, item["store_id"], item["name"], item["unit"], sum(m["delta"] for m in moves), actor, key,
                      item["idempotency_key"], item["request"]))
        conn.execute("DELETE FROM item_changes WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM stock_movements WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    return item_id


def item_changes(conn, item_id):
    _need_id(item_id, "품목")
    return [dict(r) for r in conn.execute("SELECT * FROM item_changes WHERE item_id=? ORDER BY id", (item_id,))]


def list_items(conn, store_id=None, archived=False):
    """store_id가 None이면 모든 매장의 품목. 기본은 사용 중인 품목, archived=True면 보관된 품목만."""
    rows = conn.execute(
        "SELECT i.id, i.store_id, i.name, i.unit, i.reorder_point, COALESCE(SUM(m.delta),0) AS stock FROM items i"
        " LEFT JOIN stock_movements m ON m.item_id=i.id WHERE (?1 IS NULL OR i.store_id=?1) AND i.archived=?2"
        " GROUP BY i.id ORDER BY i.id", (store_id, int(archived))
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


def list_pos(conn, store_id=None):
    with snapshot(conn):
        ids = [r[0] for r in conn.execute(
            "SELECT p.id FROM purchase_orders p JOIN items i ON i.id=p.item_id"
            " WHERE (?1 IS NULL OR i.store_id=?1) ORDER BY p.id DESC", (store_id,))]
        return [get_po(conn, i) for i in ids]


def history(conn, item_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM stock_movements WHERE item_id=? ORDER BY id", (item_id,))]


def current_stock(conn, item_id):
    return _stock(conn, item_id)


USAGE_DAYS = 7   # 최근 며칠의 사용량으로 하루 평균을 낼지
COVER_DAYS = 7   # 권장 발주량이 버티게 할 일수 (가정: "일주일 쓸 만큼")

# 품목별 최근 사용·폐기량. 기간은 원래 기록의 시각으로 고르고, 그 기록의 정정(조회 시점까지)을 더한다.
# 정정 합계를 먼저 한 번 묶어 두고 조인한다(기록마다 정정을 다시 찾으면 기록 수의 제곱만큼 느려진다).
# 매개변수: (조회 시각, 기간 시작, 조회 시각)
USAGE_SQL = (
    "SELECT m.item_id, SUM(-(m.delta + COALESCE(c.fix, 0))) FROM stock_movements m"
    " LEFT JOIN (SELECT corrects_id, SUM(delta) AS fix FROM stock_movements"
    "   WHERE corrects_id IS NOT NULL AND created_at<=? GROUP BY corrects_id) c ON c.corrects_id=m.id"
    " WHERE m.kind IN ('use','waste') AND m.created_at>=? AND m.created_at<=?"
    " GROUP BY m.item_id"
)


def shortage_report(conn, now=None, store_id=None):
    """부족 품목과 권장 발주량. 숫자는 모두 여기서 계산하고, AI는 이 결과를 문장으로만 바꾼다."""
    with snapshot(conn):
        now = now or conn.execute("SELECT datetime('now','localtime')").fetchone()[0]
        since = conn.execute("SELECT datetime(?, ?)", (now, "-%d days" % USAGE_DAYS)).fetchone()[0]
        pos = list_pos(conn, store_id)
        items = list_items(conn, store_id)
        used_by_item = dict(conn.execute(USAGE_SQL, (now, since, now)).fetchall())
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
def create_po(conn, item_id, qty, key=None, actor=""):
    _need_id(item_id, "품목")
    _need_positive(qty)
    _need_text(actor, "작업자")
    if key is not None:
        _need_key(key)
    with tx(conn):
        if key is not None:
            done = conn.execute("SELECT * FROM purchase_orders WHERE idempotency_key=?", (key,)).fetchone()
            if done:
                if (done["item_id"], done["ordered_qty"]) != (item_id, qty):
                    raise InventoryError("같은 요청 키로 다른 발주가 이미 등록되어 있습니다")
                return done["id"]
        _need_active_item(conn, item_id)
        return conn.execute(
            "INSERT INTO purchase_orders(item_id,ordered_qty,idempotency_key,created_by) VALUES(?,?,?,?)",
            (item_id, qty, key, actor),
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


def cancel_remaining(conn, po_id, reason, actor=""):
    _need_id(po_id, "발주")
    _need_text(reason, "취소 사유")
    _need_text(actor, "작업자")
    with tx(conn):
        po = get_po(conn, po_id)
        if po["remaining"] == 0:
            raise InventoryError("취소할 잔량이 없습니다")
        conn.execute("UPDATE purchase_orders SET cancelled_qty=cancelled_qty+?, cancel_reason=?, cancelled_by=? WHERE id=?",
                     (po["remaining"], reason, actor, po_id))
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
        _need_active_item(conn, item_id)
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
        # 보관 조건(재고 0·입고 예정 0)을 보관된 동안에도 지킨다. 정정은 재고를 바꾸므로 복원한 뒤에만 한다.
        if conn.execute("SELECT archived FROM items WHERE id=?", (m["item_id"],)).fetchone()["archived"]:
            raise InventoryError("보관된 품목의 기록은 정정할 수 없습니다. 복원한 뒤 정정하세요")
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
