"""Drive the app headless through a tour and render it to an MP4.

Every key goes through the real bindings (pilot.press); each step saves an SVG
screenshot with how long it stays on screen and a caption. rsvg-convert, ImageMagick
and ffmpeg turn those into 1920x1080 video.

    trello-demo record [BOARD] [CARD]     board and card by name (defaults below)
"""

import asyncio
import hashlib
import html
import re
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
COLS, ROWS = 118, 34
FONT = "JetBrainsMono Nerd Font"      # what Omarchy's terminals use
NARROW = "Adwaita Mono"               # the terminal's fallback for ✔ ☐ ☑ (rsvg's would be a wide emoji)
CAPTION_FONT = "iA-Writer-Duo-S-Bold"
BG = "#11131a"
PACE = 1.6              # every hold is this much longer (typing keeps its speed)
LINGER = 1.2            # extra seconds on the last frame of a step, before the next caption
BOARD, CARD = "Projects", "laravel-daily-menu"
END = ((88, "white", -70, "Trello for Omarchy"),
       (44, "#b8bcc8", 30, "Fast in the terminal, and nothing is ever lost"),
       (36, "#7d8290", 110, "history · restore · Ctrl+P to anything"))


class Film:
    def __init__(self, app, pilot):
        self.app, self.pilot = app, pilot
        self.frames: list[tuple[str, float, str]] = []
        self.caption = ""

    async def snap(self, seconds: float, caption: str | None = None, settle: float = 0.08, pace=PACE):
        await self.pilot.pause(settle)
        if caption is not None:
            if caption != self.caption:
                print("·", caption or "-", flush=True)
                if self.frames and self.frames[-1][0]:        # let the step's result sink in
                    svg, held, cap = self.frames[-1]
                    self.frames[-1] = (svg, held + LINGER, cap)
            self.caption = caption
        self.frames.append((self.app.export_screenshot(title="Trello"), seconds * pace, self.caption))

    async def key(self, k: str, seconds=0.45, caption: str | None = None):
        await self.pilot.press(k)
        await self.snap(seconds, caption)

    async def type(self, s: str, caption: str | None = None):
        for ch in s:
            await self.pilot.press("space" if ch == " " else ch)
            await self.snap(0.09, caption, settle=0.02, pace=1)

    async def until(self, cond, timeout=10.0):
        end = time.time() + timeout
        while not cond():
            if time.time() > end:
                raise TimeoutError("demo step never finished")
            await self.pilot.pause(0.05)


def row_ids(app):
    return [o.id for o in app.screen.query_one("#rows")._options]


async def goto_row(film: Film, want, step=0.12):
    """Walk the cursor down/up to the first row `want(id)` accepts, a frame per step."""
    rows = film.app.screen.query_one("#rows")
    target = next(i for i, x in enumerate(row_ids(film.app)) if want(x or ""))
    while rows.highlighted != target:
        await film.key("down" if rows.highlighted < target else "up", step)


