"""Scriptable Trello commands (for shell scripts and AI agents): read and change boards without
opening the client. Reads come from the local copy the client and `trello-snapshot.timer` keep
(at most ~5 minutes old; --fetch refreshes); every write is recorded in the history like the
client's own, so it can be reverted from there.

A card is named by its short link (the 8 characters in trello.com/c/XXXXXXXX), its id, its URL
or a unique part of its name; boards and lists by a unique part of their name (accents and
case don't matter).
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config
from .history import Version
from .store import Store


def fold(s: str) -> str:
    import unicodedata
    return "".join(unicodedata.normalize("NFD", ch)[0] for ch in (s or "")).lower()


def _die(msg: str):
    sys.exit(f"trello: {msg}")


def short(c: dict) -> str:
    return (c.get("shortUrl") or "").rsplit("/", 1)[-1] or c["id"]


def _due(c: dict) -> str:
    if not c.get("due"):
        return ""
    d = datetime.fromisoformat(c["due"].replace("Z", "+00:00")).astimezone()
    return d.strftime("%Y-%m-%d %H:%M") + (" ✓" if c.get("dueComplete") else "")


def parse_due(s: str) -> str | None:
    """'2026-10-05', '2026-10-05 14:00', 'today', 'tomorrow', '+3d', 'none'."""
    s = s.strip().lower()
    if s in ("none", "-", ""):
        return None
    now = datetime.now().astimezone()
    if s in ("today", "dnes"):
        d = now.replace(hour=17, minute=0, second=0, microsecond=0)
    elif s in ("tomorrow", "zitra", "zítra"):
        d = (now + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
    elif m := re.fullmatch(r"\+(\d+)d", s):
        d = (now + timedelta(days=int(m.group(1)))).replace(hour=17, minute=0, second=0, microsecond=0)
    else:
        d = datetime.fromisoformat(s)
        if len(s) <= 10:
            d = d.replace(hour=17)
        if d.tzinfo is None:
            d = d.astimezone()
    return d.astimezone().isoformat()


class Ctx:
    def __init__(self, cfg: Config):
        self.store = Store(cfg, lambda fn, *a: fn(*a))
        self.h = self.store.history
        self.api = self.store.api

    @property
    def me(self) -> dict:
        return self.store.me()

    def boards(self) -> list[dict]:
        return self.store.cached_boards() or self.store.fetch_boards()

    def board(self, bid: str, fetch=False) -> dict:
        b, _ = self.store.cached_board(bid)
        if fetch or not b:
            b, _ = self.store.fetch_board(bid, check_writes=False)
        return b

    def comments(self, bid: str) -> list[dict]:
        return self.store.cached_board(bid)[1]

    def refresh(self, bid: str):
        """After a write: the client and the next read see it at once."""
        self.store.fetch_board(bid, check_writes=False)

    def find_board(self, what: str) -> dict:
        boards = self.boards()
        hit = [b for b in boards if b["id"] == what or short(b) == what or (b.get("shortUrl") or "") == what]
        hit = hit or [b for b in boards if fold(b["name"]) == fold(what)] or \
            [b for b in boards if fold(what) in fold(b["name"])]
        if len(hit) != 1:
            _die(f"board {what!r}: " + (", ".join(b["name"] for b in hit) if hit else "no match; see `trello boards`"))
        return hit[0]

    def find_list(self, board: dict, what: str) -> dict:
        lists = [l for l in board["lists"] if not l.get("closed")]
        hit = [l for l in lists if l["id"] == what] or [l for l in lists if fold(l["name"]) == fold(what)] or \
            [l for l in lists if fold(what) in fold(l["name"])]
        if len(hit) != 1:
            _die(f"list {what!r} on {board['name']}: " +
                 (", ".join(l["name"] for l in hit) if hit else "no match; lists: " + ", ".join(l["name"] for l in lists)))
        return hit[0]

    def find_card(self, what: str, fetch=False) -> tuple[dict, dict]:
        """(card, board)."""
        what = what.strip()
        if m := re.search(r"trello\.com/c/([A-Za-z0-9]+)", what):
            what = m.group(1)
        boards = [self.board(b["id"]) for b in self.boards()]
        exact, named = [], []
        for b in boards:
            for c in b.get("cards", []):
                if c["id"] == what or short(c) == what:
                    exact.append((c, b))
                elif fold(what) in fold(c["name"]):
                    named.append((c, b))
        live = [x for x in named if not x[0].get("closed")]
        hit = exact or [x for x in live if fold(x[0]["name"]) == fold(what)] or live or \
            [x for x in named if fold(x[0]["name"]) == fold(what)] or named
        if len(hit) != 1:
            _die(f"card {what!r}: " + ("; ".join(f"{short(c)} {c['name']} ({b['name']})" for c, b in hit[:10])
                                        if hit else "no match"))
        c, b = hit[0]
        if fetch:
            b = self.board(b["id"], fetch=True)
            c = next(x for x in b["cards"] if x["id"] == c["id"])
        return c, b


def _names(b: dict, ids, key="labels", field="name") -> list[str]:
    by = {x["id"]: x for x in b.get(key, [])}
    out = []
    for i in ids or []:
        x = by.get(i)
        if x:
            out.append(x.get(field) or x.get("color") or "?")
    return out


def card_row(c: dict, b: dict) -> dict:
    lists = {l["id"]: l["name"] for l in b.get("lists", [])}
    return {"card": short(c), "name": c["name"], "board": b["name"], "list": lists.get(c["idList"], "?"),
            "due": _due(c), "labels": _names(b, c.get("idLabels")),
            "members": _names(b, c.get("idMembers"), "members", "fullName"), "closed": c.get("closed", False)}


def _print_cards(rows: list[dict], as_json: bool, with_board=True):
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        where = f"{r['board']} / {r['list']}" if with_board else r["list"]
        extra = "".join(f"  [{x}]" for x in r["labels"]) + (f"  due {r['due']}" if r["due"] else "") + \
            (f"  @{', '.join(r['members'])}" if r["members"] else "") + ("  (archived)" if r["closed"] else "")
        print(f"{r['card']}  {where}: {r['name']}{extra}")
    if not rows:
        print("(nothing)")


# ------------------------------------------------------------ reading

def cmd_boards(cfg: Config, args):
    ctx = Ctx(cfg)
    boards = ctx.store.fetch_boards() if args.fetch else ctx.boards()
    if args.json:
        print(json.dumps([{"id": b["id"], "name": b["name"], "url": b.get("shortUrl")} for b in boards],
                         ensure_ascii=False))
        return
    for b in boards:
        print(f"{b['id']}  {b['name']}")


def cmd_board(cfg: Config, args):
    ctx = Ctx(cfg)
    b = ctx.board(ctx.find_board(args.board)["id"], fetch=args.fetch)
    lists = [l for l in sorted(b["lists"], key=lambda l: l.get("pos") or 0) if args.all or not l.get("closed")]
    if args.list:
        lists = [ctx.find_list(b, args.list)]
    checklists = {}
    for cl in b.get("checklists", []):
        items = cl.get("checkItems", [])
        done, n = sum(i["state"] == "complete" for i in items), len(items)
        checklists.setdefault(cl["idCard"], [0, 0])
        checklists[cl["idCard"]][0] += done
        checklists[cl["idCard"]][1] += n
    out = []
    for l in lists:
        cards = sorted((c for c in b["cards"] if c["idList"] == l["id"] and (args.all or not c.get("closed"))),
                       key=lambda c: c.get("pos") or 0)
        rows = []
        for c in cards:
            r = card_row(c, b)
            if c["id"] in checklists:
                r["checklist"] = "{}/{}".format(*checklists[c["id"]])
            rows.append(r)
        out.append({"list": l["name"], "cards": rows})
    if args.json:
        print(json.dumps({"board": b["name"], "url": b.get("shortUrl"), "lists": out}, ensure_ascii=False))
        return
    print(f"# {b['name']}  {b.get('shortUrl', '')}")
    for l in out:
        print(f"\n## {l['list']} ({len(l['cards'])})")
        for r in l["cards"]:
            extra = "".join(f" [{x}]" for x in r["labels"]) + (f" due {r['due']}" if r["due"] else "") + \
                (f" ☑{r['checklist']}" if r.get("checklist") else "") + \
                (f" @{', '.join(r['members'])}" if r["members"] else "") + (" (archived)" if r["closed"] else "")
            print(f"  {r['card']}  {r['name']}{extra}")


def cmd_card(cfg: Config, args):
    ctx = Ctx(cfg)
    c, b = ctx.find_card(args.card, fetch=args.fetch)
    r = card_row(c, b)
    r["url"] = c.get("shortUrl")
    r["id"] = c["id"]
    r["desc"] = c.get("desc", "")
    r["checklists"] = [{"id": cl["id"], "name": cl["name"],
                        "items": [{"id": i["id"], "name": i["name"], "done": i["state"] == "complete"}
                                  for i in sorted(cl.get("checkItems", []), key=lambda i: i.get("pos") or 0)]}
                       for cl in sorted((x for x in b.get("checklists", []) if x["idCard"] == c["id"]),
                                        key=lambda x: x.get("pos") or 0)]
    r["comments"] = [{"id": a["id"], "author": (a.get("memberCreator") or {}).get("fullName", ""),
                      "date": a.get("date", "")[:16].replace("T", " "), "text": a["data"].get("text", "")}
                     for a in sorted(ctx.comments(b["id"]), key=lambda a: a.get("date", ""))
                     if (a.get("data", {}).get("card") or {}).get("id") == c["id"]]
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
        return
    print(f"{r['name']}\n{r['board']} / {r['list']}   {r['url']}   ({r['card']}, id {r['id']})")
    meta = [f"due {r['due']}" if r["due"] else "", "labels: " + ", ".join(r["labels"]) if r["labels"] else "",
            "members: " + ", ".join(r["members"]) if r["members"] else "", "ARCHIVED" if r["closed"] else ""]
    if any(meta):
        print("   ".join(m for m in meta if m))
    if r["desc"]:
        print("\n" + r["desc"].rstrip())
    for cl in r["checklists"]:
        done = sum(i["done"] for i in cl["items"])
        print(f"\n☑ {cl['name']} ({done}/{len(cl['items'])})")
        for i in cl["items"]:
            print(f"  [{'x' if i['done'] else ' '}] {i['name']}")
    if r["comments"]:
        print("\nComments:")
        for a in r["comments"]:
            print(f"  {a['date']} {a['author']}: " + a["text"].replace("\n", "\n    "))


def _card_text(c: dict, b: dict, comments: list[dict]) -> str:
    parts = [c["name"], c.get("desc", "")]
    parts += [i["name"] for cl in b.get("checklists", []) if cl["idCard"] == c["id"] for i in cl.get("checkItems", [])]
    parts += [a["data"].get("text", "") for a in comments if (a["data"].get("card") or {}).get("id") == c["id"]]
    return fold("\n".join(parts))


def cmd_search(cfg: Config, args):
    ctx = Ctx(cfg)
    words = fold(args.query).split()
    rows = []
    for bb in ctx.boards():
        b, comments = ctx.store.cached_board(bb["id"])
        if not b:
            continue
        for c in b.get("cards", []):
            if c.get("closed") and not args.all:
                continue
            t = _card_text(c, b, comments)
            if all(w in t for w in words):
                rows.append(card_row(c, b))
    _print_cards(rows[:args.limit], args.json)


def cmd_mine(cfg: Config, args):
    """Open cards with me on them, not in a Done list and not due-complete; soonest due first."""
    ctx = Ctx(cfg)
    me = ctx.me["id"]
    rows = []
    for bb in ctx.boards():
        b, _ = ctx.store.cached_board(bb["id"])
        open_lists = {l["id"] for l in (b or {}).get("lists", [])
                      if not l.get("closed") and not fold(l["name"]).startswith(("done", "hotovo"))}
        for c in (b or {}).get("cards", []):
            if me in (c.get("idMembers") or []) and not c.get("closed") and c["idList"] in open_lists \
                    and not c.get("dueComplete"):
                if args.due and not c.get("due"):
                    continue
                rows.append((c.get("due") or "9999", card_row(c, b)))
    rows.sort(key=lambda x: x[0])
    _print_cards([r for _, r in rows], args.json)


def cmd_actions(cfg: Config, args):
    """The client's My actions: open cards with the actions label and me on them, with their open items."""
    ctx = Ctx(cfg)
    label = cfg.actions_label.strip().casefold()
    want = cfg.actions_member.strip().casefold()
    out = []
    for bb in ctx.boards():
        b, _ = ctx.store.cached_board(bb["id"])
        if not b:
            continue
        lids = {l["id"] for l in b["labels"] if (l.get("name") or "").casefold() == label}
        who = ctx.me["id"] if not want else next(
            (m["id"] for m in b.get("members", []) if want in ((m.get("username") or "").casefold(),
             (m.get("fullName") or "").casefold(), (m.get("initials") or "").casefold())), None)
        if not lids or not who:
            continue
        lists = {l["id"] for l in b["lists"] if not l.get("closed")}
        for c in b["cards"]:
            if not c.get("closed") and c["idList"] in lists and who in (c.get("idMembers") or []) \
                    and lids & set(c.get("idLabels") or []):
                r = card_row(c, b)
                r["open_items"] = [i["name"] for cl in sorted((x for x in b["checklists"] if x["idCard"] == c["id"]),
                                                              key=lambda x: x.get("pos") or 0)
                                   for i in sorted(cl.get("checkItems", []), key=lambda i: i.get("pos") or 0)
                                   if i["state"] != "complete"]
                out.append((c.get("due") if c.get("due") and not c.get("dueComplete") else "9999", r))
    out.sort(key=lambda x: x[0])
    rows = [r for _, r in out]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    if not rows:
        print(f"(no open cards labelled “{cfg.actions_label}” with you on them)")
    for r in rows:
        print(f"{r['card']}  {r['name']}  ({r['board']} / {r['list']})" + (f"  due {r['due']}" if r["due"] else ""))
        for i in r["open_items"][:args.items or None]:
            print(f"    [ ] {i}")
        if args.items and len(r["open_items"]) > args.items:
            print(f"    … {len(r['open_items']) - args.items} more")


