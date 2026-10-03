---
name: trello
description: >
  Read and change the user's Trello boards through the `trello` CLI of the petrzpav.trello Omarchy
  plugin, including its full history (restore deleted checklists, items, cards, comments). Use
  whenever the user asks about Trello, their tasks, cards, boards, checklists or due dates:
  "what's on my plate", "add a card", "tick off …", "move X to Done", "what changed on …",
  "bring back the deleted checklist". Prefer it over the Trello website or raw API calls.
---

# Trello (`trello` CLI)

`trello` with no arguments opens the user's TUI client, so never run it bare. Always give it a subcommand.

## How it works

- Reads come from the local copy (`~/.local/share/petrzpav-trello/history.db`).
  - The client and `trello-snapshot.timer` keep it at most ~5 minutes old.
  - Add `--fetch` to `board`/`card` when freshness matters.
- Writes go to Trello and are recorded in the history like the client's own, so they can be reverted.
- The history DB is the user's data. Never delete or rewrite it.

## Naming things

- **Card:** its short link (the 8 characters of `trello.com/c/XXXXXXXX`, first column of every listing), its URL, its id, or a unique part of its name. Accents and case don't matter.
- **Board, list:** a unique part of the name.
- If a name is ambiguous, the error lists the candidates. Retry with the short link.

## Reading

```
trello boards
trello board BOARD [--list LIST] [--all] [--fetch]   # lists with cards: labels, due, ☑ done/total, members
trello card CARD [--fetch]          # description, checklists with [x] items, comments
trello mine [--due]                 # open cards assigned to the user, soonest due first
trello actions [--items 3]          # the client's "My actions": cards labelled `action` with the user, plus next open items
trello search 'words' [--all]       # every word in name, description, checklist items or comments
trello history CARD | --board BOARD [-n 50]   # changes, newest first, with a seq number
trello deleted BOARD                # deleted cards/checklists/items/comments that history remembers
```

Every read command takes `--json`.

## Writing

```
trello add BOARD LIST 'name' [--desc …|--desc-file F|-] [--due DATE] [--label L]… [--me] [--top]
trello update CARD [--name] [--desc|--desc-file] [--due DATE|none] [--done|--undone]
                   [--move [BOARD/]LIST [--top]] [--label L]… [--unlabel L]… [--me|--not-me]
                   [--archive|--unarchive]
trello comment CARD 'text'          # '-' = stdin
trello item CARD 'item' ['item'…] [--checklist NAME]   # first checklist, or the named one (created if missing)
trello check CARD 'part of item text' [--uncheck]
trello restore SEQ                  # seq from `history`/`deleted`: undo that change, or bring the deleted thing back
```

- DATE is `2026-10-05`, `2026-10-05 14:00`, `today`, `tomorrow` or `+3d`. A day without a time means 17:00 local.
- Descriptions and comments are Markdown.
- There is deliberately no delete. Archive instead (`--archive`); it's reversible.
- To undo a write, run `trello history CARD`, then `trello restore SEQ` on that entry.
- Writes are visible to everyone on the board.
  - Do what the user asked directly.
  - Confirm first when the change wasn't requested explicitly: a bulk change, moving other people's cards, or archiving.

## Tips

- "What should I do today?" → `trello actions` + `trello mine --due`.
- Before adding a card, check that a similar one doesn't exist: `trello search 'words'`.
- Config: `~/.config/petrzpav-trello/config.toml` (`actions_label`, `actions_member`). Never print `secrets`.
