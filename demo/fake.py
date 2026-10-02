"""A Trello that lives in memory, seeded from the boards the app has cached.

It answers the app's reads with those boards and applies its writes to them, so the
app behaves as it does online (a sync after a write sees the write), and nothing
ever reaches trello.com.
"""

import copy
import secrets
from datetime import datetime, timezone

from trellotui.api import TrelloError


def oid() -> str:
    return secrets.token_hex(12)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def num(pos, siblings: list) -> float:
    if isinstance(pos, (int, float)):
        return float(pos)
    ps = [x.get("pos") or 0 for x in siblings]
    if pos == "top":
        return (min(ps) if ps else 65536) / 2
    return (max(ps) if ps else 0) + 65536


def ids(v) -> list:
    if isinstance(v, str):
        return [x for x in v.split(",") if x]
    return list(v or [])


class FakeTrello:
    def __init__(self, history):
        self.me_ = history.get("me") or {"id": oid(), "fullName": "Me", "username": "me", "initials": "ME"}
        self.boards_ = history.get("boards", [])
        self.b: dict[str, dict] = {}
        self.c: dict[str, list] = {}
        for x in self.boards_:
            board = history.get(f"board:{x['id']}")
            if board:
                self.b[x["id"]] = copy.deepcopy(board)
                self.c[x["id"]] = copy.deepcopy(history.get(f"comments:{x['id']}", []))

    # -- lookups

    def _find(self, coll: str, xid: str):
        for b in self.b.values():
            for o in b[coll]:
                if o["id"] == xid:
                    return b, o
        raise TrelloError(f"404 {coll} {xid}")

    def _item(self, item_id: str):
        for b in self.b.values():
            for cl in b["checklists"]:
                for it in cl.get("checkItems", []):
                    if it["id"] == item_id:
                        return b, cl, it
        raise TrelloError(f"404 item {item_id}")

    def _touch(self, b):
        b["dateLastActivity"] = now()
        for x in self.boards_:
            if x["id"] == b["id"]:
                x["dateLastActivity"] = b["dateLastActivity"]

    # -- reading

    def req(self, method, path, **params):
        if method == "GET" and path.startswith("/boards/") and params.get("fields") == "dateLastActivity":
            return {"dateLastActivity": self.b[path.split("/")[2]].get("dateLastActivity")}
        raise TrelloError(f"demo: {method} {path} not faked")

    def me(self):
        return dict(self.me_)

    def boards(self):
        return copy.deepcopy(self.boards_)

    def board(self, board_id):
        return copy.deepcopy(self.b[board_id])

    def comments(self, board_id):
        return copy.deepcopy(self.c[board_id])

    # -- cards

    def create_card(self, idList, name, pos=None, desc=None, idLabels=None, due=None, idMembers=None):
        b, _ = self._find("lists", idList)
        sib = [c for c in b["cards"] if c["idList"] == idList]
        c = {"id": oid(), "name": name, "desc": desc or "", "closed": False, "idList": idList, "idBoard": b["id"],
             "pos": num(pos, sib), "idLabels": ids(idLabels), "idMembers": ids(idMembers), "due": due,
             "dueComplete": False, "start": None, "dateLastActivity": now(), "shortUrl": ""}
        b["cards"].append(c)
        self._touch(b)
        return copy.deepcopy(c)

    def update_card(self, card_id, **fields):
        b, c = self._find("cards", card_id)
        for k, v in fields.items():
            if k in ("idLabels", "idMembers"):
                v = ids(v)
            elif k in ("closed", "dueComplete") and isinstance(v, str):
                v = v == "true"
            elif k in ("due", "start") and v == "":
                v = None
            elif k == "pos":
                v = num(v, [x for x in b["cards"] if x["idList"] == c["idList"]])
            c[k] = v
        self._touch(b)
        return copy.deepcopy(c)

    def delete_card(self, card_id):
        b, c = self._find("cards", card_id)
        b["cards"].remove(c)
        b["checklists"] = [x for x in b["checklists"] if x["idCard"] != card_id]
        self._touch(b)

    # -- comments

    def add_comment(self, card_id, text):
        b, c = self._find("cards", card_id)
        a = {"id": oid(), "date": now(), "idMemberCreator": self.me_["id"],
             "memberCreator": {"id": self.me_["id"], "fullName": self.me_["fullName"]},
             "data": {"text": text, "card": {"id": card_id, "name": c["name"]}, "board": {"id": b["id"]}}}
        self.c[b["id"]].insert(0, a)
        self._touch(b)
        return copy.deepcopy(a)

    def _comment(self, action_id):
        for bid, cs in self.c.items():
            for a in cs:
                if a["id"] == action_id:
                    return self.b[bid], cs, a
        raise TrelloError(f"404 comment {action_id}")

    def update_comment(self, action_id, text):
        b, _, a = self._comment(action_id)
        a["data"]["text"] = text
        self._touch(b)
        return copy.deepcopy(a)

    def delete_comment(self, action_id):
        b, cs, a = self._comment(action_id)
        cs.remove(a)
        self._touch(b)

    # -- checklists

    def create_checklist(self, card_id, name, pos=None):
        b, _ = self._find("cards", card_id)
        cl = {"id": oid(), "name": name, "idCard": card_id, "idBoard": b["id"],
              "pos": num(pos, [x for x in b["checklists"] if x["idCard"] == card_id]), "checkItems": []}
        b["checklists"].append(cl)
        self._touch(b)
        return copy.deepcopy(cl)

    def update_checklist(self, cl_id, **fields):
        b, cl = self._find("checklists", cl_id)
        cl.update(fields)
        self._touch(b)
        return copy.deepcopy(cl)

    def delete_checklist(self, cl_id):
        b, cl = self._find("checklists", cl_id)
        b["checklists"].remove(cl)
        self._touch(b)

    def add_item(self, cl_id, name, pos=None, checked=False):
        b, cl = self._find("checklists", cl_id)
        items = cl.setdefault("checkItems", [])
        it = {"id": oid(), "name": name, "idChecklist": cl_id, "pos": num(pos, items),
              "state": "complete" if str(checked).lower() == "true" else "incomplete", "due": None, "idMember": None}
        items.append(it)
        self._touch(b)
        return copy.deepcopy(it)

    def update_item(self, card_id, item_id, **fields):
        b, cl, it = self._item(item_id)
        for k, v in fields.items():
            if k == "pos":
                v = num(v, cl["checkItems"])
            it[k] = v
        if fields.get("idChecklist", cl["id"]) != cl["id"]:
            cl["checkItems"].remove(it)
            self._find("checklists", fields["idChecklist"])[1].setdefault("checkItems", []).append(it)
        self._touch(b)
        return copy.deepcopy(it)

    def delete_item(self, cl_id, item_id):
        b, cl, it = self._item(item_id)
        cl["checkItems"].remove(it)
        self._touch(b)

    # -- lists and labels

    def create_list(self, board_id, name, pos=None):
        b = self.b[board_id]
        lst = {"id": oid(), "name": name, "closed": False, "pos": num(pos, b["lists"])}
        b["lists"].append(lst)
        self._touch(b)
        return copy.deepcopy(lst)

    def update_list(self, list_id, **fields):
        b, lst = self._find("lists", list_id)
        for k, v in fields.items():
            lst[k] = (v == "true") if k == "closed" and isinstance(v, str) else v
        self._touch(b)
        return copy.deepcopy(lst)

    def create_label(self, board_id, name, color):
        b = self.b[board_id]
        lb = {"id": oid(), "name": name, "color": color}
        b["labels"].append(lb)
        self._touch(b)
        return copy.deepcopy(lb)
