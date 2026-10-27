# Trello for Omarchy

A fast terminal Trello client for [Omarchy](https://omarchy.org). Arrow keys and Ctrl shortcuts
(no vim), `Ctrl+P` to jump to any card on any board, and a **history that remembers everything**:
delete a checklist by accident in Trello and its items are gone for good. Here you get them back.

![A tour of the client: the board, Ctrl+P, checklists, @ mentions, delete and restore, history, My actions](demo.gif)

Try it yourself without touching Trello: `demo/trello-demo --sample` runs the client offline on
the same made-up boards (or `demo/trello-demo` on a copy of your own cached ones).

## Why it's fast

Everything is painted from a local copy at once. Every change shows on screen first and goes to
Trello behind it, in order, so you never wait for the network. Searching (`Ctrl+P` across all boards,
`Ctrl+F` within one) never leaves your machine, and it ignores accents (`spindl` finds *Špindl*).

And it stays current. While the window has focus, the open board is checked every 10 seconds
with one tiny request (every minute when it doesn't), and coming back to the window checks at
once. Your other boards are checked every minute in one request for all of them. A change made
with the `trello` commands (or by Claude) shows in an open client within a second, with no
request at all. Set `poll` and `poll_idle` in `config.toml` to change the intervals.

## History

Every card, list, checklist, check item, label and comment is versioned in a local SQLite
database (`~/.local/share/petrzpav-trello/history.db`). The history is recorded:

- when the app syncs a board,
- when the app itself changes something (the moment Trello confirms it),
- every 3 minutes by `trello-snapshot.timer`, so changes made in the browser or by
  teammates are caught too.

`Alt+H` shows the history of a card (or of the board) and `Alt+R` lists what was deleted. Enter on an
event shows the before/after and puts it back. A deleted checklist comes back with all its items
and their ticks, and a deleted card with its checklists and comments. `Ctrl+Z` undoes your own
changes, again and again.

## My actions

`Alt+A` lists your open cards labelled **action**, from every board, with the next three open
checklist items of each. Overdue and due cards come first, then the most recently active ones.
`Space` ticks an item and the next one moves up, `→` shows all of a card's items (and `←` fewer,
then none), and `Enter` opens the card. The label, the member (you by default) and the number of
items per card are set in `config.toml`:

```toml
actions_label = "action"
actions_member = ""    # username, full name or initials; empty = you
actions_items = 3
```

## Keys

| Key | |
|---|---|
| `← → ↑ ↓`, `Enter` | move between lists and cards, open a card |
| `Shift+← →`, `Shift+↑ ↓` | move the card to the next list / up and down (items too, in a card) |
| `Ctrl+Shift+↑ ↓` | in a card, move the checklist under the cursor up and down |
| `Ctrl+P` | go to any card or board; `>` for commands |
| `Ctrl+O` | switch board |
| `Ctrl+F`, `Esc` | filter cards (name, description, labels, members, checklists, comments, attachments) |
| `Ctrl+N`, `Alt+N` | new card (in a card: new item, then the next one…), new checklist |
| `F2` / `Enter` | rename / edit what's under the cursor; on an attachment, open it |
| `Space` | tick an item |
| `← →` | in a card, on a checklist heading: fold / unfold it |
| `Alt+C` | hide / show completed checklist items (remembered) |
| `Ctrl+L` | move to another list (also on another board) |
| `Alt+L` | labels: `Enter` toggles one |
| `Alt+M` | members: `Enter` adds / removes one |
| `Alt+D` | due date |
| `Ctrl+R` | comment |
| `Ctrl+S`, `Esc` | in the description / comment editor: save, cancel |
| `@` | in the editor: mention a board member, picked from a list |
| `Delete` | archive the card; in a card, delete the item / checklist / comment |
| `Ctrl+Z` | undo |
| `Alt+H`, `Alt+R` | history, recently deleted |
| `Alt+O` | open in the browser |
| `Ctrl+C` | copy what's under the cursor: card name, item, description, comment, attachment link… |
| `Alt+Y` | copy the link of the card (or the board) |
| `Alt+A` | my actions: “action” cards I'm on, from every board |
| `F5`, `F1`, `Ctrl+Q` | refresh, help, quit |

Every key can be changed in `~/.config/petrzpav-trello/config.toml` under `[keys]`.

## Scripting and Claude Code

`trello boards`, `board BOARD`, `card CARD`, `attachment CARD [N]`, `mine`, `actions`, `search 'words'`, `add`, `update`
(move, due, labels, archive), `comment`, `item`, `check`, `history`, `deleted` and `restore SEQ`
work without the client and record every write in the history. They read the local copy after
one quick check against Trello, which re-reads only the boards that changed (`--cached` skips it,
for offline use). A card is its short link, URL or part of its name. Lists take `--json`; see `trello -h`.
`trello attachment CARD N` downloads an attached file to `~/.cache/petrzpav-trello/attachments` and
prints its path, so a script or an agent can read it; `--open` opens it, `-o DIR/` copies it there.

To let Claude Code manage your cards with the same commands, run `install.sh --skill`: it links
`skill/` into `~/.claude/skills/trello`, so Claude knows them in every project. Plain `install.sh`
leaves `~/.claude` alone; `--remove` takes the link back.

## Install

```
omarchy plugin add https://github.com/petrzpav/omarchy-trello --enable
~/.config/omarchy/plugins/petrzpav.trello/install.sh
trello auth
systemctl --user enable --now trello-snapshot.timer
```

`trello auth` asks for an API key. Get one at <https://trello.com/power-ups/admin>: create a
Power-Up (no capabilities, no iframe URL), open **Trello Auth**, generate an API key, and then
allow the token in the browser window it opens. You need [uv](https://docs.astral.sh/uv/);
Python and the dependencies are set up on first run.

## Remove

```
~/.config/omarchy/plugins/petrzpav.trello/install.sh --remove   # timer, ~/.local/bin links
omarchy plugin remove petrzpav.trello
rm -rf ~/.config/petrzpav-trello ~/.local/share/petrzpav-trello   # optional: config, key, history
```

`install.sh` only removes what it made: links it didn't create are left alone. The app talks to
`api.trello.com` only, with your own key and token (kept in `~/.config/petrzpav-trello/secrets`,
chmod 600).
