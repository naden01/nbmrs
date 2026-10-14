#!/usr/bin/env python3
"""Telegram server-status bot.

Commands:
  /fastfetch  - fastfetch output as a colored terminal screenshot
  /servstats  - live dashboard: gauges, history graphs, cores, network, top processes
  /uptime     - uptime, boot time, load average
  /disk       - all mounted disks
  /top        - top processes by CPU and RAM
  /net        - live network speed, history and interfaces
  /docker     - containers and their state
  /failed     - failed systemd units
  /alerts     - alert status and on/off switch

Every result has a 🔄 Refresh button that updates the message in place.
"""
from __future__ import annotations

import asyncio
import functools
import html
import logging
import os
import platform
import re
import shutil
import socket
import statistics
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import psutil
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Conflict, NetworkError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()
try:  # Pillow + pyte are optional: without them the bot falls back to text output
    import render

    RENDER_OK = True
except ImportError:
    render = None
    RENDER_OK = False

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
# Quiet the chatty per-tick INFO logs: httpx/httpcore embed the bot token in the
# URL, and the JobQueue (APScheduler) logs "Running job / executed successfully"
# for every 5s sample. Keep WARNING+ so real problems still surface.
for noisy in ("httpx", "httpcore", "apscheduler.executors.default"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("servbot")

# ---------------------------------------------------------------- config
TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
CPU_LIMIT = float(os.getenv("ALERT_CPU", "90"))
MEM_LIMIT = float(os.getenv("ALERT_MEM", "90"))
DISK_LIMIT = float(os.getenv("ALERT_DISK", "90"))
ALERT_INTERVAL = int(os.getenv("ALERT_INTERVAL", "60"))  # seconds between checks
ALERT_COOLDOWN = int(os.getenv("ALERT_COOLDOWN", "1800"))  # seconds between reminders while still high
ALERT_HYSTERESIS = 5  # must drop this many points below the limit to count as recovered
SAMPLE_INTERVAL = int(os.getenv("SAMPLE_INTERVAL", "5"))  # history sampling period (seconds)
HISTORY_POINTS = int(os.getenv("HISTORY_POINTS", "72"))  # 72 x 5s = 6 minutes of graph

# /fastfetch: "image" = colored terminal screenshot, "text" = plain monospace text.
FASTFETCH_MODE = os.getenv("FASTFETCH_MODE", "image").lower()
# Shell/Terminal are left out on purpose: under a bot they would say "bot.py" / "sshd".
# Set FASTFETCH_STRUCTURE= (empty) to use your own fastfetch config instead.
FASTFETCH_STRUCTURE = os.getenv(
    "FASTFETCH_STRUCTURE",
    "Title:Separator:OS:Host:Kernel:Uptime:Packages:Display:CPU:GPU:Memory:Swap:Disk:LocalIP:Locale:Break:Colors",
)

HOST = socket.gethostname()
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SPARKS = "▁▂▃▄▅▆▇█"

alerts_enabled = True
alert_state: dict[str, dict] = {}
HISTORY: deque[dict] = deque(maxlen=HISTORY_POINTS)
_prev_net: tuple[float, int, int] | None = None
SNAP_LOCK = asyncio.Lock()  # one heavy sample at a time


# ---------------------------------------------------------------- helpers
def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def bar(pct: float, width: int = 12) -> str:
    filled = max(0, min(width, round(width * pct / 100)))
    return "█" * filled + "░" * (width - filled)


def spark(values, vmax: float | None = None, width: int = 24) -> str:
    vals = list(values)[-width:]
    if not vals:
        return ""
    top = vmax or max(vals) or 1
    return "".join(SPARKS[min(7, int(max(v, 0) / top * 7.999))] for v in vals)


def dot(pct: float) -> str:
    return "🟢" if pct < 60 else "🟡" if pct < 85 else "🔴"


def fmt_uptime(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m" if h else f"{m}m"


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def pre(text: str) -> str:
    text = ANSI.sub("", text).strip() or "(no output)"
    if len(text) > 3800:
        text = text[:3800] + "\n… truncated"
    return f"<pre>{esc(text)}</pre>"


async def run_bytes(*cmd: str, timeout: int = 20, env: dict | None = None) -> bytes:
    """Run a command (no shell) and return raw combined output."""
    if not shutil.which(cmd[0]):
        return f"'{cmd[0]}' is not installed on this server.".encode()
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, env=env,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()  # reap the child so it does not linger as a zombie
        return f"Timed out after {timeout}s.".encode()
    return out


async def run(*cmd: str, timeout: int = 20) -> str:
    return (await run_bytes(*cmd, timeout=timeout)).decode(errors="replace")


# ---------------------------------------------------------------- sampling
def take_sample() -> None:
    """Called every SAMPLE_INTERVAL seconds; feeds the history graphs and alerts."""
    global _prev_net
    now, c = time.time(), psutil.net_io_counters()
    cpu = psutil.cpu_percent(None)
    if _prev_net is not None:
        dt = now - _prev_net[0]
        rx = max(c.bytes_recv - _prev_net[1], 0) / dt if dt > 0 else 0.0
        tx = max(c.bytes_sent - _prev_net[2], 0) / dt if dt > 0 else 0.0
        HISTORY.append({"t": now, "cpu": cpu, "mem": psutil.virtual_memory().percent, "rx": rx, "tx": tx})
    _prev_net = (now, c.bytes_recv, c.bytes_sent)


def sample_cpu_and_procs() -> tuple[list[float], list[tuple[int, str, float, float]]]:
    """Per-core CPU % and per-process (pid, name, cpu%, mem%) measured over ~0.8s."""
    procs = []
    for p in psutil.process_iter(["name"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except psutil.Error:
            pass
    psutil.cpu_percent(percpu=True)
    time.sleep(0.8)
    cores = psutil.cpu_percent(percpu=True)
    rows = []
    for p in procs:
        try:
            rows.append((p.pid, p.info.get("name") or "?", p.cpu_percent(None), p.memory_percent()))
        except psutil.Error:
            continue
    return cores, rows


def _temperature() -> float | None:
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
        if temps.get(key):
            return max(t.current for t in temps[key])
    for entries in temps.values():
        if entries:
            return entries[0].current
    return None


@functools.lru_cache(maxsize=1)
def _os_name() -> str:
    try:
        return platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
    except Exception:
        return platform.system()


def collect_snapshot() -> dict:
    cores, procs = sample_cpu_and_procs()
    vm, sw, du = psutil.virtual_memory(), psutil.swap_memory(), psutil.disk_usage("/")
    try:
        freq = psutil.cpu_freq().current
    except Exception:
        freq = None
    last = HISTORY[-1] if HISTORY else {"rx": 0.0, "tx": 0.0}
    procs.sort(key=lambda r: r[2], reverse=True)
    return {
        "host": HOST, "os": _os_name(), "kernel": platform.release(),
        "time": datetime.now().strftime("%H:%M:%S %Z").strip(),
        "uptime": time.time() - psutil.boot_time(),
        "load": os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0),
        "cpu": sum(cores) / len(cores) if cores else 0.0, "cores": cores,
        "threads": psutil.cpu_count() or len(cores), "freq": freq, "nproc": len(procs),
        "mem": (vm.total - vm.available, vm.total, vm.percent),
        "swap": (sw.used, sw.total, sw.percent),
        "disk": (du.used, du.total, du.percent), "disk_path": "/",
        "temp": _temperature(), "rx": last["rx"], "tx": last["tx"],
        "procs": [(n, c, m) for _, n, c, m in procs[:5]],
    }


# ---------------------------------------------------------------- views
@dataclass
class View:
    text: str | None = None
    photo: bytes | None = None


def stats_text(s: dict) -> str:
    hist = list(HISTORY)
    mem, sw, du = s["mem"], s["swap"], s["disk"]
    lines = [
        f'CPU   {bar(s["cpu"])} {s["cpu"]:5.1f}%  {spark([h["cpu"] for h in hist], 100, 14)}',
        f"RAM   {bar(mem[2])} {mem[2]:5.1f}%  {human(mem[0])}/{human(mem[1])}",
        f"Swap  {bar(sw[2])} {sw[2]:5.1f}%  {human(sw[0])}/{human(sw[1])}",
        f"Disk  {bar(du[2])} {du[2]:5.1f}%  {human(du[0])}/{human(du[1])}",
        "",
        f'Load  {" ".join(f"{v:.2f}" for v in s["load"])}   Procs {s["nproc"]}',
        f'Up    {fmt_uptime(s["uptime"])}' + (f'   Temp {s["temp"]:.0f}°C' if s["temp"] is not None else ""),
        f'Net   ↓ {human(s["rx"])}/s  ↑ {human(s["tx"])}/s',
    ]
    worst = max(s["cpu"], mem[2], du[2])
    return f'{dot(worst)} <b>{esc(s["host"])}</b> · {s["time"]}\n<pre>{esc(chr(10).join(lines))}</pre>'


async def view_servstats() -> View:
    async with SNAP_LOCK:
        snap = await asyncio.to_thread(collect_snapshot)
    if RENDER_OK:
        try:
            png = await asyncio.to_thread(render.render_dashboard, snap, list(HISTORY), SAMPLE_INTERVAL, HISTORY_POINTS)
            return View(photo=png)
        except Exception:
            log.exception("dashboard render failed, falling back to text")
    return View(text=stats_text(snap))


async def view_fastfetch() -> View:
    if not shutil.which("fastfetch"):
        return View(text="fastfetch is not installed on this server.")
    if FASTFETCH_MODE == "image" and RENDER_OK:
        try:
            env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
            env.update(TERM="xterm-256color", COLUMNS="110", LINES="100")
            args = ["--structure", FASTFETCH_STRUCTURE] if FASTFETCH_STRUCTURE else []
            raw = await run_bytes("fastfetch", "--pipe", "false", *args, env=env)
            png = await asyncio.to_thread(render.fastfetch_png, raw, f"fastfetch — {HOST}")
            return View(photo=png)
        except Exception:
            log.exception("fastfetch image render failed, falling back to text")
    structure = FASTFETCH_STRUCTURE.replace(":Break", "").replace(":Colors", "")
    args = ["--structure", structure] if structure else []
    return View(text=pre(await run("fastfetch", "--logo", "none", "--pipe", *args)))


async def view_uptime() -> View:
    up = time.time() - psutil.boot_time()
    load = os.getloadavg()
    hist = list(HISTORY)
    cpus = psutil.cpu_count() or 1
    text = (
        f"⏱ <b>Up {fmt_uptime(up)}</b>\n"
        f"<pre>Booted   {datetime.fromtimestamp(psutil.boot_time()):%Y-%m-%d %H:%M:%S}\n"
        f"Load     {load[0]:.2f}  {load[1]:.2f}  {load[2]:.2f}   (1/5/15 min, {cpus} threads)\n"
        f"Load now {bar(min(load[0] / cpus * 100, 100))} {load[0] / cpus * 100:.0f}%\n"
        f"CPU      {spark([h['cpu'] for h in hist], 100, 30)}</pre>"
    )
    return View(text=text)


async def view_disk() -> View:
    blocks = []
    for p in psutil.disk_partitions(all=False):
        if p.fstype in ("squashfs", "iso9660") or p.mountpoint.startswith(("/snap", "/var/snap")):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        blocks.append(
            f"{dot(u.percent)} <b>{esc(p.mountpoint)}</b>  <i>{esc(p.fstype)} · {esc(p.device)}</i>\n"
            f"<code>{bar(u.percent, 16)} {u.percent:4.1f}%</code>\n"
            f"<code>{human(u.used)} used · {human(u.free)} free of {human(u.total)}</code>"
        )
    try:
        io = psutil.disk_io_counters()
        blocks.append(f"<i>I/O since boot: read {human(io.read_bytes)} · written {human(io.write_bytes)}</i>")
    except Exception:
        pass
    return View(text="💾 <b>Disks</b>\n\n" + "\n\n".join(blocks))


async def view_top() -> View:
    async with SNAP_LOCK:
        cores, rows = await asyncio.to_thread(sample_cpu_and_procs)
    cpu = sum(cores) / len(cores)
    vm = psutil.virtual_memory()

    def table(items, key):
        head = f"{'PID':>7}  {'PROCESS':<18} {'CPU':>6} {'MEM':>6}"
        body = [f"{pid:>7}  {name[:18]:<18} {c:>5.1f}% {m:>5.1f}%" for pid, name, c, m in sorted(items, key=key, reverse=True)[:6]]
        return pre(head + "\n" + "\n".join(body))

    text = (
        f"{dot(cpu)} CPU <b>{cpu:.0f}%</b>   {dot(vm.percent)} RAM <b>{vm.percent:.0f}%</b>\n\n"
        f"🔥 <b>Top by CPU</b>\n{table(rows, lambda r: r[2])}\n"
        f"🧠 <b>Top by RAM</b>\n{table(rows, lambda r: r[3])}"
    )
    return View(text=text)


async def view_net() -> View:
    if not HISTORY:  # just started: measure directly
        a = psutil.net_io_counters()
        await asyncio.sleep(1)
        b = psutil.net_io_counters()
        rx, tx = b.bytes_recv - a.bytes_recv, b.bytes_sent - a.bytes_sent
        rx_hist, tx_hist = [], []
    else:
        rx, tx = HISTORY[-1]["rx"], HISTORY[-1]["tx"]
        rx_hist, tx_hist = [h["rx"] for h in HISTORY], [h["tx"] for h in HISTORY]
    c = psutil.net_io_counters()
    peak = max(rx_hist + tx_hist + [1])

    lines = [
        f"↓ {human(rx) + '/s':<12} {spark(rx_hist, peak, 30)}",
        f"↑ {human(tx) + '/s':<12} {spark(tx_hist, peak, 30)}",
        "",
        f"Total  ↓ {human(c.bytes_recv)}   ↑ {human(c.bytes_sent)}",
        f"Errors {c.errin}/{c.errout} in/out   Drops {c.dropin}/{c.dropout} in/out",
    ]
    addrs, stats = psutil.net_if_addrs(), psutil.net_if_stats()
    ifaces = []
    for name, st in stats.items():
        if name == "lo" or not st.isup or name.startswith(("veth", "docker", "br-", "virbr")):
            continue
        ip = next((a.address for a in addrs.get(name, []) if a.family == socket.AF_INET), "-")
        ifaces.append(f"{name:<10} {ip:<16}{f'{st.speed} Mbps' if st.speed else ''}")
    text = "🌐 <b>Network</b>\n" + pre("\n".join(lines))
    if ifaces:
        text += "\n<b>Interfaces</b>\n" + pre("\n".join(ifaces))
    return View(text=text)


async def view_docker() -> View:
    out = await run("docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}\t{{.Image}}")
    rows = [line.split("\t") for line in out.strip().splitlines()]
    if not rows or any(len(r) != 4 for r in rows):
        return View(text="🐳 <b>Docker</b>\n" + pre(out if out.strip() else "No containers."))
    order = {"running": 0, "restarting": 1, "paused": 1}
    rows.sort(key=lambda r: (order.get(r[1], 2), r[0]))
    lines = []
    for name, state, status, image in rows:
        icon = "🟢" if state == "running" else "🟡" if state in ("restarting", "paused") else (
            "⚪" if "(0)" in status else "🔴")
        lines.append(f"{icon} <b>{esc(name)}</b> — {esc(status)}\n      <code>{esc(image)}</code>")
    running = sum(r[1] == "running" for r in rows)
    text = f"🐳 <b>Docker</b> · {running}/{len(rows)} running\n\n" + "\n".join(lines)
    return View(text=text[:4000])


async def view_failed() -> View:
    out = await run("systemctl", "--failed", "--no-pager", "--no-legend", "--plain")
    if not out.strip():
        return View(text="✅ <b>No failed systemd units</b>")
    return View(text="⚠️ <b>Failed units</b>\n" + pre(out))


VIEWS = {
    "fastfetch": view_fastfetch, "servstats": view_servstats, "uptime": view_uptime, "disk": view_disk,
    "top": view_top, "net": view_net, "docker": view_docker, "failed": view_failed,
}
PHOTO_VIEWS = {"fastfetch", "servstats"}
MENU = [
    ("📊 Stats", "servstats"), ("🖥 Fastfetch", "fastfetch"), ("🔥 Top", "top"),
    ("🌐 Net", "net"), ("💾 Disk", "disk"), ("⏱ Uptime", "uptime"),
    ("🐳 Docker", "docker"), ("⚠️ Failed", "failed"),
]


# ---------------------------------------------------------------- telegram plumbing
def authorized(func):
    @functools.wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ALLOWED:
            log.warning("Unauthorized access from %s", user.id if user else "unknown")
            if update.effective_message:
                await update.effective_message.reply_text("⛔ Not authorized.")
            return
        return await func(update, ctx)

    return wrapper


def refresh_kb(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data=f"r:{name}")]])


