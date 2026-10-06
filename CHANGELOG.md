# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## [Unreleased]

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
[0.2.1]: https://github.com/petrzpav/omarchy-trello/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/petrzpav/omarchy-trello/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/petrzpav/omarchy-trello/releases/tag/v0.1.0
