"""Run the Trello client on a copy of your cached boards, offline.

    demo/trello-demo            the client, in this terminal
    demo/trello-demo record     render demo/out/trello-demo.mp4 (headless)
    demo/trello-demo preview    render preview.png from the made-up boards

    --sample                    made-up boards (demo/sample.py) instead of your own

The history database is copied to a temp dir and Trello is replaced by demo/fake.py,
so you can tick, delete and restore freely: nothing reaches trello.com and your own
history stays as it was.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def setup(sample=False):
    real = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "petrzpav-trello/history.db"
    if not sample and not real.exists():
        sys.exit(f"demo: no cached boards at {real}: run `trello` once first, or use --sample")
    tmp = Path(tempfile.mkdtemp(prefix="trello-demo-"))
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        os.environ[var] = str(tmp / sub)
    (tmp / "data/petrzpav-trello").mkdir(parents=True)
    sys.path[:0] = [str(HERE.parent / "lib"), str(HERE)]
    if sample:
        import sample as made_up
        from trellotui.history import History
        made_up.seed(History())
    else:
        src = sqlite3.connect(f"file:{real}?mode=ro", uri=True)
        dst = sqlite3.connect(tmp / "data/petrzpav-trello/history.db")
        src.backup(dst)
        src.close()
        dst.close()

    import webbrowser
    webbrowser.open = lambda *a, **kw: True

    from trellotui import config
    cfg = config.load()
    cfg.secrets = {"TRELLO_API_KEY": "demo", "TRELLO_TOKEN": "demo"}
    return cfg, tmp


def make_app(cfg):
    import fake
    from trellotui.app import TrelloApp

    app = TrelloApp(cfg)
    app.store.api = fake.FakeTrello(app.store.history)
    return app


def main():
    args = [a for a in sys.argv[1:] if a != "--sample"]
    cfg, tmp = setup(sample="--sample" in sys.argv or args[:1] == ["preview"])
    try:
        if args[:1] == ["record"]:
            import record
            record.main(cfg, make_app, args[1:])
        elif args[:1] == ["preview"]:
            import record
            record.preview(cfg, make_app)
        else:
            make_app(cfg).run()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