def menu_kb() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(label, callback_data=f"go:{name}") for label, name in MENU]
    return InlineKeyboardMarkup([buttons[i:i + 3] for i in range(0, len(buttons), 3)])


async def send_view(message, name: str, view: View) -> None:
    if view.photo:
        await message.reply_photo(photo=view.photo, reply_markup=refresh_kb(name))
    else:
        await message.reply_text(view.text, parse_mode=ParseMode.HTML, reply_markup=refresh_kb(name),
                                 disable_web_page_preview=True)


async def show_view(message, name: str) -> None:
    await message.chat.send_action(ChatAction.UPLOAD_PHOTO if name in PHOTO_VIEWS else ChatAction.TYPING)
    await send_view(message, name, await VIEWS[name]())


def make_command(name: str):
    @authorized
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await show_view(update.effective_message, name)

    return handler


@authorized
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🖥 <b>{esc(HOST)}</b> status bot\n\n"
        "/servstats — live dashboard\n/fastfetch — system info\n/top — heaviest processes\n"
        "/net — network speed &amp; interfaces\n/disk — disk usage\n/uptime — uptime &amp; load\n"
        "/docker — containers\n/failed — failed services\n/alerts — alert switch\n\n"
        "Or tap a button:"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=menu_kb())


async def on_go(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ALLOWED:
        await q.answer("⛔ Not authorized", show_alert=True)
        return
    name = q.data.split(":", 1)[1]
    if name not in VIEWS:
        await q.answer()
        return
    await q.answer("Loading…")
    await show_view(q.message, name)


async def on_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ALLOWED:
        await q.answer("⛔ Not authorized", show_alert=True)
        return
    name = q.data.split(":", 1)[1]
    if name not in VIEWS:
        await q.answer()
        return
    await q.answer("Refreshing…")
    view = await VIEWS[name]()
    try:
        if view.photo and q.message.photo:
            await q.edit_message_media(InputMediaPhoto(view.photo), reply_markup=refresh_kb(name))
        elif view.text and not q.message.photo:
            await q.edit_message_text(view.text, parse_mode=ParseMode.HTML, reply_markup=refresh_kb(name),
                                      disable_web_page_preview=True)
        else:  # message type changed (e.g. image failed earlier and fell back to text)
            await send_view(q.message, name, view)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ---------------------------------------------------------------- alerts
def alerts_message() -> tuple[str, InlineKeyboardMarkup]:
    state = "ON 🔔" if alerts_enabled else "OFF 🔕"
    active = [f"{n} high" for n, st in alert_state.items() if st["active"]]
    text = (
        f"<b>Alerts {state}</b>\n"
        f"<pre>CPU   ≥ {CPU_LIMIT:.0f}%  (avg of last 3 samples)\nRAM   ≥ {MEM_LIMIT:.0f}%\nDisk  ≥ {DISK_LIMIT:.0f}%  (/)</pre>"
        f"Checked every {ALERT_INTERVAL}s, reminder every {ALERT_COOLDOWN // 60} min while high."
    )
    if active:
        text += "\n🚨 Active: " + ", ".join(active)
    label = "🔕 Turn off" if alerts_enabled else "🔔 Turn on"
    return text, InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="a:toggle")]])


