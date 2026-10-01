"""Thin Trello REST client. Every method returns the JSON Trello sends back.

Auth goes in the Authorization header so the token never shows up in a URL or a log.
"""

import time

import httpx

BASE = "https://api.trello.com/1"

CARD_FIELDS = ("name,desc,closed,idList,idBoard,pos,idLabels,due,dueComplete,start,idMembers,"
               "dateLastActivity,shortUrl")


class TrelloError(Exception):
    pass


class Trello:
    def __init__(self, key: str, token: str):
        if not key or not token:
            raise TrelloError("TRELLO_API_KEY / TRELLO_TOKEN missing: run `trello auth`")
        self.http = httpx.Client(
            base_url=BASE, timeout=30, http2=False,
            headers={"Authorization": f'OAuth oauth_consumer_key="{key}", oauth_token="{token}"'})

    def req(self, method: str, path: str, **params):
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(5):
            r = self.http.request(method, path, params=params)
            if r.status_code == 429:          # 100 requests / 10 s per token
                time.sleep(1 + attempt * 2)
                continue
            if r.status_code >= 400:
                raise TrelloError(f"{method} {path}: {r.status_code} {r.text[:200]}")
            return r.json() if r.content else None
        raise TrelloError(f"{method} {path}: rate limited")

    # -- reading

    def me(self):
        return self.req("GET", "/members/me", fields="fullName,username,initials")

    def boards(self):
        return self.req("GET", "/members/me/boards", filter="open",
                        fields="name,closed,dateLastActivity,idOrganization,shortUrl")

    def board(self, board_id: str):
        """The whole board in one request: lists, every card (archived too, so history keeps
        them), checklists with their items, labels and members."""
        return self.req(
            "GET", f"/boards/{board_id}",
            fields="name,desc,closed,dateLastActivity,shortUrl",
            lists="all", list_fields="name,closed,pos",
            cards="all", card_fields=CARD_FIELDS,
            checklists="all", checklist_fields="name,idCard,pos",
            labels="all", label_fields="name,color", labels_limit=1000,
            members="all", member_fields="fullName,username,initials")

    def comments(self, board_id: str):
        return self.req("GET", f"/boards/{board_id}/actions", filter="commentCard", limit=1000,
                        fields="data,date,idMemberCreator", memberCreator_fields="fullName")

    # -- cards

    def create_card(self, idList, name, pos=None, desc=None, idLabels=None, due=None, idMembers=None):
        return self.req("POST", "/cards", idList=idList, name=name, pos=pos, desc=desc,
                        idLabels=idLabels, due=due, idMembers=idMembers)

    def update_card(self, card_id, **fields):
        for k in ("idLabels", "idMembers"):
            if isinstance(fields.get(k), list):
                fields[k] = ",".join(fields[k])
        for k, v in list(fields.items()):
            if v is None and k in ("due", "start"):
                fields[k] = ""
            elif isinstance(v, bool):
                fields[k] = str(v).lower()
        return self.req("PUT", f"/cards/{card_id}", **fields)

    def delete_card(self, card_id):
        return self.req("DELETE", f"/cards/{card_id}")

    # -- comments

    def add_comment(self, card_id, text):
        return self.req("POST", f"/cards/{card_id}/actions/comments", text=text)

    def update_comment(self, action_id, text):
        return self.req("PUT", f"/actions/{action_id}", text=text)

    def delete_comment(self, action_id):
        return self.req("DELETE", f"/actions/{action_id}")

    # -- checklists

    def create_checklist(self, card_id, name, pos=None):
        return self.req("POST", "/checklists", idCard=card_id, name=name, pos=pos)

    def update_checklist(self, cl_id, **fields):
        return self.req("PUT", f"/checklists/{cl_id}", **fields)

    def delete_checklist(self, cl_id):
        return self.req("DELETE", f"/checklists/{cl_id}")

    def add_item(self, cl_id, name, pos=None, checked=False):
        return self.req("POST", f"/checklists/{cl_id}/checkItems", name=name, pos=pos,
                        checked=str(bool(checked)).lower())

    def update_item(self, card_id, item_id, **fields):
        return self.req("PUT", f"/cards/{card_id}/checkItem/{item_id}", **fields)

    def delete_item(self, cl_id, item_id):
        return self.req("DELETE", f"/checklists/{cl_id}/checkItems/{item_id}")

    # -- lists and labels

    def create_list(self, board_id, name, pos=None):
        return self.req("POST", "/lists", idBoard=board_id, name=name, pos=pos)

    def update_list(self, list_id, **fields):
        if isinstance(fields.get("closed"), bool):
            fields["closed"] = str(fields["closed"]).lower()
        return self.req("PUT", f"/lists/{list_id}", **fields)

    def create_label(self, board_id, name, color):
        return self.req("POST", "/labels", idBoard=board_id, name=name, color=color)
