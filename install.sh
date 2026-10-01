#!/bin/bash
#
# install.sh - put `trello` on your PATH, link the history snapshot timer and start a
# config from examples/. Safe to run again: it never replaces a file it didn't create,
# and never touches an existing config.
#
#   install.sh              install
#   install.sh --remove     undo all of it (your config and history stay)

set -euo pipefail
root=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
bin="$HOME/.local/bin"
conf="${XDG_CONFIG_HOME:-$HOME/.config}/petrzpav-trello"

mine_link() { [[ -L $1 && $(readlink -f "$1") == "$root"/* ]]; }

link() {
  local target="$bin/$(basename "$1")"
  if [[ -e $target || -L $target ]] && ! mine_link "$target"; then
    echo "skipped $target: it already exists and isn't from this plugin"
    return
  fi
  ln -sfn "$1" "$target"
}

if [[ ${1:-} == --remove ]]; then
  systemctl --user disable --now trello-snapshot.timer >/dev/null 2>&1 || true
  for unit in trello-snapshot.timer trello-snapshot.service; do
    f="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$unit"
    mine_link "$f" && rm -f "$f"
  done
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  for f in "$bin/trello" "$bin/trello-window"; do
    mine_link "$f" && rm -f "$f"
  done
  echo "Removed. Your config ($conf) and history (~/.local/share/petrzpav-trello) are left in place."
  exit 0
fi

mkdir -p "$bin"
link "$root/bin/trello"
link "$root/bin/trello-window"

if [[ ! -e $conf ]]; then
  mkdir -p "$conf"
  cp "$root/examples/config.toml" "$root/examples/secrets" "$conf/"
  chmod 600 "$conf/secrets"
fi

systemctl --user link "$root/systemd/trello-snapshot.service" "$root/systemd/trello-snapshot.timer" >/dev/null

cat <<MSG
Installed. Next:
  1. trello auth                                          # API key + token
  2. trello-window                                        # open it
  3. systemctl --user enable --now trello-snapshot.timer  # history also catches what others change in the browser
MSG
