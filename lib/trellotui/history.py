"""Every version of every board object, kept forever in SQLite.

Trello forgets: delete a checklist and its items are gone, and the activity feed never
had them. So whenever a board is seen (the app syncing, `trello snapshot` on a timer,
or the app's own writes) each card, list, checklist, check item, label and comment is
compared with the last version we have, and any change is stored as a new version.
Something that disappeared gets a tombstone (data NULL); its last version holds what
it was, which is what a restore re-creates.

All rows written by one observation share `ts`, so a deleted card can be shown as one
event with its checklists and items folded in.
"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass

from .config import DATA_DIR

DB = DATA_DIR / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL, oid TEXT NOT NULL, board TEXT, card TEXT,
  ts REAL NOT NULL, source TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS v_obj ON versions(kind, oid, seq);
CREATE INDEX IF NOT EXISTS v_board ON versions(board, seq);
CREATE INDEX IF NOT EXISTS v_card ON versions(card, seq);
CREATE TABLE IF NOT EXISTS latest(
  kind TEXT NOT NULL, oid TEXT NOT NULL, board TEXT, card TEXT, seq INTEGER, data TEXT,
  PRIMARY KEY(kind, oid));
CREATE INDEX IF NOT EXISTS l_board ON latest(board);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS restored(kind TEXT, oid TEXT, ts REAL, PRIMARY KEY(kind, oid));
"""

# What history keeps of each kind (volatile fields such as dateLastActivity are left out).
FIELDS = {
    "board": ("name", "desc", "closed"),
    "list": ("name", "closed", "pos"),
    "card": ("name", "desc", "closed", "idList", "pos", "idLabels", "due", "dueComplete",
             "start", "idMembers"),
    "checklist": ("name", "idCard", "pos"),
    "item": ("name", "state", "pos", "idChecklist", "due", "idMember"),
    "label": ("name", "color"),
    "comment": ("text", "author", "date"),
}


def norm(kind: str, o: dict) -> dict:
    if kind == "comment":
        return {"text": o.get("data", {}).get("text", o.get("text", "")),
                "author": (o.get("memberCreator") or {}).get("fullName", o.get("author", "")),
                "date": o.get("date", "")}
    d = {k: o.get(k) for k in FIELDS[kind]}
    for k in ("idLabels", "idMembers"):
        if k in d and d[k] is not None:
            d[k] = sorted(d[k])
    return d


def dump(d: dict | None) -> str | None:
    return None if d is None else json.dumps(d, sort_keys=True, ensure_ascii=False)


def board_objects(board: dict, comments: list | None) -> dict:
    """(kind, oid) -> (card id, normalized data) for everything on a board."""
    out = {("board", board["id"]): (None, norm("board", board))}
    for l in board.get("lists", []):
        out[("list", l["id"])] = (None, norm("list", l))
    for c in board.get("cards", []):
        out[("card", c["id"])] = (c["id"], norm("card", c))
    for cl in board.get("checklists", []):
        out[("checklist", cl["id"])] = (cl["idCard"], norm("checklist", cl))
        for it in cl.get("checkItems", []):
            it = {**it, "idChecklist": cl["id"]}
            out[("item", it["id"])] = (cl["idCard"], norm("item", it))
    for lb in board.get("labels", []):
        out[("label", lb["id"])] = (None, norm("label", lb))
    for a in comments or []:
        card = (a.get("data", {}).get("card") or {}).get("id")
        out[("comment", a["id"])] = (card, norm("comment", a))
    return out


@dataclass
class Version:
    seq: int
    kind: str
    oid: str
    board: str
    card: str | None
    ts: float
    source: str
    data: dict | None
    prev: dict | None = None     # the version before this one (None = first seen / created)
    first: bool = False          # no earlier version at all


