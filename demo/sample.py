"""A made-up Trello for screenshots: Mia's small web studio and the Alderbrew relaunch
(the same story as the Mail demo). Written into a fresh history database, so the app
opens it as if it had been synced."""

import hashlib
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)

MEMBERS = {
    "mia": {"fullName": "Mia Lind", "username": "mialind", "initials": "ML"},
    "jonas": {"fullName": "Jonas Berg", "username": "jonasberg", "initials": "JB"},
    "clara": {"fullName": "Clara Holm", "username": "claraholm", "initials": "CH"},
    "tom": {"fullName": "Tomas Ek", "username": "tomasek", "initials": "TE"},
}
LABELS = {"action": "red", "design": "purple", "dev": "blue", "client": "green", "waiting": "orange"}

# list -> cards: (name, labels, members, due in days or None, checklists {name: [(item, done)]}, desc, comments)
BOARDS = {
    "Alderbrew relaunch": {
        "Backlog": [
            ("Czech version of the site", ["client"], ["mia"], None,
             {"Scope": [("Quote for the translation", False), ("Which pages go first", False)]},
             "Jonas asked what a Czech version would cost, roughly. Quote by Friday.", []),
            ("Newsletter signup in the footer", ["dev"], ["tom"], None, {}, "", []),
            ("Seasonal menu page", ["design"], ["clara"], None,
             {"Content": [("Autumn menu photos", False), ("Allergen icons", False)]}, "", []),
        ],
        "This week": [
            ("Move the launch to October 14", ["action", "client"], ["mia"], 2,
             {"Launch": [("Tell the printer", True), ("New date in the press kit", False),
                         ("Update the countdown on the homepage", False)]},
             "The new menu is not printed yet, so the launch moves a week.",
             [("jonas", "Can we move the launch from October 7 to October 14? Our new menu is not printed yet.")]),
            ("Contact form to Jonas and Clara", ["action", "dev"], ["mia", "tom"], 1,
             {"Form": [("Second recipient", True), ("Spam check", False), ("Test from the phone", False)]},
             "", []),
            ("Invoice with the extra photo day", ["action", "client"], ["mia"], 0,
             {"Invoice": [("Add the photo day", True), ("Send it to Jonas", False)]}, "", []),
        ],
        "Doing": [
            ("Homepage hero and photos", ["design"], ["clara", "mia"], 3,
             {"Hero": [("Pick the photo", True), ("Crop for mobile", True), ("Headline in two lengths", False)],
              "Photos": [("Brewery tour set", True), ("Taproom at night", False)]},
             "Use the photos from the extra day; the taproom ones are the best.", []),
            ("Opening hours from Google", ["dev"], ["tom"], None,
             {"Hours": [("Read them from the profile", True), ("Holiday hours", False)]}, "", []),
        ],
        "Review": [
            ("Menu page", ["design", "waiting"], ["clara"], None,
             {"Menu": [("Beers on tap", True), ("Food", True), ("Prices", True)]},
             "Waiting for the printed menu to match the prices.",
             [("jonas", "Prices for the autumn beers come next week.")]),
        ],
        "Done": [
            ("Domain and hosting", ["dev"], ["tom"], None, {"Setup": [("DNS", True), ("TLS", True)]}, "", []),
            ("Logo files", ["design"], ["clara"], None, {}, "", []),
            ("Kick-off with Jonas", ["client"], ["mia"], None, {}, "", []),
        ],
    },
    "Studio": {
        "Inbox": [
            ("Portfolio: add the Alderbrew case", ["action"], ["mia"], 9,
             {"Case": [("Before and after shots", False), ("Short story, 150 words", False),
                       ("Ask Jonas for a quote", False)]}, "", []),
        ],
        "Running": [
            ("Quarterly taxes", ["action"], ["mia"], 12,
             {"Taxes": [("Export invoices", True), ("Send to the accountant", False)]}, "", []),
            ("New laptop for Tomas", [], ["tom"], None, {}, "", []),
        ],
        "Later": [
            ("Studio website refresh", ["design"], ["mia", "clara"], None, {}, "", []),
        ],
    },
}