async def story(app, film: Film, board_name: str, card_name: str):
    from trellotui.app import BoardScreen, CardScreen, HistoryScreen

    m = app.model
    bid = next(b["id"] for b in app.store.cached_boards() if b["name"] == board_name)
    if m.id != bid:
        app.open_board(bid)
    await film.until(lambda: app.model.id == bid and isinstance(app.screen, BoardScreen) and app.screen.query("CardList"))
    await film.snap(3.0, f"Your Trello board, in the terminal")

    # -- walk the lists
    for _ in range(4):
        await film.key("right", 0.35, "Arrows walk lists and cards")
    for _ in range(3):
        await film.key("down", 0.3)
    await film.snap(0.8)

    # -- filter
    await film.key("ctrl+f", 0.5, "Ctrl+F filters cards by any text in them")
    await film.type("laravel")
    await film.snap(2.6)
    await film.key("escape", 0.6, "")

    # -- Ctrl+P: jump to a card
    await film.key("ctrl+p", 1.2, "Ctrl+P jumps to any card on any board")
    await film.type(card_name.replace("-", " ")[:13])
    await film.snap(1.6)
    await film.key("enter", 0.3)
    await film.until(lambda: isinstance(app.screen, CardScreen))
    await film.snap(2.6, "Card: description, checklists, comments")

    card = app.screen.card
    cls = app.model.checklists(card["id"])
    done_cl = max(cls, key=lambda cl: sum(i["state"] == "complete" for i in cl["checkItems"]))
    open_cl = next(cl for cl in cls if cl is not done_cl and any(i["state"] != "complete" for i in cl["checkItems"]))
    gone_cl = next((cl for cl in cls if cl not in (done_cl, open_cl)), open_cl)

    # -- fold / unfold
    await goto_row(film, lambda x: x == f"cl:{done_cl['id']}")
    await film.snap(0.8, "← → on a checklist folds and unfolds it")
    await film.key("left", 1.6)
    await film.key("right", 1.4)

    # -- hide done
    await film.key("alt+c", 2.6, "Alt+C hides what's done: only what's left")
    # -- tick
    await goto_row(film, lambda x: x.startswith(f"it:{open_cl['id']}:"))
    await film.snap(0.8, "Space ticks an item")
    await film.key("space", 1.8)
    await film.key("alt+c", 1.4, "Alt+C again shows everything")

    # -- delete a checklist
    await goto_row(film, lambda x: x == f"cl:{gone_cl['id']}")
    await film.snap(1.0, f"Delete a whole checklist, {len(gone_cl['checkItems'])} items…")
    await film.key("delete", 2.6, "Gone. On trello.com that's forever")
    await film.until(lambda: not app.store.inflight)

    # -- restore it from Recently deleted
    await film.key("escape", 0.6, "Alt+R lists everything deleted on the board")
    await film.key("alt+r", 2.2)
    await film.until(lambda: isinstance(app.screen, HistoryScreen))
    await film.key("enter", 2.4, "Enter shows it, Enter again restores it with all its items")
    await film.key("enter", 0.4)
    await film.until(lambda: not app.store.inflight and not isinstance(app.screen, HistoryScreen), 15)
    await film.until(lambda: not app.syncing, 15)

    # -- the card again, then its history
    await film.pilot.pause(0.5)
    await film.key("ctrl+p", 1.4, "Ctrl+P remembers the cards you opened")
    await film.key("enter", 0.3)
    await film.until(lambda: isinstance(app.screen, CardScreen))
    back = next(cl for cl in app.model.checklists(card["id"]) if cl["name"] == gone_cl["name"])
    await goto_row(film, lambda x: x == f"cl:{back['id']}", 0.08)
    for _ in range(min(4, len(back["checkItems"]))):
        await film.key("down", 0.12)
    await film.snap(2.6, f"“{back['name']}” is back, all {len(back['checkItems'])} items")
    await film.key("alt+h", 3.6, "Alt+H: every change to the card, each one can be put back")
    await film.key("escape", 0.3, "")
    await film.key("escape", 0.5)

    # -- commands
    await film.key("ctrl+p", 0.4, "Ctrl+P and > runs any command")
    await film.type(">")
    await film.snap(3.0)
    await film.key("escape", 0.6, "")
    film.frames.append(("", 4.0, ""))


# ------------------------------------------------------------ rendering

def fix_svg(svg: str) -> str:
    """Make rsvg draw Textual's SVG the way a terminal does."""
    svg = re.sub(r'font-family:\s*"?Fira Code"?(, monospace)?', f'font-family: "{FONT}", monospace', svg)
    svg = svg.replace("font-family: arial", f'font-family: "{FONT}"')

    def run(m):             # rsvg drops leading/trailing nbsp and stretches the rest: shift x instead
        attrs, chars = m.group(1), html.unescape(m.group(2))
        if not chars:
            return m.group(0)
        cw = float(re.search(r'textLength="([\d.]+)"', attrs).group(1)) / len(chars)
        core = chars.strip("\xa0")
        if not core:
            return ""
        x = float(re.search(r' x="([\d.]+)"', attrs).group(1)) + (len(chars) - len(chars.lstrip("\xa0"))) * cw
        attrs = re.sub(r' x="[\d.]+"', f' x="{x:.1f}"', attrs)
        attrs = re.sub(r'textLength="[\d.]+"', f'textLength="{len(core) * cw:.1f}"', attrs)
        body = html.escape(core, quote=False)
        for g in "✔☐☑":
            body = body.replace(g, f'<tspan style="font-family: {NARROW}">{g}</tspan>')
        return f"<text{attrs}>{body}</text>"
    return re.sub(r"<text([^>]*textLength[^>]*)>([^<]*)</text>", run, svg)