def _id_names(ctx: Ctx) -> dict[str, str]:
    """List, label and member ids of every cached board -> readable names (for history diffs)."""
    out = {}
    for bb in ctx.boards():
        b, _ = ctx.store.cached_board(bb["id"])
        for x in (b or {}).get("lists", []) + (b or {}).get("labels", []):
            out[x["id"]] = x.get("name") or x.get("color") or x["id"]
        for m in (b or {}).get("members", []):
            out[m["id"]] = m.get("fullName") or m["id"]
    return out


def _readable(v, names: dict):
    if isinstance(v, list):
        return [names.get(x, x) for x in v]
    return names.get(v, v) if isinstance(v, str) else v


def _version_row(v: Version, names: dict | None = None) -> dict:
    names = names or {}
    what = "deleted" if v.data is None else "created" if v.first else "changed"
    changes = {}
    if v.data is not None and v.prev:
        changes = {k: [_readable(v.prev.get(k), names), _readable(v.data.get(k), names)]
                   for k in v.data if v.prev.get(k) != v.data.get(k) and k != "pos"}
    name = ((v.data or v.prev or {}).get("name") or (v.data or v.prev or {}).get("text") or "")[:80]
    return {"seq": v.seq, "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(v.ts)), "kind": v.kind,
            "what": what, "name": name, "source": v.source, "card": v.card, "changes": changes}


