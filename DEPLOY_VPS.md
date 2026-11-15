# VPS Deploy

This bot is meant to run as a single always-on process.

## Default behavior

- Telegram polling is always on.
- The HTTP health server is disabled by default.
- This avoids port conflicts with an existing website on the same VPS.

## systemd

1. Copy the repo to `/opt/telegram_usdt_bot` on the VPS.
2. Copy `deploy/.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN`.
3. Copy `deploy/systemd/telegram_usdt_bot.service` to `/etc/systemd/system/telegram_usdt_bot.service`.
4. Edit the unit path if you used a different directory.
5. Run:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram_usdt_bot
sudo systemctl status telegram_usdt_bot
```

## Docker

Build and run separately from the website container.

```bash
docker build -t telegram_usdt_bot .
docker run -d --name telegram_usdt_bot --restart unless-stopped --env-file .env telegram_usdt_bot
```

## Notes

- Keep this bot on its own process, container, or systemd unit.
- Do not point it at the same port as the website.
