"""The Textual client: a board as columns of cards, a card as one focused page.

Everything is painted from the local copy at once; writes are applied on screen
first and sent to Trello behind it (in order), so nothing waits for the network.
"""

import itertools
import subprocess
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .config import Config
from .history import Version
from .store import Store

SYNC_AFTER_WRITE = 4      # seconds of quiet after a write before re-reading the board
FULL_SYNC = 600           # re-read a board fully at least this often, even if Trello says nothing changed

LABEL_COLORS = {
    "green": "#4bce97", "yellow": "#f5cd47", "orange": "#fea362", "red": "#f87168",
    "purple": "#9f8fef", "blue": "#579dff", "sky": "#6cc3e0", "lime": "#94c748",
    "pink": "#e774bb", "black": "#8590a2",
}

_tmp = itertools.count(1)


def tmp_id() -> str:
    return f"tmp{next(_tmp)}"


def fold(s: str) -> str:
    """Lowercase, accents stripped, same length (so match offsets still fit the original)."""
    return "".join(unicodedata.normalize("NFD", ch)[0] for ch in (s or "")).lower()


def label_color(color: str | None) -> str:
    return LABEL_COLORS.get((color or "").split("_")[0], "#8590a2")


def pretty(key: str) -> str:
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in key.split("+"))


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()


def short_date(d: datetime | None, with_time=False) -> str:
    if not d:
        return ""
    now = datetime.now().astimezone()
    fmt = "%-d %b" if d.year == now.year else "%-d %b %Y"
    if with_time and (d.hour, d.minute) != (0, 0):
        fmt += " %H:%M"
    return d.strftime(fmt)


def parse_due(s: str) -> str | None:
    """'' clears; accepts 2026-10-05, 5.10., 5.10.2026 14:00, today, tomorrow, +3d, +2w."""
    s = s.strip().lower()
    if not s:
        return None
    now = datetime.now().astimezone()
    day = None
    hm = (12, 0)
    parts = s.split()
    if len(parts) == 2 and ":" in parts[1]:
        h, m = parts[1].split(":")
        hm = (int(h), int(m))
        s = parts[0]
    if s in ("today", "dnes"):
        day = now
    elif s in ("tomorrow", "zitra", "zítra"):
        day = now + timedelta(days=1)
    elif s.startswith("+") and s[-1] in "dw" and s[1:-1].isdigit():
        day = now + timedelta(days=int(s[1:-1]) * (7 if s[-1] == "w" else 1))
    elif "-" in s:
        day = datetime.strptime(s, "%Y-%m-%d").astimezone()
    elif "." in s:
        bits = [b for b in s.split(".") if b]
        d, m = int(bits[0]), int(bits[1])
        y = int(bits[2]) if len(bits) > 2 else now.year
        day = datetime(y, m, d).astimezone()
    else:
        raise ValueError(f"don't understand “{s}”")
    day = day.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    return day.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def between(before: float | None, after: float | None) -> float:
    if before is None and after is None:
        return 65536
    if before is None:
        return after / 2
    if after is None:
        return before + 65536
    return (before + after) / 2


# ------------------------------------------------------------ the board in memory

class Model:
    """Indexes over the board JSON. The JSON dicts are edited in place by optimistic writes."""

    def __init__(self, board: dict | None, comments: list | None):
        self.b = board or {"id": "", "name": "", "lists": [], "cards": [], "checklists": [],
                           "labels": [], "members": []}
        self.comments = comments or []
        self.reindex()

    def reindex(self):
        b = self.b
        self.lists = {l["id"]: l for l in b["lists"]}
        self.cards = {c["id"]: c for c in b["cards"]}
        self.labels = {l["id"]: l for l in b.get("labels", [])}
        self.members = {m["id"]: m for m in b.get("members", [])}
        self.cl_by_card: dict[str, list] = {}
        for cl in b.get("checklists", []):
            self.cl_by_card.setdefault(cl["idCard"], []).append(cl)
        for cls in self.cl_by_card.values():
            cls.sort(key=lambda c: c.get("pos") or 0)
            for cl in cls:
                cl.setdefault("checkItems", []).sort(key=lambda i: i.get("pos") or 0)
        self.comments_by_card: dict[str, list] = {}
        for a in self.comments:
            cid = (a.get("data", {}).get("card") or {}).get("id")
            self.comments_by_card.setdefault(cid, []).append(a)

    @property
    def id(self):
        return self.b["id"]

    def open_lists(self):
        return sorted((l for l in self.b["lists"] if not l.get("closed")), key=lambda l: l.get("pos") or 0)

    def cards_in(self, list_id):
        return sorted((c for c in self.b["cards"] if c["idList"] == list_id and not c.get("closed")),
                      key=lambda c: c.get("pos") or 0)

    def checklists(self, card_id):
        return self.cl_by_card.get(card_id, [])

    def card_comments(self, card_id):
        return self.comments_by_card.get(card_id, [])

    def haystack(self, c) -> str:
        parts = [c["name"], c.get("desc", "")]
        parts += [self.labels[l]["name"] or self.labels[l]["color"] or "" for l in c.get("idLabels", []) if l in self.labels]
        parts += [self.members[m]["fullName"] for m in c.get("idMembers", []) if m in self.members]
        for cl in self.checklists(c["id"]):
            parts.append(cl["name"])
            parts += [i["name"] for i in cl.get("checkItems", [])]
        parts += [a["data"].get("text", "") for a in self.card_comments(c["id"])]
        return fold("\n".join(parts))


def matches(hay: str, query: str) -> bool:
    return all(w in hay for w in fold(query).split())


def rank(items: list[dict], query: str, limit=80) -> list[dict]:
    """items: {"text", "fold" (optional), "boost"}. Every word must appear; word starts and
    earlier hits rank higher; with few hits, fuzzy matches fill in."""
    q = fold(query).split()
    if not q:
        return items[:limit]
    scored = []
    for it in items:
        f = it.get("fold") or fold(it["text"])
        score = it.get("boost", 0)
        for w in q:
            i = f.find(w)
            if i < 0:
                break
            score += 10 - min(i, 9) * 0.5 + (5 if i == 0 or not f[i - 1].isalnum() else 0)
        else:
            scored.append((score, it))
    if len(scored) < 15:
        from textual.fuzzy import FuzzySearch
        fz = FuzzySearch()
        have = {id(it) for _, it in scored}
        qq = fold(query)
        for it in items:
            if id(it) in have:
                continue
            s, _ = fz.match(qq, it.get("fold") or fold(it["text"]))
            if s > 0:
                scored.append((s / 10 + it.get("boost", 0) - 100, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:limit]]


def inline_md(s: str, style="") -> Text:
    """**bold**, *italic*, `code` and [x](url) of a one-line text, the rest as is."""
    import re
    t = Text(style=style)
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*|`(.+?)`|(?<![*\w])\*(?!\s)(.+?)\*(?!\w)|\[([^\]]+)\]\([^)]+\)", s):
        t.append(s[pos:m.start()])
        if m.group(1):
            t.append(m.group(1), "bold")
        elif m.group(2):
            t.append(m.group(2), "cyan")
        elif m.group(3):
            t.append(m.group(3), "italic")
        else:
            t.append(m.group(4), "underline")
        pos = m.end()
    t.append(s[pos:])
    return t


def highlight(text: str, query: str, base="") -> Text:
    t = Text(text, style=base)
    f = fold(text)
    for w in fold(query).split():
        start = 0
        while (i := f.find(w, start)) >= 0:
            t.stylize("bold underline", i, i + len(w))
            start = i + len(w)
    return t


# ------------------------------------------------------------ modals

