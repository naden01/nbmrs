#!/usr/bin/env python3
"""Rendering backend for the server-status bot.

Two entry points are used by ``bot.py``:

* :func:`render_dashboard` -- builds a dark "cockpit" style dashboard PNG
  from a snapshot dict plus the sampled history; used by ``/servstats``.

* :func:`fastfetch_png` -- renders ANSI-coloured terminal output (e.g. the
  output of ``fastfetch``) to an image by emulating a terminal grid with
  :mod:`pyte` and painting every glyph with Pillow; used by ``/fastfetch``.

Both functions return PNG image bytes ready to be sent as a Telegram photo.
They are defensive and never raise on missing/blank input, so the bot's
``except`` fallback to text output is only ever a last resort.
"""
from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont
import pyte

# --------------------------------------------------------------------------- #
# Palette (RGB tuples)
# --------------------------------------------------------------------------- #
BG     = (18, 20, 28)
CARD   = (28, 31, 43)
CARD2  = (36, 40, 55)
TRACK  = (52, 57, 76)
TEXT   = (233, 236, 243)
MUTED  = (150, 158, 181)
ACCENT = (130, 190, 255)
GREEN  = (86, 214, 132)
YELLOW = (242, 200, 92)
RED    = (240, 92, 92)
CYAN   = (96, 208, 220)
PURPLE = (186, 150, 245)

_DEF_FG  = (205, 214, 244)   # default terminal foreground
_TERM_BG = (17, 18, 24)      # terminal backdrop

# pyte stores the 16 ANSI colours as *names*; map them to RGB here.
_NAMED = {
    "default": None,
    "black": (0x00, 0x00, 0x00), "brightblack": (0x7f, 0x7f, 0x7f),
    "red": (0xcd, 0x00, 0x00), "brightred": (0xff, 0x00, 0x00),
    "green": (0x00, 0xcd, 0x00), "brightgreen": (0x00, 0xff, 0x00),
    "brown": (0xcd, 0xcd, 0x00), "brightbrown": (0xff, 0xff, 0x00),
    "blue": (0x00, 0x00, 0xee), "brightblue": (0x5c, 0x5c, 0xff),
    "magenta": (0xcd, 0x00, 0xcd), "brightmagenta": (0xff, 0x00, 0xff),
    "cyan": (0x00, 0xcd, 0xcd), "brightcyan": (0x00, 0xff, 0xff),
    "white": (0xe5, 0xe5, 0xe5), "brightwhite": (0xff, 0xff, 0xff),
}

# --------------------------------------------------------------------------- #
# Fonts (monospace). DejaVu is installed by the README's `fonts-dejavu-core`,
# but we fall back through a chain of well-known monospace faces per platform
# so the terminal grid never drifts apart on an unusual host.
# --------------------------------------------------------------------------- #
_FONT_DIRS = (
    "/usr/share/fonts/truetype/dejavu",  # Debian/Ubuntu/Arch
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    "/usr/share/fonts",
    "/Library/Fonts", "/System/Library/Fonts",  # macOS
    os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts"),  # Windows
    "",
)
_FONT_FILES = {
    False: ("DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "NotoSansMono-Regular.ttf",
            "consola.ttf", "cour.ttf", "Menlo.ttc", "Courier New.ttf"),
    True:  ("DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf", "consolab.ttf",
            "courbd.ttf", "Menlo.ttc", "DejaVuSansMono.ttf"),
}
_font_cache: dict = {}