def _print_versions(ctx: Ctx, vs: list[Version], as_json: bool):
    names = _id_names(ctx)
    rows = [_version_row(v, names) for v in vs if v.source != "import"]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, default=str))
        return
    for r in rows:
        ch = ""
        if r["changes"]:
            ch = "  " + "; ".join(f"{k}: {str(a)[:40]!r} → {str(b)[:40]!r}" for k, (a, b) in r["changes"].items())
        print(f"{r['seq']:>7}  {r['time']}  {r['what']:<7} {r['kind']:<9} {r['name']}{ch}")
    if not rows:
        print("(no changes recorded)")


def cmd_history(cfg: Config, args):
    ctx = Ctx(cfg)
    if args.board:
        vs = ctx.h.board_history(ctx.find_board(args.board)["id"], limit=args.limit)
    elif args.card:
        c, _ = ctx.find_card(args.card)
        vs = ctx.h.card_history(c["id"], limit=args.limit)
    else:
        _die("name a card or --board")
    _print_versions(ctx, vs, args.json)


def cmd_deleted(cfg: Config, args):
    ctx = Ctx(cfg)
    _print_versions(ctx, ctx.h.deleted(ctx.find_board(args.board)["id"], limit=args.limit), args.json)


def cmd_restore(cfg: Config, args):
    ctx = Ctx(cfg)
    vs = ctx.h._versions("seq=?", (args.seq,), 1)
    if not vs:
        _die(f"no history entry {args.seq}")
    v = vs[0]
    board = ctx.board(v.board, fetch=True)
    print(ctx.store.restore(v, board))
    ctx.refresh(v.board)