class Picker(ModalScreen):
    """Type to filter, ↑↓ to choose, Enter to pick. With `on_toggle` it stays open and Enter
    toggles the highlighted entry (labels, members)."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close"),
                Binding("up", "move(-1)", show=False, priority=True),
                Binding("down", "move(1)", show=False, priority=True),
                Binding("pageup", "move(-10)", show=False, priority=True),
                Binding("pagedown", "move(10)", show=False, priority=True)]

    def __init__(self, title: str, source, on_toggle=None, placeholder="Type to search…", query=""):
        super().__init__()
        self.title_text, self.source, self.on_toggle = title, source, on_toggle
        self.placeholder, self.query_text = placeholder, query
        self.items: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide picker"):
            yield Static(self.title_text, classes="dialog-title")
            yield Input(self.query_text, placeholder=self.placeholder)
            yield OptionList()

    def on_mount(self):
        self.refill(self.query_text)

    def refill(self, query: str):
        ol = self.query_one(OptionList)
        self.items = self.source(query)
        opts = []
        for i, it in enumerate(self.items):
            t = it.get("prefix", Text()) + highlight(it["text"], query)
            if it.get("detail"):
                t += Text("  " + it["detail"], style="dim")
            opts.append(Option(t, id=str(i)))
        ol.set_options(opts)
        if opts:
            ol.highlighted = 0

    @on(Input.Changed)
    def changed(self, ev: Input.Changed):
        self.refill(ev.value)

    def action_move(self, d: int):
        ol = self.query_one(OptionList)
        if ol.option_count:
            ol.highlighted = max(0, min(ol.option_count - 1, (ol.highlighted or 0) + d))

    @on(Input.Submitted)
    def submitted(self):
        ol = self.query_one(OptionList)
        if ol.highlighted is not None and self.items:
            self.pick(self.items[ol.highlighted])

    @on(OptionList.OptionSelected)
    def selected(self, ev: OptionList.OptionSelected):
        self.pick(self.items[int(ev.option.id)])

    def pick(self, it: dict):
        if self.on_toggle:
            self.on_toggle(it)
            keep = self.query_one(OptionList).highlighted
            self.refill(self.query_one(Input).value)
            self.query_one(OptionList).highlighted = keep
        else:
            self.dismiss(it)


class Prompt(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, value="", note=""):
        super().__init__()
        self.title_text, self.value, self.note = title, value, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.title_text, classes="dialog-title")
            yield Input(self.value)
            if self.note:
                yield Static(self.note, classes="note")

    @on(Input.Submitted)
    def submitted(self, ev: Input.Submitted):
        self.dismiss(ev.value)


class Confirm(ModalScreen):
    BINDINGS = [Binding("escape,n", "dismiss(False)", "No"), Binding("enter,y", "dismiss(True)", "Yes")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question)
            yield Static("Enter yes  ·  Esc no", classes="note")


class Editor(ModalScreen):
    """Multi-line text (description, comment). Ctrl+S saves, Esc asks before throwing changes away."""

    def __init__(self, title: str, text: str, save_key: str):
        super().__init__()
        self.title_text, self.text = title, text
        self._bindings.bind(save_key, "save", "Save", priority=True)
        self._bindings.bind("escape", "cancel", "Cancel", priority=True)
        self.save_key = save_key

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog editor"):
            yield Static(f"{self.title_text}   [dim]{pretty(self.save_key)} save · Esc cancel · Markdown[/]",
                         classes="dialog-title")
            yield TextArea(self.text, soft_wrap=True, show_line_numbers=False, tab_behavior="indent")
        yield Footer()

    def action_save(self):
        self.dismiss(self.query_one(TextArea).text)

    def action_cancel(self):
        if self.query_one(TextArea).text == self.text:
            self.dismiss(None)
            return
        self.app.push_screen(Confirm("Throw away your changes?"),
                             lambda yes: self.dismiss(None) if yes else None)


class Help(ModalScreen):
    BINDINGS = [Binding("escape,f1,enter", "dismiss", "Close")]

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold", no_wrap=True)
        t.add_column()
        for k, d in self.rows:
            t.add_row(k, d)
        with Vertical(classes="dialog wide"):
            yield Static("Keys", classes="dialog-title")
            yield Static(t)


# ------------------------------------------------------------ board

class CardList(OptionList):
    BINDINGS = [Binding("enter", "select", show=False)]

    def __init__(self, list_id: str, **kw):
        super().__init__(**kw)
        self.list_id = list_id


class Column(Vertical):
    def __init__(self, lst: dict):
        super().__init__(classes="col")
        self.list_id = lst["id"]

    def compose(self) -> ComposeResult:
        yield Static("", classes="coltitle")
        yield CardList(self.list_id)


class BoardScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Static("", id="top")
        inp = Input(placeholder="Filter cards: words in name, description, labels, members, checklists…",
                    id="filter")
        inp.display = False
        yield inp
        yield HorizontalScroll(id="cols")
        yield Footer()

    def on_mount(self):
        k = self.app.keys
        for key, action, desc, show in [
            ("left", "col(-1)", "", False), ("right", "col(1)", "", False),
            ("shift+left", "shift_card(-1, 0)", "", False), ("shift+right", "shift_card(1, 0)", "", False),
            ("shift+up", "shift_card(0, -1)", "", False), ("shift+down", "shift_card(0, 1)", "", False),
            ("escape", "clear_filter", "", False),
            (k["new"], "new_card", "New card", True), (k["rename"], "rename", "Rename", False),
            (k["move"], "move", "Move", True), (k["labels"], "labels", "Labels", False),
            (k["members"], "members", "Members", False), (k["due"], "due", "Due", False),
            (k["archive"], "archive", "Archive", True), (k["filter"], "filter", "Filter", True),
            (k["history"], "history", "History", True), (k["deleted"], "deleted", "Deleted", True),
            (k["browser"], "browser", "Browser", False),
        ]:
            self._bindings.bind(key, action, desc, show=show)
        self.refresh_bindings()
        self.filter_text = ""
        self.cur_list: str | None = None
        self.cur_card: str | None = None
        self.col_ids: list[str] = []

    # -- painting

    def card_prompt(self, c: dict, m: Model) -> Text:
        t = inline_md(c["name"])
        badges = Text()
        for lid in c.get("idLabels", []):
            lb = m.labels.get(lid)
            if lb:
                badges.append("● " if not lb["name"] else f"● {lb['name'][:12]} ",
                              style=label_color(lb.get("color")))
        cls = m.checklists(c["id"])
        if cls:
            items = [i for cl in cls for i in cl.get("checkItems", [])]
            done = sum(i["state"] == "complete" for i in items)
            if items:
                badges.append(f"☑ {done}/{len(items)} ", style="green" if done == len(items) else "dim")
        due = parse_time(c.get("due"))
        if due:
            now = datetime.now().astimezone()
            style = ("green" if c.get("dueComplete") else "bold red" if due < now
                     else "yellow" if due < now + timedelta(days=1) else "dim")
            badges.append(f"󰃭 {short_date(due)} ", style=style)
        n = len(m.card_comments(c["id"]))
        if n:
            badges.append(f"󰆉 {n} ", style="dim")
        if c.get("desc"):
            badges.append("≡ ", style="dim")
        mem = [m.members[x].get("initials", "?") for x in c.get("idMembers", []) if x in m.members]
        if mem:
            badges.append(" ".join(mem), style="cyan")
        if badges:
            t.append("\n")
            t.append_text(badges)
        return t

    def paint(self):
        app: TrelloApp = self.app
        m = app.model
        status = app.status_text()
        filt = f"   [b]filter:[/] {self.filter_text}" if self.filter_text else ""
        self.query_one("#top", Static).update(f" [b]{m.b.get('name', '')}[/]{filt}   [dim]{status}[/]")
        lists = m.open_lists()
        cols = self.query_one("#cols", HorizontalScroll)
        ids = [l["id"] for l in lists]
        if ids != self.col_ids:
            self.col_ids = ids
            self.run_worker(self.rebuild(cols, lists), group="cols", exclusive=True)
        else:
            self.fill()

    async def rebuild(self, cols, lists):
        await cols.remove_children()
        await cols.mount_all([Column(l) for l in lists])
        self.fill()

    def fill(self):
        m = self.app.model
        f = self.filter_text
        for col in self.query(Column):
            lst = m.lists.get(col.list_id)
            if not lst or not col.children:
                continue
            cards = m.cards_in(col.list_id)
            if f:
                cards = [c for c in cards if matches(m.haystack(c), f)]
            col.query_one(".coltitle", Static).update(
                Text.assemble((lst["name"], "bold"), (f"  {len(cards)}", "dim")))
            ol = col.query_one(CardList)
            keep = ol.highlighted_option.id if ol.highlighted_option else None
            keep = self.app.store.real.get(keep, keep)
            opts = []
            for c in cards:
                if opts:
                    opts.append(None)
                opts.append(Option(self.card_prompt(c, m), id=c["id"]))
            ol.set_options(opts)
            want = self.cur_card if col.list_id == self.cur_list else keep
            idx = self.index_of(ol, want)
            if idx is not None:
                ol.highlighted = idx
            elif ol.option_count:
                ol.highlighted = 0
        self.restore_focus()

    @staticmethod
    def index_of(ol: OptionList, oid: str | None):
        if not oid:
            return None
        try:
            return ol.get_option_index(oid)
        except Exception:  # noqa: BLE001 - OptionDoesNotExist
            return None

    def restore_focus(self):
        inp = self.query_one("#filter", Input)
        if inp.display and self.focused is inp:
            return
        cols = [c for c in self.query(Column) if c.children]
        if not cols:
            return
        col = next((c for c in cols if c.list_id == self.cur_list), cols[0])
        ol = col.query_one(CardList)
        if self.focused is not ol:
            ol.focus(scroll_visible=False)
        col.parent.scroll_to_widget(col, animate=False)

    # -- where we are

    def cur(self) -> tuple[CardList | None, dict | None]:
        f = self.focused
        if not isinstance(f, CardList):
            return None, None
        opt = f.highlighted_option
        return f, self.app.model.cards.get(opt.id) if opt else None

    @on(OptionList.OptionHighlighted)
    def highlighted(self, ev: OptionList.OptionHighlighted):
        if isinstance(ev.option_list, CardList) and ev.option_list.has_focus:
            self.cur_list, self.cur_card = ev.option_list.list_id, ev.option.id

    def on_descendant_focus(self, ev):
        if isinstance(ev.widget, CardList):
            self.cur_list = ev.widget.list_id
            opt = ev.widget.highlighted_option
            self.cur_card = opt.id if opt else None
            col = ev.widget.parent
            col.parent.scroll_to_widget(col, animate=False)

    @on(OptionList.OptionSelected)
    def open_card(self, ev: OptionList.OptionSelected):
        if isinstance(ev.option_list, CardList):
            self.app.open_card(ev.option.id)

    def action_col(self, d: int):
        cols = list(self.query(Column))
        lists = [c.list_id for c in cols]
        if not cols:
            return
        i = lists.index(self.cur_list) if self.cur_list in lists else 0
        j = max(0, min(len(cols) - 1, i + d))
        src = cols[i].query_one(CardList)
        dst = cols[j].query_one(CardList)
        row = src.highlighted or 0
        dst.focus()
        if dst.option_count:
            dst.highlighted = min(row, dst.option_count - 1)

    # -- actions

    def action_shift_card(self, dl: int, dr: int):
        ol, c = self.cur()
        if not c:
            return
        app: TrelloApp = self.app
        m = app.model
        lists = [l["id"] for l in m.open_lists()]
        if dl:
            i = lists.index(c["idList"]) + dl
            if not 0 <= i < len(lists):
                return
            target = lists[i]
            row = ol.highlighted or 0
            others = m.cards_in(target)
            row = min(row, len(others))
        else:
            target = c["idList"]
            others = [x for x in m.cards_in(target) if x["id"] != c["id"]]
            row = [x["id"] for x in m.cards_in(target)].index(c["id"]) + dr
            if not 0 <= row <= len(others):
                return
        pos = between(others[row - 1]["pos"] if row > 0 else None,
                      others[row]["pos"] if row < len(others) else None)
        self.cur_list, self.cur_card = target, c["id"]
        app.set_card(c, idList=target, pos=pos)

    def action_new_card(self):
        app: TrelloApp = self.app
        lst = self.cur_list or (app.model.open_lists() or [{}])[0].get("id")
        if not lst:
            return app.notify("This board has no list yet: Ctrl+P > New list", severity="warning")
        ol, c = self.cur()

        def add(name):
            if not name or not name.strip():
                return
            cards = app.model.cards_in(lst)
            ids = [x["id"] for x in cards]
            i = ids.index(c["id"]) + 1 if c and c["id"] in ids else len(cards)
            pos = between(cards[i - 1]["pos"] if i > 0 else None, cards[i]["pos"] if i < len(cards) else None)
            new = app.new_card(lst, name.strip(), pos)
            self.cur_list, self.cur_card = lst, new["id"]
            self.paint()
            self.action_new_card()          # keep adding; Esc stops
        app.push_screen(Prompt(f"New card in {app.model.lists[lst]['name']}",
                               note="Enter adds it and asks for the next one · Esc stops"), add)

    def action_rename(self):
        _, c = self.cur()
        if c:
            self.app.rename_card(c)

    def action_move(self):
        _, c = self.cur()
        if c:
            self.app.move_dialog(c)

    def action_labels(self):
        _, c = self.cur()
        if c:
            self.app.labels_dialog(c)

    def action_members(self):
        _, c = self.cur()
        if c:
            self.app.members_dialog(c)

    def action_due(self):
        _, c = self.cur()
        if c:
            self.app.due_dialog(c)

    def action_archive(self):
        ol, c = self.cur()
        if c:
            row = ol.highlighted or 0
            self.app.set_card(c, closed=True, undo_label=f"archived “{c['name']}”")
            nxt = min(row, ol.option_count - 1)
            if ol.option_count and nxt >= 0:
                ol.highlighted = nxt
                self.cur_card = ol.highlighted_option.id if ol.highlighted_option else None

    def action_filter(self):
        inp = self.query_one("#filter", Input)
        inp.display = True
        inp.focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self, ev: Input.Changed):
        self.filter_text = ev.value
        self.paint()

    @on(Input.Submitted, "#filter")
    def filter_done(self):
        inp = self.query_one("#filter", Input)
        inp.display = bool(self.filter_text)
        self.restore_focus_any()

    def restore_focus_any(self):
        for col in self.query(Column):
            ol = col.query_one(CardList)
            if ol.option_count and (col.list_id == self.cur_list or not self.cur_list):
                ol.focus()
                return
        for col in self.query(Column):
            if col.query_one(CardList).option_count:
                col.query_one(CardList).focus()
                return

    def action_clear_filter(self):
        inp = self.query_one("#filter", Input)
        if inp.display or self.filter_text:
            inp.value = ""
            self.filter_text = ""
            inp.display = False
            self.paint()
            self.restore_focus_any()

    def action_history(self):
        self.app.show_history(None)

    def action_deleted(self):
        self.app.show_deleted()

    def action_browser(self):
        _, c = self.cur()
        self.app.open_url((c or {}).get("shortUrl") or self.app.model.b.get("shortUrl"))


# ------------------------------------------------------------ card

class CardScreen(Screen):
    """One card as a page: title, details, description, checklists, comments.
    ↑↓ walk the rows; Enter edits the row; Space ticks an item."""

    def __init__(self, card_id: str):
        super().__init__()
        self.card_id = card_id
        self.obj = None

    def compose(self) -> ComposeResult:
        with Vertical(id="page"):
            yield OptionList(id="rows")
        yield Footer()

    def on_mount(self):
        k = self.app.keys
        for key, action, desc, show in [
            ("escape", "back", "Back", True), ("space", "toggle", "Tick", True),
            ("shift+up", "shift_row(-1)", "", False), ("shift+down", "shift_row(1)", "", False),
            (k["new"], "new_item", "New item", True), (k["new_checklist"], "new_checklist", "New checklist", False),
            (k["rename"], "edit", "Edit", False), (k["comment"], "comment", "Comment", True),
            (k["move"], "move", "Move", False), (k["labels"], "labels", "Labels", True),
            (k["members"], "members", "Members", False), (k["due"], "due", "Due", True),
            (k["archive"], "delete", "Delete", True), (k["history"], "history", "History", True),
            (k["browser"], "browser", "Browser", False),
        ]:
            self._bindings.bind(key, action, desc, show=show, priority=key in ("space", "escape"))
        self.refresh_bindings()
        self.paint()
        self.query_one("#rows").focus()

    @property
    def card(self) -> dict | None:
        m = self.app.model
        c = m.cards.get(self.card_id)
        if c is None and self.obj is not None and any(x is self.obj for x in m.b["cards"]):
            c, self.card_id = self.obj, self.obj["id"]     # it was just created and got its real id
        self.obj = c
        return c

    def paint(self):
        app: TrelloApp = self.app
        m = app.model
        c = self.card
        rows = self.query_one("#rows", OptionList)
        if not c:
            rows.set_options([Option(Text("This card is gone (deleted elsewhere?). Esc goes back.", style="dim"), id="gone")])
            return
        keep = rows.highlighted_option.id if rows.highlighted_option else None
        for tmp, real in app.store.real.items():
            if keep and tmp in keep:
                keep = keep.replace(tmp, real)
        opts = [Option(Text(c["name"], style="bold"), id="title")]
        meta = Text()
        lst = m.lists.get(c["idList"])
        meta.append(f"in {lst['name'] if lst else '?'}", style="dim")
        if c.get("closed"):
            meta.append("  ARCHIVED", style="bold yellow")
        for lid in c.get("idLabels", []):
            lb = m.labels.get(lid)
            if lb:
                meta.append(f"  ● {lb['name'] or lb['color']}", style=label_color(lb.get("color")))
        due = parse_time(c.get("due"))
        if due:
            meta.append(f"  󰃭 {short_date(due, True)}" + (" ✔" if c.get("dueComplete") else ""),
                        style="green" if c.get("dueComplete") else "red" if due < datetime.now().astimezone() else "")
        names = [m.members[x]["fullName"] for x in c.get("idMembers", []) if x in m.members]
        if names:
            meta.append("  " + ", ".join(names), style="cyan")
        opts += [Option(meta, id="meta"), None]
        desc = c.get("desc") or ""
        opts.append(Option(Markdown(desc) if desc else Text("Add a description… (Enter)", style="dim italic"), id="desc"))
        for cl in m.checklists(c["id"]):
            items = cl.get("checkItems", [])
            done = sum(i["state"] == "complete" for i in items)
            head = Text.assemble(("\n" if True else "", ""), ("☰ " + cl["name"], "bold"),
                                 (f"  {done}/{len(items)}", "green" if items and done == len(items) else "dim"))
            if items:
                w = 20
                fill = round(w * done / len(items))
                head.append("  " + "━" * fill, style="green")
                head.append("━" * (w - fill), style="bright_black")
            opts.append(Option(head, id=f"cl:{cl['id']}"))
            for it in items:
                ok = it["state"] == "complete"
                opts.append(Option(Text.assemble(("  ✔ " if ok else "  ☐ ", "green" if ok else ""),
                                                 inline_md(it["name"], "dim strike" if ok else "")),
                                   id=f"it:{cl['id']}:{it['id']}"))
        comments = m.card_comments(c["id"])
        if comments:
            opts.append(Option(Text(f"\nComments  {len(comments)}", style="bold"), id="comments", disabled=True))
        for a in comments:
            who = (a.get("memberCreator") or {}).get("fullName", "?")
            when = short_date(parse_time(a.get("date")), True)
            body = Table.grid()
            body.add_row(Text.assemble((who, "cyan"), ("  " + when, "dim")))
            body.add_row(Markdown(a["data"].get("text", "")))
            opts.append(Option(body, id=f"cm:{a['id']}"))
        rows.set_options(opts)
        try:
            rows.highlighted = rows.get_option_index(keep) if keep else 0
        except Exception:  # noqa: BLE001 - the row is gone
            rows.highlighted = 0

    def row(self) -> str:
        opt = self.query_one("#rows", OptionList).highlighted_option
        return opt.id if opt else ""

    def where(self):
        """(checklist, item) under the cursor, or the last checklist."""
        m = self.app.model
        r = self.row()
        cls = m.checklists(self.card_id)
        if r.startswith(("cl:", "it:")):
            parts = r.split(":")
            cl = next((x for x in cls if x["id"] == parts[1]), None)
            it = next((i for i in (cl or {}).get("checkItems", []) if len(parts) > 2 and i["id"] == parts[2]), None)
            return cl, it
        return (cls[-1] if cls else None), None

    def comment_at(self):
        r = self.row()
        if r.startswith("cm:"):
            return next((a for a in self.app.model.card_comments(self.card_id) if a["id"] == r[3:]), None)
        return None

    @on(OptionList.OptionSelected)
    def selected(self):
        self.action_edit()

    def action_back(self):
        self.app.pop_screen()

    def action_toggle(self):
        c = self.card
        if not c:
            return
        cl, it = self.where()
        r = self.row()
        if it:
            self.app.set_item(c, cl, it, state="incomplete" if it["state"] == "complete" else "complete")
        elif r == "meta" and c.get("due"):
            self.app.set_card(c, dueComplete=not c.get("dueComplete"))

    def action_edit(self):
        app: TrelloApp = self.app
        c = self.card
        if not c:
            return
        r = self.row()
        cl, it = self.where()
        if r == "title":
            app.rename_card(c)
        elif r == "meta":
            app.due_dialog(c)
        elif r == "desc":
            app.push_screen(Editor(f"Description · {c['name']}", c.get("desc") or "", app.keys["save"]),
                            lambda t: t is not None and t != c.get("desc") and app.set_card(c, desc=t))
        elif it:
            app.push_screen(Prompt("Item", it["name"]),
                            lambda t: t and t.strip() and t != it["name"] and app.set_item(c, cl, it, name=t.strip()))
        elif cl:
            app.push_screen(Prompt("Checklist", cl["name"]),
                            lambda t: t and t.strip() and app.rename_checklist(c, cl, t.strip()))
        elif (a := self.comment_at()):
            if a.get("idMemberCreator") != app.me_id:
                return app.notify("You can only edit your own comments", severity="warning")
            app.push_screen(Editor("Edit comment", a["data"]["text"], app.keys["save"]),
                            lambda t: t and t != a["data"]["text"] and app.edit_comment(c, a, t))

    def action_new_item(self):
        app: TrelloApp = self.app
        c = self.card
        if not c:
            return
        cl, it = self.where()
        if not cl:
            cl = app.new_checklist(c, "Checklist")

        def add(name):
            if not name or not name.strip():
                return
            items = cl.get("checkItems", [])
            ids = [i["id"] for i in items]
            i = ids.index(it["id"]) + 1 if it and it["id"] in ids else len(items)
            pos = between(items[i - 1]["pos"] if i > 0 else None, items[i]["pos"] if i < len(items) else None)
            new = app.new_item(c, cl, name.strip(), pos)
            self.paint()
            rows = self.query_one("#rows", OptionList)
            rows.highlighted = rows.get_option_index(f"it:{cl['id']}:{new['id']}")
            self.action_new_item()
        app.push_screen(Prompt(f"New item in {cl['name']}", note="Enter adds it and asks for the next one · Esc stops"), add)

    def action_new_checklist(self):
        c = self.card
        if c:
            def add(name):
                if name and name.strip():
                    cl = self.app.new_checklist(c, name.strip())
                    self.paint()
                    rows = self.query_one("#rows", OptionList)
                    rows.highlighted = rows.get_option_index(f"cl:{cl['id']}")
            self.app.push_screen(Prompt("New checklist", "Checklist"), add)

    def action_shift_row(self, d: int):
        c = self.card
        cl, it = self.where()
        if not (c and it):
            return
        items = cl["checkItems"]
        i = items.index(it)
        j = i + d
        if not 0 <= j < len(items):
            return
        others = [x for x in items if x is not it]
        pos = between(others[j - 1]["pos"] if j > 0 else None, others[j]["pos"] if j < len(others) else None)
        self.app.set_item(c, cl, it, pos=pos)

    def action_delete(self):
        app: TrelloApp = self.app
        c = self.card
        if not c:
            return
        r = self.row()
        cl, it = self.where()
        rows = self.query_one("#rows", OptionList)
        at = rows.highlighted or 0
        if it:
            app.delete_item(c, cl, it)
        elif r.startswith("cl:"):
            app.delete_checklist(c, cl)
        elif (a := self.comment_at()):
            if a.get("idMemberCreator") != app.me_id:
                return app.notify("You can only delete your own comments", severity="warning")
            app.delete_comment(c, a)
        elif r == "title":
            app.set_card(c, closed=not c.get("closed"),
                         undo_label=f"{'un' if c.get('closed') else ''}archived “{c['name']}”")
            return
        else:
            return
        rows.highlighted = min(at, rows.option_count - 1)

    def action_comment(self):
        c = self.card
        if c:
            self.app.push_screen(Editor(f"Comment · {c['name']}", "", self.app.keys["save"]),
                                 lambda t: t and t.strip() and self.app.add_comment(c, t.strip()))

    def action_move(self):
        if self.card:
            self.app.move_dialog(self.card)

    def action_labels(self):
        if self.card:
            self.app.labels_dialog(self.card)

    def action_members(self):
        if self.card:
            self.app.members_dialog(self.card)

    def action_due(self):
        if self.card:
            self.app.due_dialog(self.card)

    def action_history(self):
        self.app.show_history(self.card_id)

    def action_browser(self):
        if self.card:
            self.app.open_url(self.card.get("shortUrl"))


# ------------------------------------------------------------ history

def _names(m: Model, h, kind, oid, fallback="?"):
    obj = {"card": m.cards, "list": m.lists, "label": m.labels}.get(kind, {}).get(oid)
    if obj:
        return obj.get("name") or fallback
    d = h.last_data(kind, oid)
    return (d or {}).get("name") or fallback


def describe(v: Version, m: Model, h, show_card=True) -> tuple[str, Text]:
    """(icon, text) of one history event."""
    d, p = v.data, v.prev
    q = lambda s: f"“{(s or '').strip()[:70]}”"    # noqa: E731
    card = _names(m, h, "card", v.card) if v.card and show_card else ""
    if v.source == "restore" and v.first and v.kind in ("card", "checklist"):
        n = getattr(v, "folded", 0)
        return "↺", Text.assemble(f"restored {v.kind} ", (q(d["name"]), "bold"), f" with {n} items" if n else "",
                                  Text(f"  · {card}", style="dim") if card and v.kind != "card" else "")
    on_card = Text(f"  · {card}", style="dim") if card and v.kind != "card" else Text()
    if v.kind == "item":
        x = d or p or {}
        cl = _names(m, h, "checklist", x.get("idChecklist"))
        if d is None:
            return "✗", Text.assemble("removed item ", (q(x["name"]), "bold"), (f" from {cl}", ""), on_card)
        if v.first:
            return "+", Text.assemble("added item ", (q(d["name"]), "bold"), f" to {cl}", on_card)
        if p["state"] != d["state"]:
            return ("☑", Text.assemble("checked ", q(d["name"]), on_card)) if d["state"] == "complete" else \
                   ("☐", Text.assemble("unchecked ", q(d["name"]), on_card))
        if p["name"] != d["name"]:
            return "✎", Text.assemble("item ", (q(p["name"]), "strike"), " → ", (q(d["name"]), "bold"), on_card)
        return "↕", Text.assemble(("moved item " + q(d["name"]), "dim"), on_card)
    if v.kind == "checklist":
        x = d or p or {}
        if d is None:
            n = getattr(v, "folded", 0)
            return "✗", Text.assemble("deleted checklist ", (q(x["name"]), "bold"),
                                      f" with {n} items" if n else "", on_card)
        if v.first:
            return "+", Text.assemble("added checklist ", q(d["name"]), on_card)
        if p["name"] != d["name"]:
            return "✎", Text.assemble("checklist ", (q(p["name"]), "strike"), " → ", q(d["name"]), on_card)
        return "↕", Text.assemble(("moved checklist " + q(d["name"]), "dim"), on_card)
    if v.kind == "comment":
        x = d or p or {}
        if d is None:
            return "✗", Text.assemble(f"deleted comment by {x.get('author')}: ", (q(x["text"]), "italic"), on_card)
        if v.first:
            return "󰆉", Text.assemble(f"{d.get('author')}: ", (q(d["text"]), "italic"), on_card)
        return "✎", Text.assemble("edited comment: ", (q(d["text"]), "italic"), on_card)
    if v.kind == "card":
        x = d or p or {}
        if d is None:
            n = getattr(v, "folded", 0)
            return "✗", Text.assemble("deleted card ", (q(x["name"]), "bold"),
                                      f" (with {n} checklists/items/comments)" if n else "")
        if v.first:
            return "+", Text.assemble("created card ", (q(d["name"]), "bold"), f" in {_names(m, h, 'list', d['idList'])}")
        changes = []
        if p["closed"] != d["closed"]:
            changes.append("archived" if d["closed"] else "unarchived")
        if p["idList"] != d["idList"]:
            changes.append(f"→ {_names(m, h, 'list', d['idList'])}")
        if p["name"] != d["name"]:
            changes.append(f"renamed from {q(p['name'])}")
        if p["desc"] != d["desc"]:
            changes.append("edited description")
        if p["idLabels"] != d["idLabels"]:
            added = [_names(m, h, "label", x, "label") for x in set(d["idLabels"] or []) - set(p["idLabels"] or [])]
            gone = [_names(m, h, "label", x, "label") for x in set(p["idLabels"] or []) - set(d["idLabels"] or [])]
            changes.append("labels " + " ".join([f"+{a}" for a in added] + [f"−{g}" for g in gone]))
        if p["due"] != d["due"]:
            changes.append(f"due {short_date(parse_time(d['due']), True) or 'removed'}")
        if p["dueComplete"] != d["dueComplete"]:
            changes.append("due done" if d["dueComplete"] else "due not done")
        if p["idMembers"] != d["idMembers"]:
            changes.append("members changed")
        if not changes:
            return "↕", Text(f"moved card {q(d['name'])}", style="dim")
        return "✎", Text.assemble((q(d["name"]), "bold"), " " + ", ".join(changes))
    if v.kind == "list":
        x = d or p or {}
        if v.first:
            return "+", Text(f"added list {q(x['name'])}")
        if d is None:
            return "✗", Text(f"list {q(x['name'])} left the board")
        if p["closed"] != d["closed"]:
            return "✎", Text(f"{'archived' if d['closed'] else 'unarchived'} list {q(d['name'])}")
        if p["name"] != d["name"]:
            return "✎", Text(f"list {q(p['name'])} → {q(d['name'])}")
        return "↕", Text(f"moved list {q(d['name'])}", style="dim")
    x = d or p or {}
    return "·", Text(f"{v.kind} {q(x.get('name', ''))} {'deleted' if d is None else 'changed'}", style="dim")


def fold_events(vs: list[Version], show_import=False) -> list[Version]:
    """Children deleted together with their card/checklist are counted into it, not listed."""
    gone_cards = {(v.oid, v.ts) for v in vs if v.kind == "card" and v.data is None}
    gone_cls = {(v.oid, v.ts) for v in vs if v.kind == "checklist" and v.data is None}
    restores = [v for v in vs if v.source == "restore" and v.first and v.kind in ("card", "checklist")]
    out, counts = [], {}
    for v in vs:
        if v.source == "import" and not show_import:
            continue
        if v.source == "restore" and v.first and v.kind in ("item", "checklist", "comment"):
            parent = next((r for r in restores if r is not v and r.card == v.card and abs(r.ts - v.ts) < 30
                           and (r.kind == "card" or v.kind == "item")), None)
            if parent:
                counts[(parent.oid, parent.ts)] = counts.get((parent.oid, parent.ts), 0) + 1
                continue
        if v.data is None and v.kind in ("checklist", "item", "comment") and (v.card, v.ts) in gone_cards:
            counts[(v.card, v.ts)] = counts.get((v.card, v.ts), 0) + 1
            continue
        if v.data is None and v.kind == "item" and ((v.prev or {}).get("idChecklist"), v.ts) in gone_cls:
            key = (v.prev["idChecklist"], v.ts)
            counts[key] = counts.get(key, 0) + 1
            continue
        out.append(v)
    for v in out:
        v.folded = counts.get((v.oid, v.ts), 0)
    return out


class HistoryScreen(Screen):
    """Newest first. Enter on an event shows it and offers to restore / revert."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back")]

    def __init__(self, title: str, versions: list[Version], deleted_only=False, show_card=True):
        super().__init__()
        self.title_text, self.versions, self.deleted_only = title, versions, deleted_only
        self.show_card = show_card

    def compose(self) -> ComposeResult:
        with Vertical(id="page"):
            yield Static(self.title_text, id="htitle")
            yield OptionList(id="events")
        yield Footer()

    def on_mount(self):
        self.paint()
        self.query_one("#events").focus()

    def paint(self):
        app: TrelloApp = self.app
        opts = []
        for i, v in enumerate(self.versions):
            icon, txt = describe(v, app.model, app.store.history, self.show_card)
            when = datetime.fromtimestamp(v.ts).strftime("%-d %b %H:%M")
            color = "red" if v.data is None else "cyan" if v.source == "restore" else "green" if v.first else ""
            t = Text.assemble((f"{when:>13}  ", "dim"), (icon + " ", color), txt)
            opts.append(Option(t, id=str(i)))
        if not opts:
            opts.append(Option(Text("Nothing yet. History starts with the first sync and grows with every change.",
                                    style="dim"), id="none", disabled=True))
        ev = self.query_one("#events", OptionList)
        keep = ev.highlighted
        ev.set_options(opts)
        ev.highlighted = min(keep or 0, len(opts) - 1)

    @on(OptionList.OptionSelected)
    def chosen(self, ev: OptionList.OptionSelected):
        v = self.versions[int(ev.option.id)]
        self.app.push_screen(EventDetail(v), lambda yes: yes and self.app.restore(v, self))


