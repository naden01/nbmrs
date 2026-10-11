#!/usr/bin/env python3
"""Telegram server-status bot.

Commands:
  /fastfetch  - run fastfetch on the server
  /servstats  - live CPU / RAM / disk / network snapshot
  /uptime     - uptime, boot time, load average
  /disk       - all mounted disks
  /top        - top processes by CPU and RAM
  /net        - live network speed + totals
  /docker     - running containers
  /failed     - failed systemd units
  /alerts     - toggle threshold alerts (CPU/RAM/disk)
"""
import asyncio
import functools
import html
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta

import io

import psutil
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

try:  # optional: only needed for the colored /fastfetch image
    import pyte
    from PIL import Image, ImageDraw, ImageFont

    RENDER_OK = True
except ImportError:
    RENDER_OK = False

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("servbot")

TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
CPU_LIMIT = float(os.getenv("ALERT_CPU", "90"))
MEM_LIMIT = float(os.getenv("ALERT_MEM", "90"))
DISK_LIMIT = float(os.getenv("ALERT_DISK", "90"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL", "60"))  # seconds between checks
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "1800"))  # seconds between repeat alerts

# /fastfetch: "image" renders the real colored output (logo + color blocks) as a PNG,
# "text" sends plain monospace text. Image mode falls back to text on any error.
FASTFETCH_MODE = os.getenv("FASTFETCH_MODE", "image").lower()
# Shell/Terminal are left out on purpose: under a bot they would report "bot.py" / "sshd".
# Set FASTFETCH_STRUCTURE= (empty) to use your own fastfetch config instead.
FASTFETCH_STRUCTURE = os.getenv(
    "FASTFETCH_STRUCTURE",
    "Title:Separator:OS:Host:Kernel:Uptime:Packages:Display:CPU:GPU:Memory:Swap:Disk:LocalIP:Locale:Break:Colors",
)
FONT_PATH = os.getenv("FONT_PATH", "")

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
alerts_enabled = True
last_alert: dict[str, float] = {}


# ---------- helpers ----------
def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def bar(pct: float, width: int = 10) -> str:
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def pre(text: str) -> str:
    text = ANSI.sub("", text).strip() or "(no output)"
    if len(text) > 3900:
        text = text[:3900] + "\n… truncated"
    return f"<pre>{html.escape(text)}</pre>"


async def run_bytes(*cmd: str, timeout: int = 20, env: dict | None = None) -> bytes:
    """Run a command (no shell) and return raw combined output."""
    if not shutil.which(cmd[0]):
        return f"'{cmd[0]}' is not installed on this server.".encode()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"Timed out after {timeout}s.".encode()
    return out


async def run(*cmd: str, timeout: int = 20) -> str:
    """Run a command (no shell) and return combined output as text."""
    return (await run_bytes(*cmd, timeout=timeout)).decode(errors="replace")


# ---------- ANSI -> PNG renderer (terminal emulation via pyte) ----------
BG_DEFAULT = (16, 16, 22)
FG_DEFAULT = (208, 208, 208)
PALETTE = {
    "black": (12, 12, 12), "red": (205, 49, 49), "green": (13, 188, 121), "brown": (229, 229, 16),
    "blue": (36, 114, 200), "magenta": (188, 63, 188), "cyan": (17, 168, 205), "white": (229, 229, 229),
    "brightblack": (102, 102, 102), "brightred": (241, 76, 76), "brightgreen": (35, 209, 139),
    "brightbrown": (245, 245, 67), "brightblue": (59, 142, 234), "brightmagenta": (214, 112, 214),
    "brightcyan": (41, 184, 219), "brightwhite": (255, 255, 255),
    "bfightmagenta": (214, 112, 214),  # sic: typo in pyte's background color table
}