# ------------------------------------------------------------ writing

def _text(value: str | None, file: str | None) -> str | None:
    if file == "-":
        return sys.stdin.read()
    if file:
        return Path(file).expanduser().read_text()
    return value


def _label_ids(ctx: Ctx, b: dict, names: list[str]) -> list[str]:
    out = []
    for n in names:
        hit = [l for l in b["labels"] if fold(l.get("name") or "") == fold(n) or l["id"] == n] or \
            [l for l in b["labels"] if not l.get("name") and l.get("color") == n]
        if not hit:
            _die(f"no label {n!r} on {b['name']}; labels: " +
                 ", ".join(l.get("name") or l.get("color") for l in b["labels"]))
        out.append(hit[0]["id"])
    return out


def _member_ids(b: dict, names: list[str]) -> list[str]:
    out = []
    for n in names:
        want = fold(n.lstrip("@"))
        keys = lambda m: (fold(m.get("username") or ""), fold(m.get("fullName") or ""), fold(m.get("initials") or ""))
        hit = [m for m in b.get("members", []) if m["id"] == n or want in keys(m)] or \
            [m for m in b.get("members", []) if any(want in k for k in keys(m))]
        if len(hit) != 1:
            _die(f"member {n!r}: " + ("; ".join(m.get("fullName") or m.get("username") for m in hit) if hit else
                 f"not on {b['name']}; members: " + ", ".join(f"{m.get('fullName')} (@{m.get('username')})"
                                                              for m in b.get("members", []))))
        out.append(hit[0]["id"])
    return out