def _load_font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    font = None
    for directory in _FONT_DIRS:
        for name in _FONT_FILES[bold]:
            path = os.path.join(directory, name) if directory else name
            try:
                if os.path.exists(path):
                    font = ImageFont.truetype(path, size)
                    break
            except Exception:
                font = None
        if font:
            break
    if font is None:
        # PIL's built-in default; sized via load_default(size) when supported.
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _font_cell_width(font) -> int:
    """Monospace advance width for a font, tolerant of proportional fallbacks.

    Measures a few representative glyphs and uses the widest so a slightly
    non-monospace fallback still lays the terminal grid out without overlap.
    """
    widths = []
    for ch in ("0", "M", "W", "i", "@"):
        try:
            widths.append(font.getlength(ch))
        except Exception:
            pass
    return max(1, int(round(max(widths)))) if widths else 1


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _to_rgb(value, default):
    """Resolve a pyte colour value to an ``(r, g, b)`` tuple (or ``default``)."""
    if value is None:
        return default
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        return (int(value[0]), int(value[1]), int(value[2]))
    name = str(value).strip().lower()
    if name in _NAMED:
        return _NAMED[name] if _NAMED[name] is not None else default
    hexs = name.lstrip("#")
    if len(hexs) == 6:
        try:
            return (int(hexs[0:2], 16), int(hexs[2:4], 16), int(hexs[4:6], 16))
        except ValueError:
            return default
    return default


def _blend(a, b, t: float):
    """Blend colour ``b`` into ``a`` by fraction ``t`` (avoids alpha compositing)."""
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _fmt_uptime(seconds) -> str:
    seconds = int(seconds or 0)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def _sev(pct: float):
    pct = pct or 0
    return GREEN if pct < 60 else YELLOW if pct < 85 else RED



# --------------------------------------------------------------------------- #
# /fastfetch: emulated terminal -> image
# --------------------------------------------------------------------------- #
def fastfetch_png(raw, title: str = "") -> bytes:
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")

    cols, lines = 110, 120
    screen = pyte.Screen(cols, lines)
    pyte.Stream(screen).feed(text)

    # Determine the used rectangle. fastfetch colour blocks are *spaces* with a
    # background colour, so a cell counts as "used" if it has a glyph OR a bg.
    used_rows, used_cols = 1, 1
    for y in range(lines):
        row = screen.buffer[y]
        last, row_has = 0, False
        for x in range(cols):
            ch = row[x]
            if (ch.data and ch.data.strip()) or ch.bg not in ("default", None, ""):
                row_has = True
                last = x + 1
        if row_has:
            used_rows = y + 1
            used_cols = max(used_cols, last)

    size = 15
    font = _load_font(size)
    cw = _font_cell_width(font)
    ascent, descent = font.getmetrics()
    cell_h = ascent + descent + 3

    pad = 18
    title_h = 44 if title else 8
    width = pad * 2 + used_cols * cw
    height = title_h + pad + used_rows * cell_h + pad

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    if title:
        draw.text((pad, 12), str(title), font=_load_font(20), fill=TEXT)
        draw.line([(pad, title_h - 6), (width - pad, title_h - 6)], fill=TRACK, width=1)

    ox = pad
    oy = title_h + pad // 2
    draw.rounded_rectangle(
        [ox - 8, oy - 8, ox + used_cols * cw + 8, oy + used_rows * cell_h + 6],
        radius=10, fill=_TERM_BG,
    )

    for y in range(used_rows):
        row = screen.buffer[y]
        cy = oy + y * cell_h
        for x in range(used_cols):
            ch = row[x]
            data = ch.data or " "
            if data in ("\x00", ""):
                data = " "
            fg = _to_rgb(ch.fg, _DEF_FG)
            bg = _to_rgb(ch.bg, None)
            if ch.reverse:
                fg, bg = (bg or _TERM_BG), fg
            if ch.bold and bg is None:
                fg = tuple(min(255, c + 40) for c in fg)
            cx = ox + x * cw
            if bg is not None:
                draw.rectangle([cx, cy, cx + cw, cy + cell_h], fill=bg)
            if data.strip():
                draw.text((cx, cy), data, font=font, fill=fg)

    return _png(img)


# --------------------------------------------------------------------------- #
# /servstats: dashboard widgets
# --------------------------------------------------------------------------- #
def _section(draw, x0, y, x1, title, subtitle=""):
    draw.text((x0 + 2, y), title.upper(), font=_load_font(15), fill=TEXT)
    if subtitle:
        sub_font = _load_font(12)
        draw.text((x1 - 2 - sub_font.getlength(subtitle), y + 2), subtitle,
                  font=sub_font, fill=MUTED)


