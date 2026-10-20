"""Drive the app headless through a tour of the made-up boards and render it to an MP4
and a GIF (the README's demo.gif).

Every key goes through the real bindings (pilot.press); each step saves an SVG
screenshot with how long it stays on screen and a caption. rsvg-convert draws the
screenshots, Pillow lays out the 1920x1080 frames, ffmpeg makes the video and the GIF.

    trello-demo record          demo/out/trello-demo.{mp4,gif}; cp the GIF to demo.gif
"""

import asyncio
import base64
import hashlib
import html
import io
import re
import shutil
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from rich.cells import cell_len

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
COLS, ROWS = 112, 30                  # all five lists of the board side by side
COLUMN_WIDTH = 22
FONT = "JetBrainsMono Nerd Font"      # what Omarchy's terminals use
EMOJI_FONT = "/usr/share/fonts/noto/NotoColorEmoji.ttf"
NARROW = "Adwaita Mono"               # the terminal's fallback for ✔ ☐ ☑ (rsvg's would be a wide emoji)
TEXT_FONT = "/usr/share/fonts/ttf-ia-writer/iAWriterQuattroS-Bold.ttf"
SOFT_FONT = "/usr/share/fonts/ttf-ia-writer/iAWriterQuattroS-Regular.ttf"
KEY_FONT = "/usr/share/fonts/TTF/JetBrainsMonoNerdFont-Bold.ttf"
W, H = 1920, 1080
BG_TOP, BG_BOTTOM = (22, 24, 36), (12, 13, 20)
ACCENT = (122, 162, 247)
PACE = 1.0              # every hold is this much longer (typing keeps its speed)
LINGER = 0.5            # extra seconds on the last frame of a step, before the next caption
BOARD, CARD = "Alderbrew relaunch", "Homepage hero and photos"
FEATURES = ("Ctrl+P to anything", "history & restore", "My actions", "@ mentions", "offline first")


class Film:
    def __init__(self, app, pilot):
        self.app, self.pilot = app, pilot
        self.frames: list[tuple[str, float, str]] = []
        self.caption = ""

    async def snap(self, seconds: float, caption: str | None = None, settle: float = 0.08, pace=PACE):
        """`caption` is "Keys|what they do" ("|text" for none); None keeps the one before."""
        await self.pilot.pause(settle)
        if caption is not None:
            if caption != self.caption:
                print("·", caption or "-", flush=True)
                if self.frames and self.frames[-1][0]:        # let the step's result sink in
                    svg, held, cap = self.frames[-1]
                    self.frames[-1] = (svg, held + LINGER, cap)
            self.caption = caption
        self.frames.append((self.app.export_screenshot(title="Trello"), seconds * pace, self.caption))

    async def key(self, k: str, seconds=0.3, caption: str | None = None):
        await self.pilot.press(k)
        await self.snap(seconds, caption)

    async def type(self, s: str, caption: str | None = None):
        for ch in s:
            await self.pilot.press({" ": "space", "@": "at", ">": "greater_than_sign"}.get(ch, ch))
            await self.snap(0.06, caption, settle=0.02, pace=1)

    async def until(self, cond, timeout=10.0):
        end = time.time() + timeout
        while not cond():
            if time.time() > end:
                raise TimeoutError("demo step never finished")
            await self.pilot.pause(0.05)


def row_ids(app, rows="#rows"):
    return [o.id for o in app.screen.query_one(rows)._options]


async def goto_row(film: Film, want, step=0.07):
    """Walk the cursor down/up to the first row `want(id)` accepts, a frame per step."""
    rows = film.app.screen.query_one("#rows")
    target = next(i for i, x in enumerate(row_ids(film.app)) if want(x or ""))
    while rows.highlighted != target:
        await film.key("down" if rows.highlighted < target else "up", step)