def cmd_add(cfg: Config, args):
    ctx = Ctx(cfg)
    b = ctx.board(ctx.find_board(args.board)["id"])
    lst = ctx.find_list(b, args.list)
    new = ctx.api.create_card(lst["id"], args.name, pos="top" if args.top else "bottom",
                              desc=_text(args.desc, args.desc_file),
                              idLabels=",".join(_label_ids(ctx, b, args.label or [])) or None,
                              due=parse_due(args.due) if args.due else None,
                              idMembers=ctx.me["id"] if args.me else None)
    ctx.store.rec("card", new, b["id"], new["id"])
    ctx.refresh(b["id"])
    print(f"created {short(new)} “{new['name']}” in {b['name']} / {lst['name']}  {new.get('shortUrl', '')}")


def cmd_update(cfg: Config, args):
    ctx = Ctx(cfg)
    c, b = ctx.find_card(args.card, fetch=True)
    f = {}
    if args.name:
        f["name"] = args.name
    desc = _text(args.desc, args.desc_file)
    if desc is not None:
        f["desc"] = desc
    if args.due:
        f["due"] = parse_due(args.due)
    if args.done or args.undone:
        f["dueComplete"] = bool(args.done)
    if args.archive or args.unarchive:
        f["closed"] = bool(args.archive)
    if args.move:
        target = b
        if "/" in args.move:
            bname, _, lname = args.move.partition("/")
            target = ctx.board(ctx.find_board(bname.strip())["id"])
            f["idBoard"] = target["id"]
        else:
            lname = args.move
        f["idList"] = ctx.find_list(target, lname.strip())["id"]
        f["pos"] = "top" if args.top else "bottom"
    labels = set(c.get("idLabels") or [])
    labels |= set(_label_ids(ctx, b, args.label or []))
    labels -= set(_label_ids(ctx, b, args.unlabel or []))
    if labels != set(c.get("idLabels") or []):
        f["idLabels"] = sorted(labels)
    members = set(c.get("idMembers") or [])
    if args.me or args.not_me:
        (members.add if args.me else members.discard)(ctx.me["id"])
    members |= set(_member_ids(b, args.member or []))
    members -= set(_member_ids(b, args.unmember or []))
    if members != set(c.get("idMembers") or []):
        f["idMembers"] = sorted(members)
    if not f:
        _die("nothing to change")
    ctx.store.rec("card", ctx.api.update_card(c["id"], **f), b["id"], c["id"])
    ctx.refresh(b["id"])
    if f.get("idBoard"):
        ctx.refresh(f["idBoard"])
    print(f"updated {short(c)} “{c['name']}”: {', '.join(f)}")