class History:
    def __init__(self, path=DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # -- key/value (board cache, settings)

    def get(self, k, default=None):
        with self.lock:
            row = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, json.dumps(v, ensure_ascii=False)))

    # -- recording

    def _write(self, rows, ts, source):
        """rows: (kind, oid, board, card, data-or-None). Stores the ones that differ."""
        n = 0
        cur = self.db.cursor()
        for kind, oid, board, card, data in rows:
            new = dump(data)
            old = cur.execute("SELECT data, card FROM latest WHERE kind=? AND oid=?", (kind, oid)).fetchone()
            if old is not None and old[0] == new:
                continue
            if old is None and new is None:
                continue
            if new is None and card is None and old is not None:
                card = old[1]
            cur.execute("INSERT INTO versions(kind, oid, board, card, ts, source, data) VALUES(?,?,?,?,?,?,?)",
                        (kind, oid, board, card, ts, source, new))
            cur.execute("INSERT OR REPLACE INTO latest VALUES(?,?,?,?,?,?)",
                        (kind, oid, board, card, cur.lastrowid, new))
            n += 1
        return n

    def snapshot(self, board: dict, comments: list | None, source="sync") -> int:
        """Record a full board as fetched from Trello. Returns how many versions were added."""
        bid = board["id"]
        seen = board_objects(board, comments)
        ts = time.time()
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if not self.db.execute("SELECT 1 FROM latest WHERE board=? LIMIT 1", (bid,)).fetchone():
                    source = "import"      # first look at this board: not a change, just where history starts
                rows = [(k, o, bid, card, data) for (k, o), (card, data) in seen.items()]
                # Gone since last time -> tombstone. With 1000 comments Trello may have cut older
                # ones off, so only comments newer than the oldest one fetched can be called gone.
                oldest = min((a["date"] for a in comments), default="") if comments and len(comments) >= 1000 else ""
                for kind, oid, card, data in self.db.execute(
                        "SELECT kind, oid, card, data FROM latest WHERE board=? AND data IS NOT NULL", (bid,)):
                    if (kind, oid) in seen:
                        continue
                    if kind == "comment" and (comments is None or json.loads(data)["date"] < oldest):
                        continue
                    rows.append((kind, oid, bid, card, None))
                n = self._write(rows, ts, source)
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return n

    def observe(self, kind: str, oid: str, board: str, card: str | None, obj: dict | None, source="app"):
        """Record one object the app just changed (obj None = it deleted it)."""
        with self.lock:
            self._write([(kind, oid, board, card, None if obj is None else norm(kind, obj))],
                        time.time(), source)

    def observe_deleted(self, items: list[tuple], source="app"):
        """Tombstone several objects at once (a card with its checklists...): (kind, oid, board, card)."""
        ts = time.time()
        with self.lock:
            self._write([(k, o, b, c, None) for k, o, b, c in items], ts, source)

    # -- reading

    def _versions(self, where: str, args: tuple, limit: int) -> list[Version]:
        with self.lock:
            rows = self.db.execute(
                f"SELECT seq, kind, oid, board, card, ts, source, data FROM versions WHERE {where} "
                "ORDER BY seq DESC LIMIT ?", (*args, limit)).fetchall()
            out = []
            for seq, kind, oid, board, card, ts, source, data in rows:
                prev = self.db.execute(
                    "SELECT data FROM versions WHERE kind=? AND oid=? AND seq<? ORDER BY seq DESC LIMIT 1",
                    (kind, oid, seq)).fetchone()
                out.append(Version(seq, kind, oid, board, card, ts, source,
                                   json.loads(data) if data else None,
                                   json.loads(prev[0]) if prev and prev[0] else None,
                                   first=prev is None))
        return out

    def card_history(self, card_id: str, limit=1000) -> list[Version]:
        return self._versions("card=?", (card_id,), limit)

    def board_history(self, board_id: str, limit=2000) -> list[Version]:
        return self._versions("board=?", (board_id,), limit)

    def deleted(self, board_id: str, limit=500) -> list[Version]:
        """Objects whose current state is deleted, newest deletion first."""
        with self.lock:
            seqs = [r[0] for r in self.db.execute(
                "SELECT seq FROM latest l WHERE board=? AND data IS NULL AND kind IN "
                "('card','checklist','item','comment') AND NOT EXISTS "
                "(SELECT 1 FROM restored r WHERE r.kind = l.kind AND r.oid = l.oid) "
                "ORDER BY seq DESC LIMIT ?", (board_id, limit))]
        if not seqs:
            return []
        return self._versions(f"seq IN ({','.join('?' * len(seqs))})", tuple(seqs), limit)

    def mark_restored(self, kind: str, oid: str):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO restored VALUES(?,?,?)", (kind, oid, time.time()))

    def is_restored(self, kind: str, oid: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM restored WHERE kind=? AND oid=?", (kind, oid)).fetchone() is not None

    def last_data(self, kind: str, oid: str) -> dict | None:
        """The last version in which the object still existed."""
        with self.lock:
            row = self.db.execute("SELECT data FROM versions WHERE kind=? AND oid=? AND data IS NOT NULL "
                                  "ORDER BY seq DESC LIMIT 1", (kind, oid)).fetchone()
        return json.loads(row[0]) if row else None

    def alive(self, kind: str, oid: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT data FROM latest WHERE kind=? AND oid=?", (kind, oid)).fetchone()
        return bool(row and row[0])

    def deleted_children(self, kind: str, parent: str, field: str, around: float) -> list[tuple[str, dict]]:
        """Children (by `field` == parent) that were deleted, with their last data. `around` is the
        parent's deletion time: children deleted earlier on their own are left out."""
        with self.lock:
            rows = self.db.execute(
                "SELECT l.oid, v.ts FROM latest l JOIN versions v ON v.seq = l.seq "
                "WHERE l.kind=? AND l.data IS NULL", (kind,)).fetchall()
        out = []
        for oid, ts in rows:
            if ts < around - 1 or self.is_restored(kind, oid):
                continue
            data = self.last_data(kind, oid)
            if data and data.get(field) == parent:
                out.append((oid, data))
        out.sort(key=lambda x: x[1].get("pos") or 0)
        return out

    def deleted_comments(self, card_id: str, around: float) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT l.oid, v.ts FROM latest l JOIN versions v ON v.seq = l.seq "
                "WHERE l.kind='comment' AND l.card=? AND l.data IS NULL", (card_id,)).fetchall()
        out = [self.last_data("comment", oid) for oid, ts in rows
               if ts >= around - 1 and not self.is_restored("comment", oid)]
        return sorted((c for c in out if c), key=lambda c: c["date"])