def oid(*parts) -> str:
    return hashlib.sha1("/".join(parts).encode()).hexdigest()[:24]


def iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build():
    """(me, boards list, {board id: (board, comments)})."""
    members = [{"id": oid("member", k), **v} for k, v in MEMBERS.items()]
    mid = {k: oid("member", k) for k in MEMBERS}
    out, listing = {}, []
    for bi, (bname, lists) in enumerate(BOARDS.items()):
        bid = oid("board", bname)
        labels = [{"id": oid(bid, "label", n), "name": n, "color": c} for n, c in LABELS.items()]
        lid = {l["name"]: l["id"] for l in labels}
        board = {"id": bid, "name": bname, "desc": "", "closed": False, "shortUrl": f"https://trello.com/b/{bid[:8]}",
                 "dateLastActivity": iso(NOW - timedelta(minutes=5 + bi * 90)), "lists": [], "cards": [],
                 "checklists": [], "labels": labels, "members": members}
        comments = []
        for li, (lname, cards) in enumerate(lists.items()):
            l_id = oid(bid, "list", lname)
            board["lists"].append({"id": l_id, "name": lname, "closed": False, "pos": 65536 * (li + 1)})
            for ci, (name, labs, who, due, cls, desc, notes) in enumerate(cards):
                cid = oid(bid, "card", name)
                board["cards"].append({
                    "id": cid, "name": name, "desc": desc, "closed": False, "idList": l_id, "idBoard": bid,
                    "pos": 65536 * (ci + 1), "idLabels": [lid[x] for x in labs], "idMembers": [mid[x] for x in who],
                    "due": iso((NOW + timedelta(days=due)).replace(hour=15, minute=0)) if due is not None else None,
                    "dueComplete": lname == "Done", "start": None, "shortUrl": f"https://trello.com/c/{cid[:8]}",
                    "dateLastActivity": iso(NOW - timedelta(hours=li * 7 + ci))})
                for ki, (clname, items) in enumerate(cls.items()):
                    cl_id = oid(cid, "checklist", clname)
                    board["checklists"].append({"id": cl_id, "name": clname, "idCard": cid, "idBoard": bid,
                                                "pos": 65536 * (ki + 1), "checkItems": [
                        {"id": oid(cl_id, "item", t), "name": t, "idChecklist": cl_id, "pos": 65536 * (ii + 1),
                         "state": "complete" if done else "incomplete", "due": None, "idMember": None}
                        for ii, (t, done) in enumerate(items)]})
                for ni, (author, text) in enumerate(notes):
                    comments.append({"id": oid(cid, "comment", str(ni)), "date": iso(NOW - timedelta(hours=20 + ni)),
                                     "idMemberCreator": mid[author],
                                     "memberCreator": {"id": mid[author], "fullName": MEMBERS[author]["fullName"]},
                                     "data": {"text": text, "card": {"id": cid, "name": name}, "board": {"id": bid}}})
        out[bid] = (board, comments)
        listing.append({"id": bid, "name": bname, "closed": False, "dateLastActivity": board["dateLastActivity"],
                        "shortUrl": board["shortUrl"]})
    me = {"id": mid["mia"], "fullName": "Mia Lind", "username": "mialind", "initials": "ML"}
    return me, listing, out


def seed(history):
    """Put the made-up boards into `history` as the app's cache, opened on the relaunch board."""
    me, listing, boards = build()
    history.put("me", me)
    history.put("boards", listing)
    for bid, (board, comments) in boards.items():
        history.snapshot(board, comments)
        history.put(f"board:{bid}", board)
        history.put(f"comments:{bid}", comments)
        history.put(f"seen:{bid}", board["dateLastActivity"])
    history.put("last_board", listing[0]["id"])