def cmd_comment(cfg: Config, args):
    ctx = Ctx(cfg)
    c, b = ctx.find_card(args.card)
    a = ctx.api.add_comment(c["id"], _text(args.text, "-" if args.text == "-" else None))
    ctx.store.rec("comment", a, b["id"], c["id"])
    ctx.refresh(b["id"])
    print(f"commented on {short(c)} “{c['name']}”")


def _checklist(ctx: Ctx, c: dict, b: dict, name: str | None, create=True) -> dict:
    cls = sorted((x for x in b.get("checklists", []) if x["idCard"] == c["id"]), key=lambda x: x.get("pos") or 0)
    if name:
        hit = [x for x in cls if fold(x["name"]) == fold(name)] or [x for x in cls if fold(name) in fold(x["name"])]
        if hit:
            return hit[0]
    elif cls:
        return cls[0]
    if not create:
        _die(f"no checklist {name!r} on the card")
    cl = ctx.api.create_checklist(c["id"], name or "Checklist", pos="bottom")
    ctx.store.rec("checklist", cl, b["id"], c["id"])
    return cl


def cmd_item(cfg: Config, args):
    ctx = Ctx(cfg)
    c, b = ctx.find_card(args.card, fetch=True)
    cl = _checklist(ctx, c, b, args.checklist)
    for name in args.items:
        new = ctx.api.add_item(cl["id"], name, pos="bottom")
        ctx.store.rec("item", {**new, "idChecklist": cl["id"]}, b["id"], c["id"])
    ctx.refresh(b["id"])
    print(f"added {len(args.items)} item(s) to “{cl['name']}” on {short(c)} “{c['name']}”")


def cmd_check(cfg: Config, args):
    ctx = Ctx(cfg)
    c, b = ctx.find_card(args.card, fetch=True)
    items = [(cl, i) for cl in b.get("checklists", []) if cl["idCard"] == c["id"] for i in cl.get("checkItems", [])]
    hit = [(cl, i) for cl, i in items if i["id"] == args.item] or \
        [(cl, i) for cl, i in items if fold(i["name"]) == fold(args.item)] or \
        [(cl, i) for cl, i in items if fold(args.item) in fold(i["name"])]
    if len(hit) != 1:
        _die(f"item {args.item!r}: " + ("; ".join(i["name"] for _, i in hit) if hit else "no match"))
    cl, it = hit[0]
    if args.rename:
        new = ctx.api.update_item(c["id"], it["id"], name=args.rename)
    else:
        new = ctx.api.update_item(c["id"], it["id"], state="incomplete" if args.uncheck else "complete")
    ctx.store.rec("item", {**new, "idChecklist": cl["id"]}, b["id"], c["id"])
    ctx.refresh(b["id"])
    if args.rename:
        print(f"renamed “{it['name']}” → “{args.rename}”")
    else:
        print(f"{'unchecked' if args.uncheck else 'checked'} “{it['name']}”")