@authorized
async def cmd_alerts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = alerts_message()
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def on_alert_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global alerts_enabled
    q = update.callback_query
    if q.from_user.id not in ALLOWED:
        await q.answer("⛔ Not authorized", show_alert=True)
        return
    alerts_enabled = not alerts_enabled
    await q.answer("Alerts on" if alerts_enabled else "Alerts off")
    text, kb = alerts_message()
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def notify(bot, text: str) -> None:
    for uid in ALLOWED:
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("Could not notify %s: %s", uid, e)


async def alert_job(ctx: ContextTypes.DEFAULT_TYPE):
    if not alerts_enabled:
        return
    recent = [h["cpu"] for h in list(HISTORY)[-3:]]
    values = {
        "CPU": (statistics.mean(recent) if recent else 0.0, CPU_LIMIT),
        "RAM": (psutil.virtual_memory().percent, MEM_LIMIT),
        "Disk": (psutil.disk_usage("/").percent, DISK_LIMIT),
    }
    now = time.time()
    for name, (value, limit) in values.items():
        st = alert_state.setdefault(name, {"active": False, "since": 0.0, "last": 0.0})
        if value >= limit:
            if st["active"] and now - st["last"] <= ALERT_COOLDOWN:
                continue
            first = not st["active"]
            if first:
                st["since"] = now
            st.update(active=True, last=now)
            msg = f"🚨 <b>{name} {'high' if first else 'still high'}</b> · {esc(HOST)}\n"
            msg += f"<code>{bar(value, 16)} {value:.1f}%</code>  (limit {limit:.0f}%)"
            if not first:
                msg += f"\nHigh for {fmt_uptime(now - st['since'])}"
            if name in ("CPU", "RAM"):
                try:
                    async with SNAP_LOCK:
                        _, rows = await asyncio.to_thread(sample_cpu_and_procs)
                    idx = 2 if name == "CPU" else 3
                    top = sorted(rows, key=lambda r: r[idx], reverse=True)[:3]
                    msg += "\n" + "\n".join(f"• {esc(r[1])} — {r[idx]:.1f}%" for r in top)
                except Exception:
                    log.exception("could not collect culprits")
            await notify(ctx.bot, msg)
        elif st["active"] and value < limit - ALERT_HYSTERESIS:
            st["active"] = False
            await notify(ctx.bot, f"✅ <b>{name} back to normal</b> · {esc(HOST)}\n"
                                  f"{value:.1f}% (was high for {fmt_uptime(now - st['since'])})")


