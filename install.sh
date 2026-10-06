#!/bin/bash
#
# install.sh - put `trello` on your PATH, link the history snapshot timer and start a
# config from examples/. Safe to run again: it never replaces a file it didn't create,
# and never touches an existing config.
#
#   install.sh              install
#   install.sh --skill      install, and also let Claude Code use `trello` (~/.claude/skills/trello)
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

units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [[ ${1:-} == --remove ]]; then
  # Only units linked from this plugin: a foreign timer with the same name is left alone.
  if mine_link "$units/trello-snapshot.timer"; then
    systemctl --user disable --now trello-snapshot.timer >/dev/null 2>&1 || true
  fi
  for unit in trello-snapshot.timer trello-snapshot.service; do
    mine_link "$units/$unit" && rm -f "$units/$unit"
  done
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  for f in "$bin/trello" "$bin/trello-window" "$HOME/.claude/skills/trello"; do
    mine_link "$f" && rm -f "$f"
  done
  echo "Removed. Your config ($conf) and history (~/.local/share/petrzpav-trello) are left in place."
  exit 0
fi

mkdir -p "$bin"
link "$root/bin/trello"
link "$root/bin/trello-window"
if [[ ${1:-} == --skill ]]; then   # only when asked: a skill is visible to Claude Code in every project
  skill="$HOME/.claude/skills/trello"
  if [[ -e $skill || -L $skill ]] && ! mine_link "$skill"; then
    echo "skipped $skill: it already exists and isn't from this plugin"
  else
    mkdir -p "$HOME/.claude/skills" && ln -sfn "$root/skill" "$skill"
  fi
fi

if [[ ! -e $conf ]]; then
  mkdir -p "$conf"
  cp "$root/examples/config.toml" "$root/examples/secrets" "$conf/"
  chmod 600 "$conf/secrets"
fi

for unit in trello-snapshot.service trello-snapshot.timer; do
  if [[ -e $units/$unit || -L $units/$unit ]] && ! mine_link "$units/$unit"; then
    echo "skipped $units/$unit: it already exists and isn't from this plugin"
  else
    systemctl --user link "$root/systemd/$unit" >/dev/null
  fi
done

cat <<MSG
Installed. Next:
  1. trello auth                                          # API key + token
  2. trello-window                                        # open it
  3. systemctl --user enable --now trello-snapshot.timer  # history also catches what others change in the browser
MSG
