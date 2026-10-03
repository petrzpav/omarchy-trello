import argparse
import json
import sys
import webbrowser

from . import config


def cmd_auth(args):
    print("1. Open https://trello.com/power-ups/admin → New → (any name, no capabilities, no iframe URL)")
    print("   → Trello Auth → API key → copy it.")
    key = input("API key: ").strip()
    url = ("https://trello.com/1/authorize?expiration=never&scope=read,write&response_type=token"
           f"&name=Omarchy%20Trello&key={key}")
    print(f"2. Allow access in the browser (opening {url})")
    webbrowser.open(url)
    token = input("Token: ").strip()
    config.save_secret("TRELLO_API_KEY", key)
    config.save_secret("TRELLO_TOKEN", token)
    from .api import Trello
    print("Signed in as", Trello(key, token).me()["fullName"])


def cmd_snapshot(cfg, args):
    from .store import Store
    store = Store(cfg, lambda fn, *a: fn(*a))
    out = store.snapshot_all(force=args.force)
    if args.verbose:
        print(json.dumps(out, ensure_ascii=False))


def cmd_due(cfg, args):
    from .store import DUE_FILE, Store
    if args.cached and DUE_FILE.exists():
        print(DUE_FILE.read_text().strip())
        return
    store = Store(cfg, lambda fn, *a: fn(*a))
    store.write_due()
    print(DUE_FILE.read_text().strip() if DUE_FILE.exists() else 0)


def main():
    p = argparse.ArgumentParser(prog="trello", description="Fast terminal Trello with full history")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("auth", help="store your Trello API key and token")
    s = sub.add_parser("snapshot", help="record every changed board in the history (run by the timer)")
    s.add_argument("--force", action="store_true", help="every board, changed or not")
    s.add_argument("-v", "--verbose", action="store_true")
    d = sub.add_parser("due", help="print how many of your cards are due within a day")
    d.add_argument("--cached", action="store_true")
    from . import tools
    tools.add_parsers(sub)
    args = p.parse_args()

    if args.cmd == "auth":
        return cmd_auth(args)
    cfg = config.load()
    if not cfg.api_key or not cfg.token:
        sys.exit("No Trello key yet: run `trello auth`")
    if getattr(args, "func", None):
        return args.func(cfg, args)
    if args.cmd == "snapshot":
        cmd_snapshot(cfg, args)
    elif args.cmd == "due":
        cmd_due(cfg, args)
    else:
        from .app import TrelloApp
        TrelloApp(cfg).run()


if __name__ == "__main__":
    main()