class EventDetail(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss(False)", "Close"), Binding("enter", "dismiss(True)", "Restore")]

    def __init__(self, v: Version):
        super().__init__()
        self.v = v

    def compose(self) -> ComposeResult:
        app: TrelloApp = self.app
        v = self.v
        icon, txt = describe(v, app.model, app.store.history)
        when = datetime.fromtimestamp(v.ts).strftime("%-d %b %Y %H:%M")
        how = {"sync": "noticed while syncing", "timer": "noticed by the background snapshot",
               "app": "done in this app", "restore": "restored in this app"}.get(v.source, v.source)
        with Vertical(classes="dialog wide"):
            yield Static(Text.assemble(icon + " ", txt), classes="dialog-title")
            yield Static(f"[dim]{when} · {how}[/]")
            before, after = v.prev, v.data
            for label, data in (("Before", before), ("After", after)):
                if data is None:
                    continue
                body = data.get("desc") if v.kind == "card" and before and after and before.get("desc") != after.get("desc") \
                    else data.get("text") or data.get("name") or ""
                if v.kind == "item":
                    body = ("✔ " if data.get("state") == "complete" else "☐ ") + body
                yield Static(f"\n[b]{label}[/]")
                yield Static(Markdown(body) if v.kind in ("card", "comment") else Text(body), classes="snippet")
            if v.data is None:
                action = "Enter restores it" + (f" (with its {v.folded} items)" if getattr(v, "folded", 0) else "")
            elif v.prev is None:
                action = "Esc closes"
            else:
                action = "Enter puts back what was before"
            yield Static(f"\n{action} · Esc closes", classes="note")


# ------------------------------------------------------------ app

class TrelloApp(App):
    CSS = """
    Screen { background: ansi_default; }
    #top { height: 1; background: ansi_default; }
    #filter { display: none; height: 3; border: round $accent; background: ansi_default; }
    #cols { height: 1fr; background: ansi_default; border-top: solid $panel-lighten-2; }
    .col { width: COLW; height: 1fr; border-right: vkey $panel-lighten-2; padding: 0 0 0 1; }
    .coltitle { height: 1; padding: 0 1; margin-bottom: 1; }
    CardList { height: 1fr; border: none; background: ansi_default; padding: 0; scrollbar-size-vertical: 1; }
    CardList:focus { border: none; }
    CardList > .option-list--option { padding: 0 1; }
    CardList > .option-list--option-highlighted { background: ansi_default; text-style: none; }
    CardList:focus > .option-list--option-highlighted { background: ansi_blue; color: ansi_black; text-style: none; }
    CardList:focus { border-left: none; }
    CardList > .option-list--separator { color: ansi_default; }
    #page { width: 100%; max-width: 100; height: 1fr; padding: 1 2 0 2; }
    CardScreen, HistoryScreen { align-horizontal: center; }
    #rows, #events { height: 1fr; border: none; background: ansi_default; scrollbar-size-vertical: 1; }
    #rows > .option-list--option, #events > .option-list--option { padding: 0 1; }
    #rows:focus > .option-list--option-highlighted, #events:focus > .option-list--option-highlighted {
        background: ansi_blue; color: ansi_black; text-style: none; }
    #htitle { padding: 0 1 1 1; text-style: bold; }
    .dialog { width: 70; max-width: 95%; height: auto; max-height: 85%; border: round $accent;
              background: $surface; padding: 1 2; }
    .dialog.wide { width: 100; }
    .dialog.editor { width: 100; height: 85%; }
    .dialog.editor TextArea { height: 1fr; border: none; background: $surface; }
    .dialog OptionList { height: auto; max-height: 24; border: none; background: $surface; }
    .dialog OptionList > .option-list--option-highlighted { background: ansi_blue; color: ansi_black; text-style: none; }
    TextArea > .text-area--selection { background: ansi_blue; color: ansi_black; text-style: none; }
    Input > .input--selection { background: ansi_blue; color: ansi_black; }
    .dialog Input { background: $surface; border: tall $panel; }
    .dialog-title { text-style: bold; padding-bottom: 1; }
    .snippet { max-height: 14; overflow-y: auto; padding-left: 2; }
    .note { color: $text-muted; padding-top: 1; }
    ModalScreen { align: center middle; }
    """
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, cfg: Config, store: Store | None = None):
        type(self).CSS = TrelloApp.CSS.replace("COLW", str(cfg.column_width))
        super().__init__()
        self.cfg, self.keys = cfg, cfg.keys
        self.store = store or Store(cfg, self.call_from_thread)
        self.model = Model(None, None)
        self.undo_stack: list[tuple[str, callable]] = []
        self.syncing = False
        self.error = ""
        self.last_sync = 0.0
        self.last_full: dict[str, float] = {}
        self._sync_timer = None
        self.me_id = (self.store.history.get("me") or {}).get("id", "")

    def on_mount(self):
        self.theme = "ansi-dark"
        k = self.keys
        for key, action, desc, show in [
            (k["palette"], "palette", "Go to", True), (k["boards"], "boards", "Boards", True),
            (k["undo"], "undo", "Undo", True), (k["refresh"], "refresh", "Refresh", False),
            (k["help"], "help", "Help", True),
        ]:
            self._bindings.bind(key, action, desc, show=show, priority=True)
        self.push_screen(BoardScreen())
        boards = self.store.cached_boards()
        bid = self.store.history.get("last_board") or self.cfg.default_board
        if bid and not any(b["id"] == bid for b in boards):
            bid = next((b["id"] for b in boards if b["name"] == bid), bid)
        if bid:
            self.call_after_refresh(self.open_board, bid)
        elif boards:
            self.call_after_refresh(self.action_boards)
        self.store.submit("bg", self.store.me, lambda me: setattr(self, "me_id", me["id"]), self.fail(""))
        self.store.submit("bg", self.store.fetch_boards, self.boards_fetched, self.fail("Boards: "))
        self.set_interval(self.cfg.poll, self.poll)

    # -- helpers

    def fail(self, prefix=""):
        def report(e):
            self.error = str(e)
            self.syncing = False
            self.notify(f"{prefix}{e}", severity="error", timeout=8)
            self.repaint()
        return report

    def status_text(self) -> str:
        bits = []
        if self.store.inflight:
            bits.append(f"↑ {self.store.inflight}")
        if self.syncing:
            bits.append("↻")
        elif self.last_sync:
            bits.append(datetime.fromtimestamp(self.last_sync).strftime("synced %H:%M"))
        if self.error:
            bits.append("! offline")
        return "  ".join(bits)

    def repaint(self):
        for s in self.screen_stack:
            if hasattr(s, "paint") and s.is_mounted:
                s.paint()

    def write(self, fn, label=""):
        """Send a change to Trello on the action lane; the screen already shows it."""
        def done(_):
            self.error = ""
            self.swap_ids()
            self.repaint()
            self.schedule_sync(SYNC_AFTER_WRITE)
            self.repaint_top()

        def error(e):
            self.notify(f"{label or 'Change'} failed: {e}", severity="error", timeout=8)
            self.schedule_sync(0.5)
        self.store.submit("fg", fn, done, error)
        self.repaint_top()

    def repaint_top(self):
        for s in self.screen_stack:
            if isinstance(s, BoardScreen) and s.is_mounted:
                m = self.model
                filt = f"   [b]filter:[/] {s.filter_text}" if s.filter_text else ""
                s.query_one("#top", Static).update(f" [b]{m.b.get('name', '')}[/]{filt}   [dim]{self.status_text()}[/]")

    def R(self, o: dict) -> str:
        """The Trello id of something, also when it was created a moment ago (still a tmp id on screen)."""
        return self.store.real.get(o["id"], o["id"])

    def swap_ids(self):
        """Put the real ids of things just created in place of their tmp ids (UI thread only)."""
        real = self.store.real
        if not real:
            return
        m = self.model
        sw = lambda x: real.get(x, x)      # noqa: E731
        items = [i for cl in m.b["checklists"] for i in cl.get("checkItems", [])]
        for o in m.b["lists"] + m.b["cards"] + m.b["checklists"] + items + m.comments:
            o["id"] = sw(o["id"])
        for c in m.b["cards"]:
            c["idList"] = sw(c["idList"])
        for cl in m.b["checklists"]:
            cl["idCard"] = sw(cl["idCard"])
        for a in m.comments:
            a["data"]["card"]["id"] = sw(a["data"]["card"]["id"])
        for s in self.screen_stack:
            if isinstance(s, BoardScreen):
                s.cur_list, s.cur_card = sw(s.cur_list), sw(s.cur_card)
            elif isinstance(s, CardScreen):
                s.card_id = sw(s.card_id)
        m.reindex()

    def push_undo(self, label: str, fn):
        self.undo_stack.append((label, fn))
        del self.undo_stack[:-100]

    def open_url(self, url):
        if url:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)

    # -- boards and syncing

    def open_board(self, bid: str):
        board, comments = self.store.cached_board(bid)
        self.model = Model(board or {"id": bid, "name": "…", "lists": [], "cards": [], "checklists": [],
                                     "labels": [], "members": []}, comments)
        self.store.history.put("last_board", bid)
        self.undo_stack.clear()
        while len(self.screen_stack) > 2:
            self.pop_screen()
        bs = self.screen_stack[-1]
        if isinstance(bs, BoardScreen) and bs.is_mounted:
            bs.cur_list = bs.cur_card = None
            bs.filter_text = ""
            bs.query_one("#filter", Input).value = ""
            bs.query_one("#filter", Input).display = False
        self.repaint()
        self.sync(full=True)

    def boards_fetched(self, boards):
        self.repaint_top()
        if not self.model.id and not isinstance(self.screen, Picker):
            self.action_boards()
        # Fill the local copy of every other board in the background so Ctrl+P finds any card.
        for b in boards:
            if b["id"] != self.model.id and self.store.history.get(f"seen:{b['id']}") != b.get("dateLastActivity"):
                self.store.submit("bg", lambda bid=b["id"]: self.store.fetch_board(bid, check_writes=False))

    def schedule_sync(self, delay: float):
        if self._sync_timer:
            self._sync_timer.stop()
        self._sync_timer = self.set_timer(max(delay, 0.05), lambda: self.sync(full=True))

    def poll(self):
        if not self.syncing and not self.store.inflight:
            self.sync(full=False)

    def sync(self, full=True):
        bid = self.model.id
        if not bid or self.syncing:
            return
        self.syncing = True
        self.repaint_top()
        need_full = full or time.time() - self.last_full.get(bid, 0) > FULL_SYNC
        api, hist = self.store.api, self.store.history

        def job():
            if not need_full:
                act = api.req("GET", f"/boards/{bid}", fields="dateLastActivity")["dateLastActivity"]
                if act == hist.get(f"seen:{bid}"):
                    return "same"
            return self.store.fetch_board(bid)

        def done(res):
            self.syncing = False
            self.error = ""
            self.last_sync = time.time()
            if res is None:                     # a write landed meanwhile: look again shortly
                self.schedule_sync(1.5)
            elif res != "same" and bid == self.model.id:
                self.last_full[bid] = time.time()
                self.model = Model(*res)
                self.repaint()
                return
            self.repaint_top()
        self.store.submit("bg", job, done, self.fail("Sync: "))

    def action_refresh(self):
        self.sync(full=True)
        self.store.submit("bg", self.store.fetch_boards, self.boards_fetched, self.fail("Boards: "))

    # -- go to anything

    def board_items(self):
        cur = self.model.id
        return [{"text": b["name"], "detail": "board", "kind": "board", "id": b["id"],
                 "prefix": Text("󰃥 ", style="cyan"), "boost": 3 if b["id"] == cur else 0}
                for b in self.store.cached_boards()]

    def card_items(self):
        items = []
        names = {b["id"]: b["name"] for b in self.store.cached_boards()}
        for bid in names:
            board = self.model.b if bid == self.model.id else self.store.history.get(f"board:{bid}")
            if not board:
                continue
            lists = {l["id"]: l for l in board["lists"]}
            for c in board["cards"]:
                lst = lists.get(c["idList"])
                if c.get("closed") or not lst or lst.get("closed"):
                    continue
                items.append({"text": c["name"], "detail": f"{names[bid]} › {lst['name']}", "kind": "card",
                              "id": c["id"], "board": bid, "boost": 2 if bid == self.model.id else 0})
        return items

    def command_items(self):
        cmds = [
            ("New card", "new_card"), ("New list", "new_list"), ("Rename list", "rename_list"),
            ("Archive list", "archive_list"), ("Move list left", "move_list(-1)"), ("Move list right", "move_list(1)"),
            ("Filter cards", "filter"), ("Board history", "history"), ("Recently deleted (restore)", "deleted"),
            ("Show archived cards", "archived"), ("Switch board", "boards"), ("Refresh", "refresh"),
            ("Open in browser", "browser"), ("Delete card permanently", "delete_card"), ("Undo", "undo"),
            ("Keys", "help"),
        ]
        return [{"text": n, "kind": "cmd", "id": a, "prefix": Text("> ", style="dim")} for n, a in cmds]

    def action_palette(self):
        cache = {}

        def source(q):
            if q.startswith(">"):
                return rank(self.command_items(), q[1:])
            if not q.strip():
                recent = self.store.history.get("recent", [])
                cards = {c["id"]: c for c in self.card_items()}
                return [cards[r] for r in recent if r in cards][:15] + self.board_items()
            if "all" not in cache:
                cache["all"] = self.board_items() + self.card_items()
            return rank(cache["all"], q)

        def chosen(it):
            if not it:
                return
            if it["kind"] == "board":
                self.open_board(it["id"])
            elif it["kind"] == "card":
                if it["board"] != self.model.id:
                    self.open_board(it["board"])
                self.open_card(it["id"])
            else:
                self.run_command(it["id"])
        self.push_screen(Picker("Go to card or board  ·  “>” for commands", source,
                                placeholder="Card or board name… (> for commands)"), chosen)

    def run_command(self, action: str):
        name = action.split("(")[0]
        if name == "delete_card":
            card = self.current_card()
            if card:
                self.delete_card(card)
        elif hasattr(self.screen, f"action_{name}"):
            self.call_later(self.screen.run_action, action)
        else:
            self.call_later(self.run_action, action)

    def current_card(self):
        scr = self.screen
        if isinstance(scr, CardScreen):
            return scr.card
        if isinstance(scr, BoardScreen):
            return scr.cur()[1]
        return None

    def action_boards(self):
        self.push_screen(Picker("Boards", lambda q: rank(self.board_items(), q)),
                         lambda it: it and self.open_board(it["id"]))

    def action_help(self):
        k = {n: pretty(v) for n, v in self.keys.items()}
        self.push_screen(Help([
            ("← → ↑ ↓  Enter", "move between lists and cards · open a card"),
            ("Shift+← →  Shift+↑ ↓", "move the card to the next list · up/down (items too, in a card)"),
            (f"{k['palette']}", "go to any card or board, on every board; type > for commands"),
            (f"{k['boards']}", "switch board"),
            (f"{k['filter']}  ·  Esc", "filter cards on this board (name, description, checklists…) · clear"),
            (f"{k['new']}  ·  {k['new_checklist']}", "new card (in a card: new checklist item) · new checklist"),
            (f"{k['rename']} / Enter", "rename / edit what's under the cursor"),
            ("Space", "in a card: tick an item (on the details row: due done)"),
            (f"{k['move']}  ·  {k['labels']}  ·  {k['members']}  ·  {k['due']}", "move to list · labels · members · due date"),
            (f"{k['comment']}", "comment"),
            (f"{k['archive']}", "archive card · in a card: delete item / checklist / comment"),
            (f"{k['undo']}", "undo (again and again)"),
            (f"{k['history']}", "history of the card / the board: every change, with restore"),
            (f"{k['deleted']}", "recently deleted: cards, checklists, items, comments → restore"),
            (f"{k['browser']}", "open in the browser"),
            (f"{k['refresh']}  ·  {k['help']}  ·  Ctrl+Q", "refresh · this help · quit"),
        ]))

    # -- card operations (optimistic: screen first, Trello behind)

    def open_card(self, card_id: str):
        recent = [card_id] + [r for r in self.store.history.get("recent", []) if r != card_id]
        self.store.history.put("recent", recent[:30])
        while isinstance(self.screen, (CardScreen, HistoryScreen)):
            self.pop_screen()
        self.push_screen(CardScreen(card_id))

    def set_card(self, c: dict, undo=True, undo_label="", **fields):
        old = {k: c.get(k) for k in fields}
        c.update(fields)
        self.repaint()
        bid = self.model.id
        self.write(lambda: self.store.rec("card", self.store.api.update_card(self.R(c), **fields), bid, self.R(c)),
                   "Saving the card")
        if undo:
            self.push_undo(undo_label or f"changed “{c['name']}”", lambda: self.set_card(c, undo=False, **old))

    def rename_card(self, c):
        self.push_screen(Prompt("Card name", c["name"]),
                         lambda t: t and t.strip() and t.strip() != c["name"] and self.set_card(c, name=t.strip()))

    def new_card(self, list_id, name, pos) -> dict:
        c = {"id": tmp_id(), "name": name, "desc": "", "closed": False, "idList": list_id, "idBoard": self.model.id,
             "pos": pos, "idLabels": [], "idMembers": [], "due": None, "dueComplete": False}
        self.model.b["cards"].append(c)
        self.model.reindex()
        bid = self.model.id

        def job():
            new = self.store.api.create_card(self.store.real.get(list_id, list_id), name, pos=pos)
            self.store.real[c["id"]] = new["id"]
            c["shortUrl"] = new.get("shortUrl")
            self.store.rec("card", new, bid, new["id"])
        self.write(job, "Creating the card")
        self.push_undo(f"created “{name}”", lambda: self.set_card(c, undo=False, closed=True))
        return c

    def delete_card(self, c):
        def go(yes):
            if not yes:
                return
            m = self.model
            bid = m.id
            gone = [("card", self.R(c), bid, self.R(c))]
            for cl in m.checklists(c["id"]):
                gone.append(("checklist", self.R(cl), bid, self.R(c)))
                gone += [("item", i["id"], bid, self.R(c)) for i in cl.get("checkItems", [])]
            gone += [("comment", self.R(a), bid, self.R(c)) for a in m.card_comments(c["id"])]
            m.b["cards"].remove(c)
            m.b["checklists"] = [cl for cl in m.b["checklists"] if cl["idCard"] != c["id"]]
            m.reindex()
            if isinstance(self.screen, CardScreen):
                self.pop_screen()
            self.repaint()
            when = time.time()

            def job():
                self.store.api.delete_card(self.R(c))
                self.store.history.observe_deleted([(k, self.store.real.get(o, o), b, self.store.real.get(x, x)) for k, o, b, x in gone])
            self.write(job, "Deleting the card")
            self.push_undo(f"deleted “{c['name']}”", lambda: self.restore(
                Version(0, "card", self.R(c), bid, self.R(c), when, "app", None,
                        prev=self.store.history.last_data("card", self.R(c))), None))
        self.push_screen(Confirm(f"Delete “{c['name']}” for good? (History keeps a copy you can restore.)"), go)

    def move_dialog(self, c):
        m = self.model
        items = [{"text": l["name"], "detail": "this board", "kind": "list", "id": l["id"], "board": m.id,
                  "boost": 2} for l in m.open_lists() if l["id"] != c["idList"]]
        for b in self.store.cached_boards():
            if b["id"] == m.id:
                continue
            board = self.store.history.get(f"board:{b['id']}")
            for l in sorted((l for l in (board or {}).get("lists", []) if not l.get("closed")),
                            key=lambda l: l.get("pos") or 0):
                items.append({"text": l["name"], "detail": b["name"], "kind": "list", "id": l["id"], "board": b["id"]})

        def chosen(it):
            if not it:
                return
            if it["board"] == m.id:
                cards = m.cards_in(self.R(it))
                self.set_card(c, idList=self.R(it), pos=(cards[-1]["pos"] + 65536) if cards else 65536,
                              undo_label=f"moved “{c['name']}”")
                bs = self.screen_stack[1]
                if isinstance(bs, BoardScreen) and not isinstance(self.screen, CardScreen):
                    bs.cur_list, bs.cur_card = it["id"], c["id"]
                    bs.paint()
                return
            m.b["cards"].remove(c)
            m.reindex()
            if isinstance(self.screen, CardScreen):
                self.pop_screen()
            self.repaint()
            bid = m.id
            self.write(lambda: self.store.rec("card", self.store.api.update_card(
                self.R(c), idBoard=it["board"], idList=self.R(it), pos="bottom"), it["board"], self.R(c)), "Moving the card")
            self.store.submit("bg", lambda: self.store.history.observe("card", self.R(c), bid, self.R(c), None))
            self.notify(f"Moved to {it['detail']} › {it['text']}")
        self.push_screen(Picker(f"Move “{c['name']}” to…", lambda q: rank(items, q)), chosen)

    def labels_dialog(self, c):
        m = self.model

        def source(q):
            items = []
            for lb in sorted(m.labels.values(), key=lambda l: (l.get("color") or "", l.get("name") or "")):
                on = lb["id"] in c.get("idLabels", [])
                items.append({"text": lb["name"] or lb["color"] or "(no name)", "id": lb["id"],
                              "prefix": Text.assemble(("✔ " if on else "  ", "green"),
                                                      ("● ", label_color(lb.get("color"))))})
            return rank(items, q, 200)

        def toggle(it):
            ids = list(c.get("idLabels", []))
            ids.remove(self.R(it)) if self.R(it) in ids else ids.append(self.R(it))
            self.set_card(c, idLabels=ids, undo_label="labels")
        self.push_screen(Picker(f"Labels · {c['name']}   [dim]Enter toggles · Esc done[/]", source, on_toggle=toggle))

    def members_dialog(self, c):
        m = self.model

        def source(q):
            items = [{"text": mb["fullName"], "id": mb["id"], "detail": mb.get("username", ""),
                      "prefix": Text("✔ " if mb["id"] in c.get("idMembers", []) else "  ", style="green")}
                     for mb in m.members.values()]
            return rank(items, q, 200)

        def toggle(it):
            ids = list(c.get("idMembers", []))
            ids.remove(self.R(it)) if self.R(it) in ids else ids.append(self.R(it))
            self.set_card(c, idMembers=ids, undo_label="members")
        self.push_screen(Picker(f"Members · {c['name']}   [dim]Enter toggles · Esc done[/]", source, on_toggle=toggle))

    def due_dialog(self, c):
        cur = parse_time(c.get("due"))
        val = cur.strftime("%Y-%m-%d %H:%M") if cur else ""

        def done(s):
            if s is None:
                return
            try:
                due = parse_due(s)
            except ValueError as e:
                return self.notify(str(e), severity="error")
            self.set_card(c, due=due, undo_label="due date")
        self.push_screen(Prompt(f"Due · {c['name']}", val,
                                note="2026-10-05 14:00 · 5.10. · today · tomorrow · +3d · +2w · empty removes it"), done)

    # -- checklists

    def set_item(self, c, cl, it, undo=True, **fields):
        old = {k: it.get(k) for k in fields}
        it.update(fields)
        if "pos" in fields:
            cl["checkItems"].sort(key=lambda i: i.get("pos") or 0)
        self.repaint()
        bid = self.model.id

        def job():
            new = self.store.api.update_item(self.R(c), self.R(it), **fields)
            self.store.rec("item", {**new, "idChecklist": self.R(cl)}, bid, self.R(c))
        self.write(job, "Saving the item")
        if undo:
            self.push_undo(f"item “{it['name']}”", lambda: self.set_item(c, cl, it, undo=False, **old))

    def new_item(self, c, cl, name, pos) -> dict:
        it = {"id": tmp_id(), "name": name, "state": "incomplete", "pos": pos}
        cl.setdefault("checkItems", []).append(it)
        cl["checkItems"].sort(key=lambda i: i.get("pos") or 0)
        bid = self.model.id

        def job():
            new = self.store.api.add_item(self.R(cl), name, pos=pos)
            self.store.real[it["id"]] = new["id"]
            self.store.rec("item", {**new, "idChecklist": self.R(cl)}, bid, self.R(c))
        self.write(job, "Adding the item")
        self.push_undo(f"added “{name}”", lambda: self.delete_item(c, cl, it, undo=False))
        return it

    def delete_item(self, c, cl, it, undo=True):
        if it in cl.get("checkItems", []):
            cl["checkItems"].remove(it)
        self.repaint()
        bid = self.model.id

        def job():
            self.store.api.delete_item(self.R(cl), self.R(it))
            self.store.history.observe_deleted([("item", self.R(it), bid, self.R(c))])
        self.write(job, "Deleting the item")
        if undo:
            data = {"name": it["name"], "state": it["state"], "pos": it["pos"], "idChecklist": self.R(cl)}
            self.push_undo(f"deleted “{it['name']}”", lambda: self.new_item_back(c, cl, data))

    def new_item_back(self, c, cl, data):
        it = self.new_item(c, cl, data["name"], data["pos"])
        self.undo_stack.pop()
        if data["state"] == "complete":
            self.set_item(c, cl, it, undo=False, state="complete")

    def new_checklist(self, c, name) -> dict:
        m = self.model
        cls = m.checklists(c["id"])
        cl = {"id": tmp_id(), "name": name, "idCard": c["id"], "checkItems": [],
              "pos": (cls[-1]["pos"] + 65536) if cls else 65536}
        m.b["checklists"].append(cl)
        m.reindex()
        bid = m.id

        def job():
            new = self.store.api.create_checklist(self.R(c), name, pos=cl["pos"])
            self.store.real[cl["id"]] = new["id"]
            self.store.rec("checklist", new, bid, self.R(c))
        self.write(job, "Adding the checklist")
        self.push_undo(f"added checklist “{name}”", lambda: self.delete_checklist(c, cl, undo=False))
        return cl

    def rename_checklist(self, c, cl, name, undo=True):
        old = cl["name"]
        cl["name"] = name
        self.repaint()
        bid = self.model.id
        self.write(lambda: self.store.rec("checklist", self.store.api.update_checklist(self.R(cl), name=name),
                                          bid, self.R(c)), "Renaming the checklist")
        if undo:
            self.push_undo("renamed checklist", lambda: self.rename_checklist(c, cl, old, undo=False))

    def delete_checklist(self, c, cl, undo=True):
        m = self.model
        if cl in m.b["checklists"]:
            m.b["checklists"].remove(cl)
        m.reindex()
        self.repaint()
        bid = m.id
        when = time.time()
        gone = [("checklist", self.R(cl), bid, self.R(c))] + [("item", i["id"], bid, self.R(c)) for i in cl.get("checkItems", [])]

        def job():
            self.store.api.delete_checklist(self.R(cl))
            self.store.history.observe_deleted([(k, self.store.real.get(o, o), b, self.store.real.get(x, x)) for k, o, b, x in gone])
        self.write(job, "Deleting the checklist")
        if undo:
            n = len(cl.get("checkItems", []))
            self.notify(f"Deleted checklist “{cl['name']}” ({n} items) · {pretty(self.keys['undo'])} brings it back")
            self.push_undo(f"deleted checklist “{cl['name']}”", lambda: self.restore(
                Version(0, "checklist", self.R(cl), bid, self.R(c), when, "app", None,
                        prev={"name": cl["name"], "idCard": self.R(c), "pos": cl["pos"]}), None))

    # -- comments

    def add_comment(self, c, text):
        me = self.store.history.get("me") or {}
        a = {"id": tmp_id(), "idMemberCreator": self.me_id, "date": datetime.now(timezone.utc).isoformat(),
             "data": {"text": text, "card": {"id": c["id"]}}, "memberCreator": {"fullName": me.get("fullName", "me")}}
        self.model.comments.insert(0, a)
        self.model.reindex()
        self.repaint()
        bid = self.model.id

        def job():
            new = self.store.api.add_comment(self.R(c), text)
            self.store.real[a["id"]] = new["id"]
            self.store.rec("comment", new, bid, self.R(c))
        self.write(job, "Commenting")
        self.push_undo("comment", lambda: self.delete_comment(c, a, undo=False))

    def edit_comment(self, c, a, text):
        old = a["data"]["text"]
        a["data"]["text"] = text
        self.repaint()
        bid = self.model.id

        def job():
            self.store.api.update_comment(self.R(a), text)
            self.store.rec("comment", a, bid, self.R(c))
        self.write(job, "Saving the comment")
        self.push_undo("comment", lambda: self.edit_comment(c, a, old))

    def delete_comment(self, c, a, undo=True):
        self.model.comments.remove(a)
        self.model.reindex()
        self.repaint()
        bid = self.model.id

        def job():
            self.store.api.delete_comment(self.R(a))
            self.store.history.observe_deleted([("comment", self.R(a), bid, self.R(c))])
        self.write(job, "Deleting the comment")
        if undo:
            self.push_undo("deleted comment", lambda: self.add_comment(c, a["data"]["text"]) or self.undo_stack.pop())

    # -- lists

    def cur_list(self):
        bs = self.screen_stack[1] if len(self.screen_stack) > 1 else None
        return self.model.lists.get(bs.cur_list) if isinstance(bs, BoardScreen) and bs.cur_list else None

    def action_new_list(self):
        m = self.model
        lists = m.open_lists()

        def add(name):
            if not name or not name.strip():
                return
            cur = self.cur_list()
            i = lists.index(cur) + 1 if cur in lists else len(lists)
            pos = between(lists[i - 1]["pos"] if i > 0 else None, lists[i]["pos"] if i < len(lists) else None)
            lst = {"id": tmp_id(), "name": name.strip(), "closed": False, "pos": pos}
            m.b["lists"].append(lst)
            m.reindex()
            self.repaint()
            bid = m.id

            def job():
                new = self.store.api.create_list(bid, lst["name"], pos=pos)
                self.store.real[lst["id"]] = new["id"]
                self.store.rec("list", new, bid)
            self.write(job, "Adding the list")
            self.push_undo("new list", lambda: self.set_list(lst, undo=False, closed=True))
        self.push_screen(Prompt("New list"), add)

    def set_list(self, lst, undo=True, **fields):
        old = {k: lst.get(k) for k in fields}
        lst.update(fields)
        self.repaint()
        bid = self.model.id
        self.write(lambda: self.store.rec("list", self.store.api.update_list(self.R(lst), **fields), bid), "Saving the list")
        if undo:
            self.push_undo(f"list “{lst['name']}”", lambda: self.set_list(lst, undo=False, **old))

    def action_rename_list(self):
        lst = self.cur_list()
        if lst:
            self.push_screen(Prompt("List name", lst["name"]),
                             lambda t: t and t.strip() and self.set_list(lst, name=t.strip()))

    def action_archive_list(self):
        lst = self.cur_list()
        if lst:
            self.push_screen(Confirm(f"Archive list “{lst['name']}” with its cards?"),
                             lambda yes: yes and self.set_list(lst, closed=True))

    def action_move_list(self, d: int):
        lst = self.cur_list()
        lists = self.model.open_lists()
        if not lst:
            return
        i = lists.index(lst)
        others = [l for l in lists if l is not lst]
        j = i + d
        if not 0 <= j <= len(others):
            return
        self.set_list(lst, pos=between(others[j - 1]["pos"] if j > 0 else None,
                                       others[j]["pos"] if j < len(others) else None))

    def action_archived(self):
        m = self.model
        items = [{"text": c["name"], "id": c["id"], "detail": m.lists.get(c["idList"], {}).get("name", "")}
                 for c in m.b["cards"] if c.get("closed")]

        def chosen(it):
            if it:
                self.set_card(m.cards[it["id"]], closed=False, undo_label="unarchived")
                self.notify(f"“{it['text']}” is back")
        self.push_screen(Picker("Archived cards  ·  Enter brings it back", lambda q: rank(items, q, 300)), chosen)

    def action_new_card(self):
        self.screen_stack[1].action_new_card()

    def action_filter(self):
        self.screen_stack[1].action_filter()

    def action_browser(self):
        c = self.current_card()
        self.open_url((c or {}).get("shortUrl") or self.model.b.get("shortUrl"))

    # -- history, restore, undo

    def show_history(self, card_id: str | None):
        h = self.store.history
        if card_id:
            name = self.model.cards.get(card_id, {}).get("name", "")
            vs = fold_events(h.card_history(card_id))
            self.push_screen(HistoryScreen(f"History · {name}   [dim]Enter on a change: see it, put it back[/]", vs,
                                           show_card=False))
        else:
            vs = fold_events(h.board_history(self.model.id))
            self.push_screen(HistoryScreen(f"History · {self.model.b.get('name')}   "
                                           "[dim]Enter on a change: see it, put it back[/]", vs))

    def action_history(self):
        c = self.current_card() if isinstance(self.screen, CardScreen) else None
        self.show_history(c["id"] if c else None)

    def show_deleted(self):
        vs = fold_events(self.store.history.deleted(self.model.id))
        self.push_screen(HistoryScreen(f"Recently deleted · {self.model.b.get('name')}   "
                                       "[dim]Enter: restore[/]", vs, deleted_only=True))

    def action_deleted(self):
        self.show_deleted()

    def restore(self, v: Version, screen):
        board = self.model.b

        def done(msg):
            self.notify(msg)
            self.sync(full=True)
            if screen is not None and screen.is_mounted:
                self.call_later(self.pop_screen)
        self.store.submit("fg", lambda: self.store.restore(v, board), done,
                          lambda e: self.notify(f"Restore failed: {e}", severity="error", timeout=10))
        self.notify("Restoring…")

    def action_undo(self):
        if not self.undo_stack:
            return self.notify("Nothing to undo")
        label, fn = self.undo_stack.pop()
        fn()
        self.notify(f"Undone: {label}")