def render(frames, out: Path, work: Path, fps=30):
    """SVG screenshots -> captioned 1920x1080 PNGs -> one image per video frame -> MP4."""
    work.mkdir(parents=True, exist_ok=True)
    pngs: dict[str, Path] = {}
    seq = work / "seq"
    seq.mkdir()
    n, t = 0, 0.0
    for svg, seconds, caption in frames:
        key = hashlib.sha1((svg + caption).encode()).hexdigest()[:16]
        if key not in pngs:
            pngs[key] = card(work / key, caption, fix_svg(svg)) if svg else card(work / key, "", lines=END)
        t += seconds
        while n < round(t * fps):                    # cumulative, so short frames never drift
            (seq / f"{n:05d}.png").hardlink_to(pngs[key])
            n += 1
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", seq / "%05d.png",
                    "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-movflags", "+faststart", out], check=True)


def card(base: Path, caption: str, svg: str = "", lines: tuple = ()) -> Path:
    """One 1920x1080 frame: the screenshot on top with the caption under it, or a title card."""
    frame = base.with_suffix(".png")
    cmd = ["magick", "-size", "1920x1080", f"xc:{BG}"]
    if svg:
        src, shot = base.with_suffix(".svg"), base.with_name(base.name + "-shot.png")
        src.write_text(svg)
        subprocess.run(["rsvg-convert", "-w", "1560", "-o", shot, src], check=True)
        cmd += [shot, "-gravity", "north", "-geometry", "+0+18", "-composite"]
    if caption:
        cmd += ["-font", CAPTION_FONT, "-pointsize", "48", "-fill", "white", "-gravity", "south",
                "-annotate", "+0+34", caption]
    for size, colour, dy, text in lines:
        cmd += ["-font", CAPTION_FONT, "-pointsize", str(size), "-fill", colour, "-gravity", "center",
                "-annotate", f"+0{dy:+d}", text]
    subprocess.run(cmd + [frame], check=True)
    return frame


def preview(cfg, make_app):
    """preview.png for the marketplace: the made-up relaunch board, a card picked."""
    cfg.column_width = 27                 # four lists side by side
    app = make_app(cfg)
    shot = {}

    async def run():
        from trellotui.app import BoardScreen
        async with app.run_test(size=(114, 30)) as pilot:
            await pilot.pause(0.8)
            assert isinstance(app.screen, BoardScreen) and app.model.b["name"] == "Alderbrew relaunch"
            await pilot.press("right")
            await pilot.press("down")
            await pilot.pause(0.4)
            shot["svg"] = app.export_screenshot(title="Trello")

    asyncio.run(run())
    out = HERE.parent / "preview.png"
    work = OUT / "preview"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    (work / "shot.svg").write_text(fix_svg(shot["svg"]))
    subprocess.run(["rsvg-convert", "-w", "1200", "-o", out, work / "shot.svg"], check=True)
    shutil.rmtree(work)
    print(out)


def main(cfg, make_app, argv):
    board, card_name = (argv + [BOARD, CARD][len(argv):])[:2]
    app = make_app(cfg)
    film = None

    async def run():
        nonlocal film
        async with app.run_test(size=(COLS, ROWS)) as pilot:
            film = Film(app, pilot)
            await pilot.pause(0.5)
            await story(app, film, board, card_name)

    asyncio.run(run())
    OUT.mkdir(exist_ok=True)
    work = OUT / "frames"
    shutil.rmtree(work, ignore_errors=True)
    out = OUT / "trello-demo.mp4"
    render(film.frames, out, work)
    shutil.rmtree(work)
    total = sum(s for _, s, _ in film.frames)
    print(f"{out}  {len(film.frames)} frames, {total:.1f}s")
