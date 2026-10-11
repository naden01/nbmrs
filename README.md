# Server Status Telegram Bot

## Setup
```bash
sudo apt install fastfetch fonts-dejavu-core   # or your distro's packages
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # fill in BOT_TOKEN and ALLOWED_USER_IDS
python bot.py
```

## Commands
| Command | What it does |
|---|---|
| `/fastfetch` | Runs fastfetch and sends it as a colored image (logo + color blocks); falls back to text |
| `/servstats` | CPU, RAM, swap, disk, load, uptime, network, temp |
| `/uptime` | Boot time, uptime, load average |
| `/disk` | All mounted disks |
| `/top` | Top 5 processes by CPU and RAM |
| `/net` | Live up/down speed and totals |
| `/docker` | Running containers |
| `/failed` | Failed systemd units |
| `/alerts` | Toggle automatic alerts when CPU/RAM/disk pass thresholds |

## Run as a service
`/etc/systemd/system/servbot.service`:
```ini
[Unit]
Description=Server status Telegram bot
After=network-online.target

[Service]
WorkingDirectory=/opt/servbot
ExecStart=/opt/servbot/venv/bin/python bot.py
Restart=always
User=servbot

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable --now servbot
```
Note: `/docker` needs the service user in the `docker` group.

## Security
Only IDs in `ALLOWED_USER_IDS` can use the bot, and it refuses to start if that list is empty. No command accepts user input, so nothing is executed from chat text.