def _donut(draw, box, label, pct, sub):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle([x0, y0, x1, y1], radius=12, fill=CARD)
    cx, cy, r, w = (x0 + x1) // 2, y0 + 46, 28, 9
    bbox = [cx - r, cy - r, cx + r, cy + r]
    draw.arc(bbox, 0, 360, fill=TRACK, width=w)
    draw.arc(bbox, -90, -90 + 360 * max(0.0, min(100.0, pct)) / 100, fill=_sev(pct), width=w)
    pct_text = f"{pct:.0f}%"
    pct_font = _load_font(20)
    draw.text((cx - pct_font.getlength(pct_text) / 2, cy - 11), pct_text, font=pct_font, fill=TEXT)
    lab_font = _load_font(15)
    draw.text((cx - lab_font.getlength(label) / 2, y0 + 84), label, font=lab_font, fill=TEXT)
    sub_font = _load_font(12)
    draw.text((cx - sub_font.getlength(sub) / 2, y0 + 100), sub, font=sub_font, fill=MUTED)


def _sparkline(draw, box, series, color, vmax=None, baseline_label=""):
    x0, y0, x1, y1 = box
    series = [float(v) for v in series if v is not None]
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=8, fill=CARD2)
    if not series:
        draw.text((x0 + 8, (y0 + y1) // 2 - 6), "no data", font=_load_font(13), fill=MUTED)
        return
    top = max(vmax if vmax else (max(series) or 1), 1e-9)
    n = len(series)
    step = w / max(1, n - 1) if n > 1 else 0
    pts = [
        (x0 + (i * step if n > 1 else w / 2),
         y1 - (max(0.0, min(series[i], top)) / top) * (h - 8) - 4)
        for i in range(n)
    ]
    if baseline_label:
        draw.text((x0 + 8, y0 + 4), baseline_label, font=_load_font(12), fill=MUTED)
    if n > 1:
        draw.polygon([(pts[0][0], y1), *pts, (pts[-1][0], y1)], fill=_blend(CARD2, color, 0.25))
        draw.line(pts, fill=color, width=2, joint="curve")
    lx, ly = pts[-1]
    draw.ellipse([lx - 3, ly - 3, lx + 3, ly + 3], fill=color)


def render_dashboard(snap, history, sample_interval: int = 5, history_points: int = 72) -> bytes:
    snap = snap or {}
    history = list(history or [])

    width, height = 1240, 1750
    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    pad = 20
    y = pad

    # ---- header ----
    draw.text((pad, y), str(snap.get("host", "")), font=_load_font(30), fill=TEXT)
    draw.text((pad, y + 40), f"{snap.get('os', '')}   ·   kernel {snap.get('kernel', '')}",
              font=_load_font(16), fill=MUTED)
    right = str(snap.get("time", ""))
    right_font = _load_font(15)
    draw.text((width - pad - right_font.getlength(right), y + 4), right, font=right_font, fill=MUTED)
    up_txt = f"up {_fmt_uptime(snap.get('uptime', 0))}"
    up_font = _load_font(17)
    draw.text((width - pad - up_font.getlength(up_txt), y + 26), up_txt, font=up_font, fill=ACCENT)
    y += 70
    draw.line([(pad, y), (width - pad, y)], fill=TRACK, width=1)
    y += 16

    # ---- metric donuts ----
    mem = snap.get("mem", (0, 0, 0)) or (0, 0, 0)
    swp = snap.get("swap", (0, 0, 0)) or (0, 0, 0)
    dsk = snap.get("disk", (0, 0, 0)) or (0, 0, 0)
    gap = 14
    card_w = (width - pad * 2 - gap * 3) // 4
    card_h = 116
    metrics = [
        ("CPU", snap.get("cpu", 0), f"{snap.get('threads', 0)} threads"),
        ("RAM", mem[2], f"{_human(mem[0])} / {_human(mem[1])}"),
        ("SWAP", swp[2], f"{_human(swp[0])} / {_human(swp[1])}"),
        ("DISK", dsk[2], f"{snap.get('disk_path', '/')}  {_human(dsk[0])} / {_human(dsk[1])}"),
    ]
    for i, (label, value, sub) in enumerate(metrics):
        x0 = pad + i * (card_w + gap)
        _donut(draw, [x0, y, x0 + card_w, y + card_h], label, float(value or 0), sub)
    y += card_h + 16

    # ---- history sparklines ----
    _section(draw, pad, y, width - pad, "History",
             f"last {min(len(history), history_points) or 0} samples · {sample_interval}s apart")
    y += 30
    gh = 92
    cpu_hist = [h.get("cpu") for h in history][-history_points:]
    mem_hist = [h.get("mem") for h in history][-history_points:]
    net_hist = [(h.get("rx") or 0) + (h.get("tx") or 0) for h in history][-history_points:]
    _sparkline(draw, [pad, y, width - pad, y + gh], cpu_hist, ACCENT,
               vmax=100, baseline_label=f"CPU   peak {max(cpu_hist):.0f}%" if cpu_hist else "CPU")
    y += gh + 10
    _sparkline(draw, [pad, y, width - pad, y + gh], mem_hist, PURPLE,
               vmax=100, baseline_label=f"MEM   peak {max(mem_hist):.0f}%" if mem_hist else "MEM")
    y += gh + 10
    _sparkline(draw, [pad, y, width - pad, y + gh], net_hist, CYAN,
               vmax=max(net_hist + [1]),
               baseline_label=f"NET   peak {_human(max(net_hist + [0]))}/s" if net_hist else "NET")
    y += gh + 18

    # ---- CPU cores ----
    cores = [float(c) for c in (snap.get("cores") or [])]
    if cores:
        _section(draw, pad, y, width - pad, "CPU cores", f"{len(cores)} cores")
        y += 30
        ch = 150
        draw.rounded_rectangle([pad, y, width - pad, y + ch], radius=10, fill=CARD)
        per_row = 8
        bar_gap = 10
        bar_w = (width - pad * 2 - 20 - (per_row - 1) * bar_gap) / per_row
        base_y, max_h = y + ch - 26, 90
        lab_font = _load_font(11)
        for i, val in enumerate(cores):
            bx = pad + 10 + (i % per_row) * (bar_w + bar_gap)
            hgt = max(2, max(0.0, min(100.0, val)) / 100 * max_h)
            draw.rounded_rectangle([bx, base_y - hgt, bx + bar_w, base_y], radius=3, fill=_sev(val))
            lab = f"{val:.0f}"
            draw.text((bx + bar_w / 2 - lab_font.getlength(lab) / 2, base_y - hgt - 14),
                      lab, font=lab_font, fill=MUTED)
        y += ch + 18

    # ---- top processes ----
    procs = snap.get("procs") or []
    if procs:
        _section(draw, pad, y, width - pad, "Top processes", "by CPU")
        y += 30
        row_h, head_h = 28, 30
        ph = head_h + row_h * len(procs) + 10
        draw.rounded_rectangle([pad, y, width - pad, y + ph], radius=10, fill=CARD)
        c_name = pad + 16
        c_cpu = width - pad - 140
        c_mem = width - pad - 16
        head_font = _load_font(14)
        draw.text((c_name, y + 10), "PROCESS", font=head_font, fill=MUTED)
        draw.text((c_cpu, y + 10), "CPU", font=head_font, fill=MUTED, anchor="ra")
        draw.text((c_mem, y + 10), "MEM", font=head_font, fill=MUTED, anchor="ra")
        y += head_h + 6
        body_font = _load_font(15)
        for name, cpu, mem_v in procs:
            draw.text((c_name, y), str(name)[:24], font=body_font, fill=TEXT)
            draw.text((c_cpu, y), f"{cpu:5.1f}%", font=body_font, fill=_sev(cpu), anchor="ra")
            draw.text((c_mem, y), f"{mem_v:5.1f}%", font=body_font, fill=MUTED, anchor="ra")
            y += row_h
        y += 8

    # ---- footer stats ----
    y += 8
    load = snap.get("load", (0, 0, 0)) or (0, 0, 0)
    temp, freq = snap.get("temp"), snap.get("freq")
    rx, tx = snap.get("rx", 0) or 0, snap.get("tx", 0) or 0
    bits = [
        ("Load", f"{load[0]:.2f}  {load[1]:.2f}  {load[2]:.2f}"),
        ("Temp", f"{temp:.0f} °C" if temp is not None else "—"),
        ("Freq", f"{freq:.0f} MHz" if freq else "—"),
        ("Net", f"↓ {_human(rx)}/s   ↑ {_human(tx)}/s"),
        ("Procs", str(snap.get("nproc", 0))),
    ]
    fh = 70
    draw.rounded_rectangle([pad, y, width - pad, y + fh], radius=10, fill=CARD2)
    seg = (width - pad * 2) / len(bits)
    for i, (key, val) in enumerate(bits):
        sx = pad + i * seg
        draw.text((sx + 16, y + 14), key.upper(), font=_load_font(12), fill=MUTED)
        draw.text((sx + 16, y + 32), str(val), font=_load_font(16), fill=TEXT)
    y += fh + pad

    return _png(img.crop((0, 0, width, min(height, y))))


if __name__ == "__main__":  # python render.py  -> writes sample PNGs for a visual check
    sample_snap = {
        "host": "web-01", "os": "Debian GNU/Linux 12 (bookworm)", "kernel": "6.1.0-18-amd64",
        "time": "14:23:07 UTC", "uptime": 273660, "load": (1.20, 0.95, 0.80),
        "cpu": 37.5, "cores": [40, 55, 30, 20, 48, 60, 25, 35], "threads": 8, "freq": 2600.0,
        "nproc": 214, "mem": (5 * 1024**3, 16 * 1024**3, 31.2),
        "swap": (128 * 1024**2, 4 * 1024**3, 3.1), "disk": (42 * 1024**3, 100 * 1024**3, 42.0),
        "disk_path": "/", "temp": 55.0, "rx": 1_250_000, "tx": 320_000,
        "procs": [("postgres", 18.2, 22.4), ("python3", 9.1, 5.6), ("node", 4.4, 3.2),
                  ("dockerd", 2.0, 2.9), ("sshd", 0.5, 0.4)],
    }
    sample_hist = [
        {"t": 0, "cpu": 20 + (i * 3) % 60, "mem": 28 + (i % 6),
         "rx": 800_000 + (i * 50_000) % 900_000, "tx": 200_000 + (i * 20_000) % 300_000}
        for i in range(72)
    ]
    with open("dashboard.png", "wb") as fh:
        fh.write(render_dashboard(sample_snap, sample_hist, 5, 72))
    ff = (
        "\x1b[1;36mnaze@web-01\x1b[0m\n"
        "\x1b[1;34mOS:\x1b[0m Debian GNU/Linux 12 (bookworm)\n"
        "\x1b[1;35mKernel:\x1b[0m 6.1.0-18-amd64\n"
        "\x1b[1;33mUptime:\x1b[0m 3 days, 4 hours, 3 mins\n"
        "\x1b[1;32mMemory:\x1b[0m 5.1 GiB / 16 GiB (31%)\nColors: "
        "\x1b[40m   \x1b[41m   \x1b[42m   \x1b[43m   \x1b[44m   \x1b[45m   \x1b[46m   \x1b[47m   \x1b[0m\n"
    )
    with open("fastfetch.png", "wb") as fh:
        fh.write(fastfetch_png(ff.encode(), "fastfetch — web-01"))
    print("wrote dashboard.png and fastfetch.png")
