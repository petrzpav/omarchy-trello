# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-10-20

_Stable release based on [0.3.0-rc.1]._

## [0.3.0-rc.1] - 2026-10-20

### Added

- Typing `@` in a comment or description offers the board's members to mention.
- `Ctrl+C` copies what's under the cursor: a card's name, a checklist item, the description, a comment, a history entry.
- Coming back to the client's window checks Trello at once; `poll_idle` in `config.toml` sets how often it checks while the window has no focus.
- `--cached` on the reading commands reads the local copy without asking Trello, for offline use.

### Changed

- The open board is checked for changes every 10 seconds while the window has focus, and the other boards every minute, so changes made in the browser or by teammates show up sooner.
- The board on screen is re-read ahead of the other boards, so opening a board or a change made a moment ago never waits behind the background sync.
- Reading commands (`card`, `board`, `search`, `mine`, `actions`, …) check Trello first and re-read only the boards that changed, so they never show an old copy.
- A change shows confirmed by Trello within about a second and a half instead of four.
- `trello-snapshot.timer` runs every 3 minutes.

### Fixed

- Changes made with the `trello` commands (or by Claude) show in an open client within a second, instead of up to 10 minutes later.
- `trello-snapshot.timer` runs again: it never fired when the session started more than 2 minutes after boot.
- A board re-read that arrives while you edit no longer makes the edit disappear for a few seconds, and an older re-read never overwrites a newer copy or shows up in history as a revert.
- An edit, undo, label or member change made after the board re-synced shows on screen at once instead of only after the next sync.
- A card moved to another board (in the client or with `trello update --move`) shows there at once, and neither it nor its checklists, items and comments are listed as deleted in history.
- A lost connection after a suspend or a network change no longer stalls syncing for half a minute; the client tries again within seconds.

## [0.2.2] - 2026-10-06

### Changed

- The Claude Code skill is installed only with `install.sh --skill`.

## [0.2.1] - 2026-10-06

### Added

- Instructions for AI agents: the repository follows Flow (ig-flow and ig-changelog skills).

## [0.2.0] - 2026-10-06

### Added

- Scriptable commands (`boards`, `board`, `card`, `mine`, `actions`, `search`, `add`, `update`, `comment`, `item`, `check`, `history`, `deleted`, `restore`), every write recorded in the history, and a Claude Code skill.
- `trello open` shows a card or board in the client, opening or focusing its window.
- Renaming checklist items (`trello check --rename`) and assigning any board member (`trello update --member` / `--unmember`).
- Demo video from made-up boards.

### Changed

- F1 help in sections, one key per row, scrollable.

### Fixed

- The highlighted row is readable: labels, dates and members take the highlight's colours.
- No crash when adding checklist items while the board re-syncs.

## [0.1.0] - 2026-10-02

### Added

- Fast terminal Trello client with a full history that restores deleted checklists, items, cards and comments.
- My actions (`Alt+A`): open cards with the "action" label you are on, from every board, with their next open items.
- Foldable checklists, hiding done items (`Alt+C`), copying a card's or board's link (`Alt+Y`).
- Offline demo, and `install.sh` with `--remove`.

### Security

- Names typed by others are shown as plain text, never as Textual markup.

[Unreleased]: https://github.com/petrzpav/omarchy-trello/compare/staging...dev
[0.3.0]: https://github.com/petrzpav/omarchy-trello/compare/v0.2.2...v0.3.0
[0.3.0-rc.1]: https://github.com/petrzpav/omarchy-trello/releases/tag/v0.2.2
[0.2.1]: https://github.com/petrzpav/omarchy-trello/compare/v0.2.0...v0.2.1
[0.2.2]: https://github.com/petrzpav/omarchy-trello/compare/v0.2.1...v0.2.2
[0.2.1]: https://https://github.com/petrzpav/omarchy-trello/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/petrzpav/omarchy-trello/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/petrzpav/omarchy-trello/releases/tag/v0.1.0