async def story(app, film: Film):
    from trellotui.app import ActionsScreen, BoardScreen, CardScreen, Editor, HistoryScreen

    bid = next(b["id"] for b in app.store.cached_boards() if b["name"] == BOARD)
    if app.model.id != bid:
        app.open_board(bid)
    await film.until(lambda: app.model.id == bid and isinstance(app.screen, BoardScreen) and app.screen.query("CardList"))
    await film.snap(2.2, "|Your Trello board, right in the terminal")

    # -- the board: walk, move, undo, add, filter
    await film.key("right", 0.2, "← → ↑ ↓|walk lists and cards")
    await film.key("down", 0.2)
    await film.key("down", 0.6)
    await film.key("shift+right", 1.1, "Shift+→|moves the card to the next list")
    await film.key("ctrl+z", 1.3, "Ctrl+Z|undoes it, again and again")
    await film.key("ctrl+n", 0.4, "Ctrl+N|adds a card")
    await film.type("Press kit photos")
    await film.key("enter", 0.4)
    await film.key("escape", 1.2)
    await film.key("ctrl+f", 0.3, "Ctrl+F|filters by any text in the cards")
    await film.type("menu")
    await film.snap(1.6)
    await film.key("escape", 0.3)

    # -- Ctrl+P to the card
    await film.key("ctrl+p", 0.5, "Ctrl+P|jumps to any card on any board")
    await film.type("homepage")
    await film.snap(0.9)
    await film.key("enter", 0.1)
    await film.until(lambda: isinstance(app.screen, CardScreen))
    await film.snap(1.8, "|A card: description, checklists, comments")

    card = app.screen.card
    cls = {cl["name"]: cl for cl in app.model.checklists(card["id"])}
    hero, photos, copy = cls["Hero"], cls["Photos"], cls["Copy"]

    await goto_row(film, lambda x: x == f"cl:{hero['id']}")
    await film.key("left", 0.9, "← →|fold and unfold a checklist")
    await film.key("right", 0.7)
    await film.key("alt+c", 1.5, "Alt+C|hides what's done, only what's left")
    await goto_row(film, lambda x: x.startswith(f"it:{copy['id']}:"))
    await film.key("space", 1.3, "Space|ticks an item")
    await film.key("alt+c", 0.8, "Alt+C|again shows everything")
    await film.key("ctrl+c", 1.4, "Ctrl+C|copies what's under the cursor")

    # -- a comment with a mention
    await film.key("ctrl+r", 0.3, "Ctrl+R|comments, and @ mentions a teammate")
    await film.until(lambda: isinstance(app.screen, Editor))
    await film.type("@cl")
    await film.snap(1.0)
    await film.key("tab", 0.3)
    await film.type("can you crop the taproom one too?")
    await film.snap(0.6)
    await film.key("ctrl+s", 0.2)
    await film.until(lambda: isinstance(app.screen, CardScreen))
    await goto_row(film, lambda x: x.startswith("cm:"), 0.04)
    await film.snap(1.4)

    # -- delete a checklist, get it back
    await goto_row(film, lambda x: x == f"cl:{photos['id']}", 0.04)
    await film.snap(0.6, f"Delete|removes a whole checklist with its {len(photos['checkItems'])} items")
    await film.key("delete", 1.6, "|On trello.com, that's gone for good. Not here:")
    await film.until(lambda: not app.store.inflight)
    await film.key("escape", 0.3, "Alt+R|lists everything deleted on the board")
    await film.key("alt+r", 1.5)
    await film.until(lambda: isinstance(app.screen, HistoryScreen))
    await film.key("enter", 1.8, "Enter|shows it · Enter again restores it, ticks and all")
    await film.key("enter", 0.3)
    await film.until(lambda: not app.store.inflight and not isinstance(app.screen, HistoryScreen), 15)
    await film.until(lambda: not app.syncing, 15)
    await film.pilot.pause(0.4)
    await film.key("ctrl+p", 0.9, "Ctrl+P|remembers the cards you opened")
    await film.key("enter", 0.1)
    await film.until(lambda: isinstance(app.screen, CardScreen))
    back = next(cl for cl in app.model.checklists(card["id"]) if cl["name"] == photos["name"])
    await goto_row(film, lambda x: x == f"cl:{back['id']}", 0.04)
    await film.snap(1.6, f"|“{back['name']}” is back, all {len(back['checkItems'])} items")
    await film.key("alt+h", 2.4, "Alt+H|every change to the card, each one can be put back")
    await film.key("escape", 0.1)
    await film.key("escape", 0.2)

    # -- My actions
    await film.key("alt+a", 1.9, "Alt+A|My actions: your to-dos from every board")
    await film.until(lambda: isinstance(app.screen, ActionsScreen))
    await goto_row(film, lambda x: x.startswith("i:"), 0.1)
    await film.key("space", 1.5, "Space|ticks it off, the next one moves up")
    await film.key("escape", 0.2)

    # -- commands
    await film.key("ctrl+p", 0.2, "Ctrl+P  >|runs any command")
    await film.type(">")
    await film.snap(1.8)
    await film.key("escape", 0.3)
    film.frames.append(("", 3.5, ""))


# ------------------------------------------------------------ rendering