async def sample_job(ctx: ContextTypes.DEFAULT_TYPE):
    take_sample()


# ---------------------------------------------------------------- app
async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    if isinstance(err, Conflict):
        log.error("409 Conflict: another process is polling with this token. Stop it or use a different bot token.")
    elif isinstance(err, NetworkError):
        log.warning("Network error: %s", err)
    else:
        log.error("Unhandled error", exc_info=err)


async def post_init(app: Application):
    await app.bot.set_my_commands([
        ("fastfetch", "System info (terminal screenshot)"),
        ("servstats", "Live dashboard"),
        ("uptime", "Uptime and load average"),
        ("disk", "Disk usage"),
        ("top", "Top processes"),
        ("net", "Network speed and interfaces"),
        ("docker", "Containers"),
        ("failed", "Failed systemd units"),
        ("alerts", "Alert status and switch"),
    ])
    up = time.time() - psutil.boot_time()
    if up < 300:  # bot started right after a boot -> tell the owner the server restarted
        await notify(app.bot, f"🔄 <b>{esc(HOST)}</b> just booted (up {fmt_uptime(up)}). Bot is online.")


def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")
    if not ALLOWED:
        raise SystemExit("ALLOWED_USER_IDS is empty — refusing to start an open server-control bot")
    if not RENDER_OK:
        log.warning("Pillow/pyte not installed — running in text-only mode (pip install -r requirements.txt)")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    for name in VIEWS:
        app.add_handler(CommandHandler(name, make_command(name)))
    app.add_handler(CallbackQueryHandler(on_refresh, pattern=r"^r:"))
    app.add_handler(CallbackQueryHandler(on_go, pattern=r"^go:"))
    app.add_handler(CallbackQueryHandler(on_alert_toggle, pattern=r"^a:toggle$"))
    app.add_error_handler(on_error)

    psutil.cpu_percent(None)  # prime the baseline for take_sample()
    app.job_queue.run_repeating(sample_job, interval=SAMPLE_INTERVAL, first=1)
    app.job_queue.run_repeating(alert_job, interval=ALERT_INTERVAL, first=ALERT_INTERVAL)
    app.run_polling()


if __name__ == "__main__":
    main()