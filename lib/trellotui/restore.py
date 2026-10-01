"""Bring back what history remembers: deleted cards, checklists, items and comments,
or the state of something before a change. Runs on the write lane."""

from .history import History, Version


def _rec(h: History, kind, obj, board, card, oid=None):
    h.observe(kind, oid or obj["id"], board, card, obj, source="restore")
    return obj


def _list_for(board: dict, wanted: str | None) -> str:
    lists = [l for l in board.get("lists", []) if not l.get("closed")]
    if any(l["id"] == wanted for l in lists):
        return wanted
    if not lists:
        raise ValueError("the board has no open list to restore into")
    return lists[0]["id"]


def comment_text(c: dict) -> str:
    return f"*(restored comment by {c.get('author') or '?'}, {c.get('date', '')[:16].replace('T', ' ')})*\n\n{c['text']}"


def restore_checklist(api, h: History, bid, oid, data, ts, card_id):
    cl = api.create_checklist(card_id, data["name"], pos=data.get("pos"))
    _rec(h, "checklist", cl, bid, card_id)
    items = h.deleted_children("item", oid, "idChecklist", ts)
    h.mark_restored("checklist", oid)
    for it_oid, it in items:
        h.mark_restored("item", it_oid)
        new = api.add_item(cl["id"], it["name"], pos=it.get("pos"), checked=it.get("state") == "complete")
        _rec(h, "item", {**new, "idChecklist": cl["id"]}, bid, card_id)
    return len(items)


def restore_card(api, h: History, board, oid, data, ts) -> str:
    bid = board["id"]
    labels = {l["id"] for l in board.get("labels", [])}
    members = {m["id"] for m in board.get("members", [])}
    card = api.create_card(
        _list_for(board, data.get("idList")), data["name"], pos=data.get("pos"), desc=data.get("desc") or None,
        idLabels=",".join(l for l in data.get("idLabels") or [] if l in labels) or None,
        idMembers=",".join(m for m in data.get("idMembers") or [] if m in members) or None,
        due=data.get("due"))
    if data.get("dueComplete"):
        card = api.update_card(card["id"], dueComplete=True)
    _rec(h, "card", card, bid, card["id"])
    h.mark_restored("card", oid)
    n_cl = n_it = 0
    for cl_oid, cl in h.deleted_children("checklist", oid, "idCard", ts):
        n_it += restore_checklist(api, h, bid, cl_oid, cl, ts, card["id"])
        n_cl += 1
    comments = h.deleted_comments(oid, ts)
    with h.lock:
        gone = [r[0] for r in h.db.execute("SELECT oid FROM latest WHERE kind='comment' AND card=? AND data IS NULL", (oid,))]
    for c_oid in gone:
        h.mark_restored("comment", c_oid)
    for c in comments:
        api.add_comment(card["id"], comment_text(c))
    return (f"restored card “{data['name']}” with {n_cl} checklists, {n_it} items, "
            f"{len(comments)} comments")


def restore(api, h: History, v: Version, board: dict) -> str:
    bid = board["id"]
    if v.data is None:                       # a deletion: bring the object back
        data = v.prev or h.last_data(v.kind, v.oid)
        if data is None:
            raise ValueError("nothing remembered about it")
        if v.kind == "card":
            return restore_card(api, h, board, v.oid, data, v.ts)
        card_alive = v.card and h.alive("card", v.card)
        if v.kind in ("checklist", "item", "comment") and not card_alive:
            card_v = next((x for x in h.deleted(bid) if x.kind == "card" and x.oid == v.card), None)
            if not card_v:
                raise ValueError("its card is gone too and history doesn't have it")
            return restore_card(api, h, board, card_v.oid, card_v.prev or h.last_data("card", card_v.oid), card_v.ts)
        if v.kind == "checklist":
            n = restore_checklist(api, h, bid, v.oid, data, v.ts, v.card)
            return f"restored checklist “{data['name']}” with {n} items"
        if v.kind == "item":
            cl = data["idChecklist"]
            if not h.alive("checklist", cl):
                cl_data = h.last_data("checklist", cl)
                cl_v = next((x for x in h.deleted(bid) if x.kind == "checklist" and x.oid == cl), None)
                n = restore_checklist(api, h, bid, cl, cl_data, cl_v.ts if cl_v else v.ts, v.card)
                return f"restored checklist “{cl_data['name']}” with {n} items"
            new = api.add_item(cl, data["name"], pos=data.get("pos"), checked=data.get("state") == "complete")
            _rec(h, "item", {**new, "idChecklist": cl}, bid, v.card)
            h.mark_restored("item", v.oid)
            return f"restored item “{data['name']}”"
        if v.kind == "comment":
            api.add_comment(v.card, comment_text(data))
            h.mark_restored("comment", v.oid)
            return "restored comment"
        if v.kind == "list":
            raise ValueError("lists can't be restored")
        raise ValueError(f"can't restore a {v.kind}")

    # a change: put back what it was before
    old = v.prev
    if old is None:
        raise ValueError("this is when it was created; there's nothing before it")
    if not h.alive(v.kind, v.oid):
        raise ValueError(f"the {v.kind} has been deleted since; restore it from Deleted first")
    changed = {k: old[k] for k in old if old.get(k) != v.data.get(k)}
    if v.kind == "card":
        if "idList" in changed:
            changed["idList"] = _list_for(board, changed["idList"])
        _rec(h, "card", api.update_card(v.oid, **changed), bid, v.oid)
    elif v.kind == "checklist":
        changed.pop("idCard", None)
        _rec(h, "checklist", api.update_checklist(v.oid, **changed), bid, v.card)
    elif v.kind == "item":
        changed.pop("idChecklist", None)
        changed.pop("due", None)
        changed.pop("idMember", None)
        new = api.update_item(v.card, v.oid, **changed)
        _rec(h, "item", {**new, "idChecklist": v.data["idChecklist"]}, bid, v.card)
    elif v.kind == "comment":
        api.update_comment(v.oid, old["text"])
        _rec(h, "comment", {"text": old["text"], "author": old.get("author"), "date": old.get("date")},
             bid, v.card, oid=v.oid)
    elif v.kind == "list":
        changed.pop("pos", None)
        _rec(h, "list", api.update_list(v.oid, **changed), bid)
    else:
        raise ValueError(f"can't revert a {v.kind}")
    return "reverted to the earlier version"