@lru_cache(64)
def emoji_png(glyph: str) -> str:
    """A color emoji as a data: URI (rsvg would draw it in one flat color)."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(EMOJI_FONT, 109)            # the one size the bitmap font has
    im = Image.new("RGBA", (160, 160))
    ImageDraw.Draw(im).text((8, 8), glyph, font=font, embedded_color=True)
    im = im.crop(im.getbbox() or (0, 0, 1, 1))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def fix_svg(svg: str) -> str:
    """Make rsvg draw Textual's SVG the way a terminal does: every character in its own cell
    (rsvg stretches runs unevenly and drops nbsp), emoji in color."""
    svg = re.sub(r'font-family:\s*"?Fira Code"?(, monospace)?', f'font-family: "{FONT}", monospace', svg)
    svg = svg.replace("font-family: arial", f'font-family: "{FONT}"')

    def run(m):
        attrs, chars = m.group(1), html.unescape(m.group(2))
        if not chars:
            return m.group(0)
        cw = float(re.search(r'textLength="([\d.]+)"', attrs).group(1)) / max(1, cell_len(chars))
        x = float(re.search(r' x="([\d.]+)"', attrs).group(1))
        y = float(re.search(r' y="([\d.]+)"', attrs).group(1))
        clip = re.search(r'clip-path="[^"]*"', attrs)
        base = re.sub(r' (x|y|textLength)="[^"]*"', "", attrs)
        out, i = [], 0
        while i < len(chars):
            g = chars[i]
            i += 1
            while i < len(chars) and (chars[i] in "\ufe0f\u200d" or chars[i - 1] == "\u200d"):
                g += chars[i]
                i += 1
            w = cell_len(g)
            if ord(g[0]) >= 0x2300 and (w == 2 or "️" in g):
                size = min(w * cw, 21)
                out.append(f'<image x="{x + (w * cw - size) / 2:.1f}" y="{y - 17.5:.1f}" width="{size:.1f}" '
                           f'height="{size:.1f}" {clip.group(0) if clip else ""} href="{emoji_png(g)}"/>')
            elif g in "✔☐☑":                                 # rsvg's would be a wide emoji
                out.append(f'<text{base} x="{x:.1f}" y="{y:.1f}" style="font-family: {NARROW}">{g}</text>')
            elif g.strip("\xa0 "):
                out.append(f'<text{base} x="{x:.1f}" y="{y:.1f}">{html.escape(g, quote=False)}</text>')
            x += w * cw
        return "".join(out)
    return re.sub(r"<text([^>]*textLength[^>]*)>([^<]*)</text>", run, svg)


def render(frames, out: Path, work: Path, fps=30):
    """SVG screenshots -> designed 1920x1080 PNGs -> one image per video frame -> MP4."""
    work.mkdir(parents=True, exist_ok=True)
    pngs: dict[str, Path] = {}
    seq = work / "seq"
    seq.mkdir()
    total = sum(s for _, s, _ in frames)
    n, t = 0, 0.0
    for svg, seconds, caption in frames:
        done = t / total
        key = hashlib.sha1(f"{svg}{caption}{done:.3f}".encode()).hexdigest()[:16]
        if key not in pngs:
            pngs[key] = frame(work / key, caption, fix_svg(svg), done) if svg else end_card(work / key)
        t += seconds
        while n < round(t * fps):                    # cumulative, so short frames never drift
            (seq / f"{n:05d}.png").hardlink_to(pngs[key])
            n += 1
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", seq / "%05d.png",
                    "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                    "-movflags", "+faststart", out], check=True)


@lru_cache(None)
def font(path: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(path, size)


@lru_cache(1)
def backdrop():
    """A dark vertical gradient with a soft glow of the accent behind the window."""
    from PIL import Image, ImageDraw, ImageFilter
    bg = Image.new("RGB", (W, H))
    px = ImageDraw.Draw(bg)
    for y in range(H):
        f = y / (H - 1)
        px.line([(0, y), (W, y)], fill=tuple(round(a + (b - a) * f) for a, b in zip(BG_TOP, BG_BOTTOM)))
    glow = Image.new("L", (W, H))
    ImageDraw.Draw(glow).ellipse((W * 0.18, H * 0.05, W * 0.82, H * 0.8), fill=38)
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    return Image.composite(Image.new("RGB", (W, H), ACCENT), bg, glow)


def shot_png(base: Path, svg: str, width: int, height: int):
    from PIL import Image
    src, png = base.with_suffix(".svg"), base.with_name(base.name + "-shot.png")
    src.write_text(svg)
    subprocess.run(["rsvg-convert", "-w", str(width), "-h", str(height), "--keep-aspect-ratio", "-o", png, src],
                   check=True)
    return Image.open(png).convert("RGBA")


def with_shadow(im, shot, x: int, y: int):
    """Paste the window at (x, y) on `im` over a soft drop shadow."""
    from PIL import Image, ImageFilter
    pad = 60
    sh = Image.new("RGBA", (shot.width + 2 * pad, shot.height + 2 * pad), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 150), (pad, pad), shot.split()[3])
    sh = sh.filter(ImageFilter.GaussianBlur(26))
    im.alpha_composite(sh, (x - pad, y - pad + 18))
    im.alpha_composite(shot, (x, y))


def chips(draw, keys: list[str], x: float, cy: float, measure=False) -> float:
    """Keys drawn as keycaps, left to right from x, centred on cy; returns the width."""
    f = font(KEY_FONT, 34)
    start = x
    for k in keys:
        tw = draw.textlength(k, font=f)
        w, h = tw + 36, 58
        if not measure:
            draw.rounded_rectangle((x, cy - h / 2 + 4, x + w, cy + h / 2 + 4), 12, fill=(8, 9, 14))
            draw.rounded_rectangle((x, cy - h / 2, x + w, cy + h / 2), 12, fill=(44, 49, 66),
                                   outline=(78, 86, 112), width=2)
            draw.text((x + 18, cy), k, font=f, fill=(232, 236, 248), anchor="lm")
        x += w + 12
    return x - start - (12 if keys else 0)


def frame(base: Path, caption: str, svg: str, done: float) -> Path:
    """The window in the middle, the step's keys and what they do under it, a progress line."""
    from PIL import Image, ImageDraw
    out = base.with_suffix(".png")
    im = backdrop().convert("RGBA")
    shot = shot_png(base, svg, W - 160, H - 200)        # 160 px under it for the caption
    with_shadow(im, shot, (W - shot.width) // 2, 34)
    d = ImageDraw.Draw(im)
    keys, _, text = caption.rpartition("|")
    keys = [k for k in keys.split("  ") if k]
    if text:
        f = font(TEXT_FONT, 46)
        gap = 26 if keys else 0
        width = chips(d, keys, 0, 0, measure=True) + gap + d.textlength(text, font=f)
        x, cy = (W - width) / 2, H - 84
        x += chips(d, keys, x, cy) + gap
        d.text((x, cy), text, font=f, fill=(240, 242, 250), anchor="lm")
    d.rectangle((0, H - 6, W, H), fill=(30, 33, 46))
    d.rectangle((0, H - 6, round(W * done), H), fill=ACCENT)
    im.convert("RGB").save(out)
    return out


def end_card(base: Path) -> Path:
    from PIL import ImageDraw
    out = base.with_suffix(".png")
    im = backdrop().convert("RGBA")
    d = ImageDraw.Draw(im)
    d.text((W / 2, H / 2 - 110), "Trello for Omarchy", font=font(TEXT_FONT, 104), fill="white", anchor="mm")
    d.text((W / 2, H / 2 + 5), "Fast in the terminal, and nothing is ever lost",
           font=font(SOFT_FONT, 48), fill=(184, 188, 204), anchor="mm")
    f = font(KEY_FONT, 30)
    widths = [d.textlength(t, font=f) + 44 for t in FEATURES]
    x, y = (W - sum(widths) - 16 * (len(widths) - 1)) / 2, H / 2 + 110
    for t, w in zip(FEATURES, widths):
        d.rounded_rectangle((x, y - 28, x + w, y + 28), 28, outline=ACCENT, width=2)
        d.text((x + w / 2, y), t, font=f, fill=(200, 214, 250), anchor="mm")
        x += w + 16
    d.rectangle((0, H - 6, W, H), fill=ACCENT)
    im.convert("RGB").save(out)
    return out


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
    cfg.column_width = COLUMN_WIDTH
    app = make_app(cfg)
    app.clip = lambda text: None          # Ctrl+C in the tour: leave the real clipboard alone
    film = None

    async def run():
        nonlocal film
        async with app.run_test(size=(COLS, ROWS)) as pilot:
            film = Film(app, pilot)
            await pilot.pause(0.5)
            await story(app, film)

    asyncio.run(run())
    OUT.mkdir(exist_ok=True)
    work = OUT / "frames"
    shutil.rmtree(work, ignore_errors=True)
    out = OUT / "trello-demo.mp4"
    render(tuple(film.frames), out, work)
    shutil.rmtree(work)
    gif = out.with_suffix(".gif")              # the README's: held frames dropped, 128 colours
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", out, "-vf",
                    "fps=12,scale=1200:-1:flags=lanczos,mpdecimate,split[a][b];"
                    "[a]palettegen=max_colors=160:stats_mode=diff[p];[b][p]paletteuse=dither=none",
                    "-fps_mode", "vfr", gif], check=True)
    total = sum(s for _, s, _ in film.frames)
    print(f"{out}  {len(film.frames)} frames, {total:.1f}s\n{gif}")
