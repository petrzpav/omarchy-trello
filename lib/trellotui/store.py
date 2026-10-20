"""Local-first access to Trello.

Boards are kept on disk (in the history database) and painted from there at once;
three lanes work behind the UI, each strictly in order on its own thread: `fg` sends
what the user just did, `now` re-reads the board on screen, `bg` syncs the other boards
(so the board you look at never waits behind them). Every board fetched also goes into
the history, and every write the app makes is recorded the moment Trello confirms it.

Several processes share the cache (the client, the CLI, the timer): each write stamps
its board (`wrote:`), and a fetch that began before a newer write or a newer fetch is
thrown away instead of overwriting the cache with something older (History.save_board).
"""

import threading
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
        self._pools = {lane: ThreadPoolExecutor(1, f"trello-{lane}") for lane in ("fg", "now", "bg")}
        self._io = ThreadPoolExecutor(4, "trello-io")    # the requests of one fetch, side by side
        self._lock = threading.Lock()   # inflight / write_gen change on the UI and the fg thread
        self._due_lock = threading.Lock()
        self._due: dict[str, int] | None = None     # board id -> my cards due soon (for the bar)
        self.inflight = 0          # writes queued or running
        self.write_gen = 0         # bumps when a write finishes
        self.real: dict[str, str] = {}   # tmp id of something just created -> its Trello id

    def submit(self, lane: str, fn, done=None, error=None):
        if lane == "fg":
            with self._lock:
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
                    with self._lock:
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
        self.history.put("fetched_at:boards", time.time())
        return boards

    def get_board(self, bid: str) -> tuple[dict, list]:
        """The board and its comments from Trello, both requests at once."""
        comments = self._io.submit(self.api.comments, bid)
        return self.api.board(bid), comments.result()

    def fetch_board(self, bid: str, check_writes=True):
        """Fetch, record in history, cache. Returns None when a write happened meanwhile, here or
        in another process (the result might not show it yet; the caller syncs again).
        `check_writes=False` (the CLI) looks once more instead and always returns the board."""
        for attempt in range(1 if check_writes else 2):
            gen, t0 = self.write_gen, time.time()
            board, comments = self.get_board(bid)
            if check_writes and (self.inflight or gen != self.write_gen):
                return None
            if self.history.save_board(board, comments, t0) is not None:
                self.write_due(board)
                return board, comments
        return None if check_writes else (board, comments)

    def stale(self, bid: str, activity: str | None = None) -> bool:
        """Is the cached copy of a board older than Trello's (`activity` from the board list) or
        than a write made to it since it was fetched?"""
        h = self.history
        if not h.has(f"board:{bid}"):
            return True
        if h.get(f"wrote:{bid}", 0) > h.get(f"fetched_at:{bid}", 0):
            return True
        return activity is not None and activity != h.get(f"seen:{bid}")

    def write_due(self, board: dict | None = None):
        """The bar's count. With `board` (just fetched) only that board is counted again; the
        others keep the count from the last full pass, so a sync doesn't decode every board."""
        try:
            me = (self.history.get("me") or {}).get("id", "")
            with self._due_lock:
                if self._due is None or board is None:
                    self._due = {}
                    for b in self.cached_boards():
                        cached = self.history.get(f"board:{b['id']}")
                        if cached:
                            self._due[b["id"]] = count_due([cached], me)
                else:
                    self._due[board["id"]] = count_due([board], me)
                n = sum(self._due.values())
            DUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            DUE_FILE.write_text(f"{n}\n")
        except Exception:  # noqa: BLE001 - the bar count is best effort
            log_error("due")

    def snapshot_all(self, force=False) -> dict:
        """For the timer: record every board that changed since we last saw it."""
        self.me()
        todo = []
        for b in self.fetch_boards():
            if not force and self.history.get(f"seen:{b['id']}") == b.get("dateLastActivity"):
                last = self.history.get(f"snap:{b['id']}", 0)
                if time.time() - last < 3600:     # hourly full look anyway: deletions may not bump activity
                    continue
            todo.append(b)

        def one(b):
            for _ in range(2):             # a newer write or fetch got there first: look once more
                t0 = time.time()
                board, comments = self.get_board(b["id"])
                n = self.history.save_board(board, comments, t0, source="timer")
                if n is not None:
                    self.history.put(f"snap:{b['id']}", time.time())
                    return b["name"], n
            return b["name"], 0
        with ThreadPoolExecutor(3, "trello-snap") as pool:
            out = dict(pool.map(one, todo))
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