def _rgb(name: str, default):
    if name == "default":
        return default
    if name in PALETTE:
        return PALETTE[name]
    if len(name) == 6:  # pyte gives 256-color / truecolor as a hex string
        try:
            return tuple(int(name[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return default


def _load_fonts(size: int):
    candidates = [
        FONT_PATH,
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            regular = ImageFont.truetype(path, size)
            bold_path = path.replace("Mono.ttf", "Mono-Bold.ttf").replace("Regular", "Bold")
            bold = ImageFont.truetype(bold_path, size) if os.path.exists(bold_path) else regular
            return regular, bold
    raise RuntimeError("No monospace TTF font found (install fonts-dejavu-core or set FONT_PATH)")


def render_ansi(raw: bytes, cols: int = 110, rows: int = 100, size: int = 18) -> bytes:
    """Emulate a terminal, feed it fastfetch's output, and draw the screen as a PNG."""
    screen = pyte.Screen(cols, rows)
    screen.set_mode(pyte.modes.LNM)  # treat \n as CRLF like a real tty
    pyte.ByteStream(screen).feed(raw)

    last_row = last_col = -1
    for y in range(rows):
        line = screen.buffer[y]
        for x in range(cols):
            ch = line[x]
            if ch.data.strip() or ch.bg != "default":
                last_row, last_col = max(last_row, y), max(last_col, x)
    if last_row < 0:
        raise RuntimeError("fastfetch produced no output")

    font, bold = _load_fonts(size)
    cw = font.getlength("M")
    ascent, descent = font.getmetrics()
    lh = ascent + descent + 2
    pad = 18
    img = Image.new(
        "RGB", (int(pad * 2 + cw * (last_col + 1)) + 1, pad * 2 + lh * (last_row + 1)), BG_DEFAULT
    )
    draw = ImageDraw.Draw(img)
    for y in range(last_row + 1):
        for x in range(last_col + 1):
            ch = screen.buffer[y][x]
            fg, bg = _rgb(ch.fg, FG_DEFAULT), _rgb(ch.bg, None)
            if ch.reverse:
                fg, bg = (bg or BG_DEFAULT), fg
            x0, x1 = round(pad + x * cw), round(pad + (x + 1) * cw) - 1
            y0 = pad + y * lh
            if bg:
                draw.rectangle([x0, y0, x1, y0 + lh - 1], fill=bg)
            if ch.data.strip():
                draw.text((x0, y0 + 1), ch.data, font=bold if ch.bold else font, fill=fg)

    if img.width > 1280:  # keep within Telegram's photo size sweet spot
        img = img.resize((1280, round(img.height * 1280 / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def authorized(func):
    @functools.wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ALLOWED:
            log.warning("Unauthorized access from %s", user.id if user else "unknown")
            if update.message:
                await update.message.reply_text("⛔ Not authorized.")
            return
        return await func(update, ctx)

    return wrapper


async def reply(update: Update, text: str):
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------- commands ----------
@authorized
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, "🖥 <b>Server bot ready</b>\n\n" + html.escape(__doc__.split("Commands:")[1].strip("\n")).join(["<pre>", "</pre>"]))


@authorized
async def fastfetch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not shutil.which("fastfetch"):
        await reply(update, "fastfetch is not installed on this server.")
        return
    if FASTFETCH_MODE == "image" and RENDER_OK:
        try:
            env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
            env.update(TERM="xterm-256color", COLUMNS="110", LINES="100")
            args = ["--structure", FASTFETCH_STRUCTURE] if FASTFETCH_STRUCTURE else []
            raw = await run_bytes("fastfetch", "--pipe", "false", *args, env=env)
            png = await asyncio.to_thread(render_ansi, raw)
            await update.message.reply_photo(photo=png)
            return
        except Exception:
            log.exception("fastfetch image render failed, falling back to text")
    structure = FASTFETCH_STRUCTURE.replace(":Break", "").replace(":Colors", "")
    args = ["--structure", structure] if structure else []
    out = await run("fastfetch", "--logo", "none", "--pipe", *args)
    await reply(update, pre(out))


def _servstats() -> str:
    cpu = psutil.cpu_percent(interval=1)
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    du = psutil.disk_usage("/")
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    up = timedelta(seconds=int(time.time() - psutil.boot_time()))
    net = psutil.net_io_counters()
    lines = [
        f"CPU   {bar(cpu)} {cpu:5.1f}%  ({psutil.cpu_count()} cores)",
        f"RAM   {bar(vm.percent)} {vm.percent:5.1f}%  {human(vm.used)}/{human(vm.total)}",
        f"Swap  {bar(sw.percent)} {sw.percent:5.1f}%  {human(sw.used)}/{human(sw.total)}",
        f"Disk  {bar(du.percent)} {du.percent:5.1f}%  {human(du.used)}/{human(du.total)}",
        f"Load  {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}",
        f"Up    {up}",
        f"Procs {len(psutil.pids())}",
        f"Net   ↑ {human(net.bytes_sent)}  ↓ {human(net.bytes_recv)} (since boot)",
    ]
    try:
        temps = psutil.sensors_temperatures()
        if temps:
            first = next(iter(temps.values()))[0]
            lines.append(f"Temp  {first.current:.0f}°C ({first.label or 'sensor'})")
    except Exception:
        pass
    return "\n".join(lines)


@authorized
async def servstats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = await asyncio.to_thread(_servstats)
    await reply(update, f"📊 <b>Server stats</b> — {datetime.now():%H:%M:%S}\n" + pre(text))


@authorized
async def uptime(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    boot = datetime.fromtimestamp(psutil.boot_time())
    up = timedelta(seconds=int(time.time() - psutil.boot_time()))
    load = os.getloadavg()
    await reply(update, pre(f"Up since  {boot:%Y-%m-%d %H:%M:%S}\nUptime    {up}\nLoad avg  {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}"))


@authorized
async def disk(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows = []
    for p in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        rows.append(f"{p.mountpoint:<14} {bar(u.percent)} {u.percent:5.1f}%  {human(u.used)}/{human(u.total)}")
    await reply(update, "💾 <b>Disks</b>\n" + pre("\n".join(rows)))


def _top(n: int = 5) -> str:
    procs = list(psutil.process_iter())
    for p in procs:
        try:
            p.cpu_percent(None)
        except psutil.Error:
            pass
    time.sleep(1)
    data = []
    for p in procs:
        try:
            data.append((p.pid, p.name()[:18], p.cpu_percent(None), p.memory_percent()))
        except psutil.Error:
            continue
    by_cpu = sorted(data, key=lambda x: x[2], reverse=True)[:n]
    by_mem = sorted(data, key=lambda x: x[3], reverse=True)[:n]
    fmt = lambda r: "\n".join(f"{pid:>7}  {name:<18} CPU {c:5.1f}%  MEM {m:5.1f}%" for pid, name, c, m in r)
    return f"Top CPU\n{fmt(by_cpu)}\n\nTop RAM\n{fmt(by_mem)}"


@authorized
async def top(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, pre(await asyncio.to_thread(_top)))


def _net() -> str:
    a = psutil.net_io_counters()
    time.sleep(1)
    b = psutil.net_io_counters()
    return (
        f"Speed  ↑ {human(b.bytes_sent - a.bytes_sent)}/s   ↓ {human(b.bytes_recv - a.bytes_recv)}/s\n"
        f"Total  ↑ {human(b.bytes_sent)}   ↓ {human(b.bytes_recv)}\n"
        f"Errors in/out: {b.errin}/{b.errout}   Drops in/out: {b.dropin}/{b.dropout}"
    )


@authorized
async def net(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await reply(update, "🌐 <b>Network</b>\n" + pre(await asyncio.to_thread(_net)))


@authorized
async def docker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    out = await run("docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}")
    await reply(update, "🐳 <b>Docker</b>\n" + pre(out))


@authorized
async def failed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    out = await run("systemctl", "--failed", "--no-pager", "--no-legend")
    await reply(update, "⚠️ <b>Failed units</b>\n" + pre(out if out.strip() else "None 🎉"))


@authorized
async def alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global alerts_enabled
    alerts_enabled = not alerts_enabled
    state = "ON ✅" if alerts_enabled else "OFF 🔕"
    await reply(update, f"Alerts {state}\nThresholds: CPU {CPU_LIMIT:.0f}%, RAM {MEM_LIMIT:.0f}%, Disk {DISK_LIMIT:.0f}%")


# ---------- background alerts ----------
async def alert_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not alerts_enabled:
        return
    checks = {
        "CPU": (psutil.cpu_percent(interval=None), CPU_LIMIT),
        "RAM": (psutil.virtual_memory().percent, MEM_LIMIT),
        "Disk": (psutil.disk_usage("/").percent, DISK_LIMIT),
    }
    now = time.time()
    for name, (value, limit) in checks.items():
        if value >= limit and now - last_alert.get(name, 0) > ALERT_COOLDOWN:
            last_alert[name] = now
            for uid in ALLOWED:
                await ctx.bot.send_message(uid, f"🚨 {name} at {value:.1f}% (limit {limit:.0f}%)")


async def post_init(app: Application):
    await app.bot.set_my_commands([
        ("fastfetch", "Run fastfetch on the server"),
        ("servstats", "Live server statistics"),
        ("uptime", "Uptime and load average"),
        ("disk", "Disk usage"),
        ("top", "Top processes"),
        ("net", "Network speed and totals"),
        ("docker", "Running containers"),
        ("failed", "Failed systemd units"),
        ("alerts", "Toggle threshold alerts"),
    ])


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")
    if not ALLOWED:
        raise SystemExit("ALLOWED_USER_IDS is empty — refusing to start an open server-control bot")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    for name, fn in [
        ("start", start), ("help", start), ("fastfetch", fastfetch), ("servstats", servstats),
        ("uptime", uptime), ("disk", disk), ("top", top), ("net", net),
        ("docker", docker), ("failed", failed), ("alerts", alerts),
    ]:
        app.add_handler(CommandHandler(name, fn))
    psutil.cpu_percent(interval=None)  # prime for alert job
    app.job_queue.run_repeating(alert_job, interval=ALERT_INTERVAL, first=ALERT_INTERVAL)
    app.run_polling()


if __name__ == "__main__":
    main()