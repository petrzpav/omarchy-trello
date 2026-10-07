"""Load ~/.config/petrzpav-trello/{config.toml,secrets}."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "petrzpav-trello"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "petrzpav-trello"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "petrzpav-trello"
ERROR_LOG = STATE_DIR / "errors.log"

DEFAULT_KEYS = {
    "palette": "ctrl+p",
    "boards": "ctrl+o",
    "filter": "ctrl+f",
    "new": "ctrl+n",
    "new_checklist": "alt+n",
    "rename": "f2",
    "move": "ctrl+l",
    "labels": "alt+l",
    "members": "alt+m",
    "due": "alt+d",
    "comment": "ctrl+r",
    "archive": "delete",
    "undo": "ctrl+z",
    "history": "alt+h",
    "deleted": "alt+r",
    "browser": "alt+o",
    "copy": "ctrl+c",
    "copy_link": "alt+y",
    "actions": "alt+a",
    "hide_done": "alt+c",
    "refresh": "f5",
    "help": "f1",
    "save": "ctrl+s",
}


@dataclass
class Config:
    default_board: str = ""
    poll: int = 60                # seconds between syncs of the open board
    column_width: int = 32
    actions_label: str = "action"   # My actions: open cards with this label…
    actions_member: str = ""        # …and this member on them (username, full name or initials; empty = me)
    actions_items: int = 3          # open checklist items shown per card before "… more"
    keys: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def api_key(self) -> str:
        return self.secrets.get("TRELLO_API_KEY", "")

    @property
    def token(self) -> str:
        return self.secrets.get("TRELLO_TOKEN", "")


def read_secrets(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def load() -> Config:
    raw = {}
    path = CONFIG_DIR / "config.toml"
    if path.exists():
        raw = tomllib.loads(path.read_text())
    cfg = Config(
        default_board=raw.get("default_board", ""),
        poll=int(raw.get("poll", 60)),
        column_width=int(raw.get("column_width", 32)),
        actions_label=str(raw.get("actions_label", "action")),
        actions_member=str(raw.get("actions_member", "")),
        actions_items=int(raw.get("actions_items", 3)),
        keys={**DEFAULT_KEYS, **raw.get("keys", {})},
        secrets=read_secrets(CONFIG_DIR / "secrets"),
    )
    for k in ("TRELLO_API_KEY", "TRELLO_TOKEN"):
        if os.environ.get(k):
            cfg.secrets[k] = os.environ[k]
    return cfg


def save_secret(name: str, value: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / "secrets"
    lines = [l for l in (path.read_text().splitlines() if path.exists() else [])
             if not l.strip().startswith(name + "=")]
    lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