# ------------------------------------------------------------ argparse

def add_parsers(sub):
    def js(p):
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("boards", help="your open boards")
    p.add_argument("--fetch", action="store_true")
    js(p)
    p.set_defaults(func=cmd_boards)

    p = sub.add_parser("board", help="a board's lists and cards")
    p.add_argument("board")
    p.add_argument("--list", help="only this list")
    p.add_argument("--all", action="store_true", help="archived lists and cards too")
    p.add_argument("--fetch", action="store_true", help="refresh from Trello first")
    js(p)
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("card", help="a card: description, checklists, comments")
    p.add_argument("card")
    p.add_argument("--fetch", action="store_true")
    js(p)
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("search", help="cards whose name, description, checklists or comments have every word")
    p.add_argument("query")
    p.add_argument("--all", action="store_true", help="archived cards too")
    p.add_argument("-n", "--limit", type=int, default=40)
    js(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("mine", help="open cards assigned to you, soonest due first")
    p.add_argument("--due", action="store_true", help="only cards with an open due date")
    js(p)
    p.set_defaults(func=cmd_mine)

    p = sub.add_parser("actions", help="My actions: cards with the actions label and you, with open items")
    p.add_argument("--items", type=int, default=3, help="open items shown per card (0 = all)")
    js(p)
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser("history", help="what changed on a card (or --board)")
    p.add_argument("card", nargs="?")
    p.add_argument("--board")
    p.add_argument("-n", "--limit", type=int, default=50)
    js(p)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("deleted", help="deleted cards, checklists, items and comments on a board")
    p.add_argument("board")
    p.add_argument("-n", "--limit", type=int, default=50)
    js(p)
    p.set_defaults(func=cmd_deleted)

    p = sub.add_parser("restore", help="bring back a deleted thing or revert a change (seq from history/deleted)")
    p.add_argument("seq", type=int)
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("add", help="create a card")
    p.add_argument("board")
    p.add_argument("list")
    p.add_argument("name")
    p.add_argument("--desc")
    p.add_argument("--desc-file", help="- for stdin")
    p.add_argument("--due", help="2026-10-05[ 14:00], today, tomorrow, +3d")
    p.add_argument("--label", action="append")
    p.add_argument("--me", action="store_true", help="assign to me")
    p.add_argument("--top", action="store_true")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("update", help="change a card")
    p.add_argument("card")
    p.add_argument("--name")
    p.add_argument("--desc")
    p.add_argument("--desc-file", help="- for stdin")
    p.add_argument("--due", help="date, today, tomorrow, +3d, or none")
    p.add_argument("--done", action="store_true", help="due date complete")
    p.add_argument("--undone", action="store_true")
    p.add_argument("--move", metavar="[BOARD/]LIST")
    p.add_argument("--top", action="store_true", help="with --move: to the top of the list")
    p.add_argument("--label", action="append", help="add a label (name, or colour of an unnamed one)")
    p.add_argument("--unlabel", action="append", help="remove a label")
    p.add_argument("--me", action="store_true", help="assign to me")
    p.add_argument("--not-me", action="store_true")
    p.add_argument("--member", action="append", help="add a board member (username, name or initials)")
    p.add_argument("--unmember", action="append", help="remove a member")
    p.add_argument("--archive", action="store_true")
    p.add_argument("--unarchive", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("comment", help="comment on a card")
    p.add_argument("card")
    p.add_argument("text", help="- for stdin")
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("item", help="add checklist items (to the first checklist, or --checklist, created if missing)")
    p.add_argument("card")
    p.add_argument("items", nargs="+")
    p.add_argument("--checklist")
    p.set_defaults(func=cmd_item)

    p = sub.add_parser("check", help="tick a checklist item (by part of its text)")
    p.add_argument("card")
    p.add_argument("item")
    p.add_argument("--uncheck", action="store_true")
    p.add_argument("--rename", metavar="TEXT", help="change the item's text instead of ticking it")
    p.set_defaults(func=cmd_check)
