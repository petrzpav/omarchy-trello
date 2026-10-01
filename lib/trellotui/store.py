"""Local-first access to Trello.

Boards are kept on disk (in the history database) and painted from there at once;
two lanes work behind the UI, each strictly in order on its own thread: `fg` sends
what the user just did, `bg` syncs boards. Every board fetched also goes into the
history, and every write the app makes is recorded the moment Trello confirms it.
"""

import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import restore
from .api import Trello
from .config import DATA_DIR, ERROR_LOG, Config
from .history import History

DUE_FILE = DATA_DIR / "due"


def log_error(where: str):
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a") as f:
            f.write(f"--- {time.strftime('%F %T')} {where}\n{traceback.format_exc()}\n")
    except OSError:
        pass


def count_due(boards: list[dict], me: str) -> int:
    """Open cards of mine due within a day (or overdue) and not done."""
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    n = 0
    for b in boards:
        open_lists = {l["id"] for l in b.get("lists", []) if not l.get("closed")}
        for c in b.get("cards", []):
            if (c.get("due") and not c.get("dueComplete") and not c.get("closed")
                    and c.get("idList") in open_lists and me in (c.get("idMembers") or [])
                    and datetime.fromisoformat(c["due"].replace("Z", "+00:00")) <= soon):
                n += 1
    return n


class Store:
    def __init__(self, cfg: Config, call_ui, api=None, history=None):
        self.cfg = cfg
        self.call_ui = call_ui
        self.api = api or Trello(cfg.api_key, cfg.token)
        self.history = history or History()
        self._pools = {"fg": ThreadPoolExecutor(1, "trello-fg"), "bg": ThreadPoolExecutor(1, "trello-bg")}
        self.inflight = 0          # writes queued or running
        self.write_gen = 0         # bumps when a write finishes
        self.real: dict[str, str] = {}   # tmp id of something just created -> its Trello id

    def submit(self, lane: str, fn, done=None, error=None):
        if lane == "fg":
            self.inflight += 1

        def job():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001 - reported to the UI, always logged
                log_error(lane)
                if error:
                    self.call_ui(error, e)
                return
            finally:
                if lane == "fg":
                    self.inflight -= 1
                    self.write_gen += 1
            if done:
                self.call_ui(done, res)
        self._pools[lane].submit(job)

    # -- cache

    def cached_board(self, bid: str):
        return self.history.get(f"board:{bid}"), self.history.get(f"comments:{bid}", [])

    def cached_boards(self) -> list[dict]:
        return self.history.get("boards", [])

    def me(self) -> dict:
        me = self.history.get("me")
        if not me:
            me = self.api.me()
            self.history.put("me", me)
        return me

    # -- fetching (bg lane)

    def fetch_boards(self) -> list[dict]:
        boards = self.api.boards()
        self.history.put("boards", boards)
        return boards

    def fetch_board(self, bid: str, check_writes=True):
        """Fetch, record in history, cache. Returns None when a write happened meanwhile
        (the result might not show it yet; the caller syncs again)."""
        gen = self.write_gen
        board = self.api.board(bid)
        comments = self.api.comments(bid)
        if check_writes and (self.inflight or gen != self.write_gen):
            return None
        self.history.snapshot(board, comments)
        self.history.put(f"board:{bid}", board)
        self.history.put(f"comments:{bid}", comments)
        self.history.put(f"seen:{bid}", board.get("dateLastActivity"))
        self.write_due()
        return board, comments

    def write_due(self):
        try:
            me = self.history.get("me") or {}
            boards = [self.history.get(f"board:{b['id']}") for b in self.cached_boards()]
            DUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            DUE_FILE.write_text(f"{count_due([b for b in boards if b], me.get('id', ''))}\n")
        except Exception:  # noqa: BLE001 - the bar count is best effort
            log_error("due")

    def snapshot_all(self, force=False) -> dict:
        """For the timer: record every board that changed since we last saw it."""
        self.me()
        out = {}
        for b in self.fetch_boards():
            if not force and self.history.get(f"seen:{b['id']}") == b.get("dateLastActivity"):
                last = self.history.get(f"snap:{b['id']}", 0)
                if time.time() - last < 3600:     # hourly full look anyway: deletions may not bump activity
                    continue
            board = self.api.board(b["id"])
            comments = self.api.comments(b["id"])
            out[b["name"]] = self.history.snapshot(board, comments, source="timer")
            self.history.put(f"board:{b['id']}", board)
            self.history.put(f"comments:{b['id']}", comments)
            self.history.put(f"seen:{b['id']}", board.get("dateLastActivity"))
            self.history.put(f"snap:{b['id']}", time.time())
        self.write_due()
        return out

    # -- recording writes (fg lane)

    def rec(self, kind: str, obj: dict | None, board: str, card: str | None = None, oid: str | None = None):
        try:
            self.history.observe(kind, oid or obj["id"], board, card, obj)
        except Exception:  # noqa: BLE001 - history must never break a write
            log_error("history")
        return obj

    def restore(self, version, board: dict) -> str:
        return restore.restore(self.api, self.history, version, board)
