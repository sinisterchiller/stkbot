# Discord Stock Price Bot

A single-file Discord bot that responds to `/price <ticker>` with the latest available stock price from pre-market, regular and after-hours sessions, the daily change versus the previous trading close, and a dark 1-day 5-minute chart of that same extended-hours session.

## Features

- **Slash command**: `/price NVDA` (case-insensitive, normalized to uppercase)
- **Latest price**: Most recent valid 1-minute close from Yahoo Finance 5-day extended-hours (`prepost=True`) data, spanning pre-market, regular and after-hours sessions; labeled as delayed / latest available, never claimed as real-time
- **Daily change**: Dollar and percentage change against the latest valid daily close strictly before the quote's exchange-local session date (a pre-market quote on a new day uses the previous regular day's close; an after-hours quote on the same day uses the previous day's close)
- **Closed markets**: Uses a 10-day daily lookback instead of blindly taking the second-to-last row, and falls back to `info.previousClose` when daily history is unavailable
- **Chart**: dark-themed 1-day line chart at 5-minute intervals covering the quote's exchange-local session, including pre-market, regular trading and after-hours. It is aggregated from the same 5-day/1-minute extended-hours fetch as the quote (no extra chart request), so it reaches the latest quote timestamp and value. Segments are green when price rises, red when it falls and gray when flat, even when the overall day moved the other way. When the chart cannot be derived the quote is sent without one
- **Timestamp**: Exchange-local time with timezone label, marked as delayed / latest available
- **Error handling**: Friendly messages for invalid tickers, empty data, Yahoo Finance failures, and chart/send failures
- **Non-blocking**: Yahoo Finance history (extended quote plus daily close) and chart rendering all run in an executor
- **Observable command registration**: the startup log reports whether the slash command sync succeeded, which commands were registered, which servers got a copy, and what to check when it failed
- **Automatic per-server registration**: every server the bot is in, and every server it joins later, gets its own `/price` registration as soon as the bot is ready — no guild ID or manual step needed
- **Optional explicit guild**: `DISCORD_GUILD_ID` can still register one server before the gateway connects

## Setup

### 1. Create a Discord Bot Application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** → name it → **Create**
3. Go to the **Bot** tab → **Add Bot** → **Reset Token** → copy the token
4. Go to **OAuth2** → **URL Generator** → select the **bot** and **applications.commands** scopes
5. Under **Bot Permissions**, select:
   - Send Messages
   - Embed Links
   - Attach Files
6. Copy the generated URL, open it in a browser, and invite the bot to your server

This bot uses Discord's default intents and does not require any privileged gateway intents.

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env and set DISCORD_TOKEN to your bot token
```

`.env` options:

| Variable           | Required | Purpose                                                                                        |
| ------------------ | -------- | ---------------------------------------------------------------------------------------------- |
| `DISCORD_TOKEN`    | yes      | Bot token from the Developer Portal                                                             |
| `DISCORD_GUILD_ID` | no       | Optional override: also register `/price` in this one server during startup                     |

Leave `DISCORD_GUILD_ID` unset or empty for the default behaviour: global registration plus automatic
registration in every server the bot is in. A value that is not a numeric server ID stops the bot at
startup with a friendly `Configuration error` message.

### 3. Install Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Run the Bot

```bash
python bot.py
```

The bot registers its slash commands on startup: global registration in `setup_hook` (before the
gateway connects) and a per-server copy for every server it is in once Discord reports the bot as
ready. A successful start prints something like:

```
Synced 1 global command(s): price
Global commands can take up to an hour to appear. Every server the bot is in also receives an instant guild copy once the bot is ready.
Logged in as StockBot#1234 (ID: 123456789012345678)
Synced 1 command(s) to guild 123456789012345678 (My Server): price
Synced 1 command(s) to guild 987654321098765432 (Second Server): price
Registered slash commands in 2 guild(s): 123456789012345678, 987654321098765432
```

If a sync fails, the bot still comes online but prints an `ERROR: ... slash command sync failed`
line with the command names, server ID (when known) and troubleshooting hints. See
[Troubleshooting](#slash-command-troubleshooting).

## How `/price` Is Registered

Registration needs no configuration and no server ID:

1. **Startup (`setup_hook`, before the gateway connects)** — commands are registered globally by
   default. The guild cache does not exist yet at this point, so no per-server work happens here. If
   `DISCORD_GUILD_ID` is set, that one server is registered here instead for the fastest possible
   startup there.
2. **Ready (`on_ready`)** — the first moment Discord has reported the bot's server list. Every
   server the bot is in that has not been registered yet gets a guild copy: the global commands are
   copied with `tree.copy_global_to(guild=...)` and pushed with `tree.sync(guild=...)`. A guild copy
   overrides the global registration, so `/price` is usable right away instead of waiting for global
   propagation.
3. **Server join (`on_guild_join`)** — a server the bot is invited to later is registered
   immediately, using the same copy-then-sync flow.
4. **Server leave (`on_guild_remove`)** — Discord deletes the server's command registrations when the
   bot leaves, so the bot forgets that server. Inviting the bot back registers it again.

Details that keep this safe:

- **No repeated REST syncs.** `ready` fires again on reconnects. Servers already registered are
  skipped, so a reconnect only registers servers that are genuinely missing.
- **Failures stay per server.** A server the bot cannot register in (missing
  `applications.commands` scope, no permission, HTTP error) is logged with its ID and an actionable
  hint, and is never marked as registered. The remaining servers still sync. That server is retried
  up to three times with a short delay, then on the next reconnect or restart.
- **Global registrations are never deleted.** Per-server registration only adds guild copies;
  `tree.clear_commands` is never called, so servers relying on the global registration keep
  working.

The two configuration modes:

- **Unset `DISCORD_GUILD_ID` (default)**: global registration plus automatic per-server registration
  as described above.
- **Set `DISCORD_GUILD_ID`**: additionally registers that one server before the gateway connects.
  Every other server is still registered automatically once the bot is ready. Global command
  registrations are **left unchanged**. If both a global and a guild registration exist for the same
  server, Discord uses the guild copy.

## Slash Command Troubleshooting

### Is the command missing, or is it failing?

- **Command not listed at all** when you type `/`: a registration, invite-scope or client-cache
  problem — use the steps below.
- **Command listed but returns an error / "The application did not respond"**: registration worked
  and the failure is in the bot process. Check the console running `bot.py` and the channel
  permissions below.

### Checklist

1. **Invite scopes.** The bot must be invited with both `bot` and `applications.commands`
   (Developer Portal → **OAuth2** → **URL Generator**). Re-invite with the same settings if unsure;
   without `applications.commands` the bot appears online but has **no** slash commands, and the
   startup log reports a `Forbidden` / missing access error for that server.
2. **Right server.** Every server the bot is in gets its own registration automatically, so no guild
   ID is needed. Global commands also appear wherever the bot is. Confirm you are in that server and
   not looking at a DM.
3. **Bot and channel permissions.** In the target channel the bot needs **View Channel**, **Send
   Messages**, **Embed Links** and **Attach Files** to answer `/price`. Slash commands can also be
   disabled per server under **Server Settings → Integrations** (manage the bot's commands there).
4. **Restart the Discord client.** Press `Ctrl+R` (Windows/Linux) or `Cmd+R` (macOS), or fully quit
   and reopen Discord — the client caches its command list.
5. **Check the startup log.** `Registered slash commands in N guild(s): ...` lists the servers the
   bot registered. A server missing from that list was retried and failed; the `ERROR` / `WARNING`
   lines name its ID and what to fix, and it is retried again on the next reconnect or restart. Only
   the global registration waits for Discord's propagation (usually minutes, up to about an hour).
6. **Read the failure hints.** A failed sync prints `ERROR: ...` plus hints, including wrong
   token/application, missing invite scope, or slash commands disabled for the bot in that server.
   The token is never printed.

### Forcing a clean re-registration

- This bot never deletes global commands, and per-server registration only adds guild copies.
- Restart the bot to register every server it is in again; a server that is still failing is retried
  on each reconnect (`ready`) or restart.
- If an old or unwanted command must actually be removed, do it deliberately yourself, e.g. under
  **Server Settings → Integrations** for a single server, or with a one-off maintenance script you
  run knowingly. No bot startup path removes app-wide commands.

## Usage

In Discord, type `/price` followed by a ticker symbol:

```
/price nvda
/price AAPL
/price BRK-B
/price ^GSPC
```

The bot responds with an embed showing:

- Company name and ticker
- Latest extended-hours price with dollar/percentage change vs the previous trading close
- Previous trading close for reference
- Exchange-local data timestamp from the quote bar, labeled as delayed / latest available
- Dark 1-day 5-minute extended-hours chart as an attachment, covering pre-market, regular and after-hours and ending at the latest quote point (omitted when it cannot be derived)

## How Data Is Selected

1. Fetch extended-hours history once with `period="5d"`, `interval="1m"`, `prepost=True` (pre-market, regular and after-hours).
2. Drop unusable rows and reject non-positive prices; the last valid bar in chronological order is the quote price, and its own timestamp is the displayed timestamp.
3. Fetch daily history with `period="10d"`, `interval="1d"`.
4. Choose the latest valid daily close whose date is strictly earlier than the quote bar's exchange-local session date. This handles weekends, holidays, and stale same-day rows: a pre-market quote on a new day uses the previous regular day's close, and an after-hours quote on the same day uses the previous day's close.
5. If no valid daily close exists, use a valid `info.previousClose`.
6. Derive the chart from the extended-hours data already in memory: keep the exchange-local date of the latest quote bar, whatever weekday it is, so a weekend or holiday quote keeps its own bars (and a day without new bars naturally keeps the last available quote date), resample the 1-minute closes into 5-minute bins taking each bin's last valid close, and append the raw quote point when the quote falls inside a partial final bin. No second chart request is made, and the chart is never used as a fallback for the quote price.

## Notes

- Data source: **yfinance** (unofficial Yahoo Finance interface) — quotes may be delayed and availability is not guaranteed. The bot labels data as delayed / latest available and does not claim real-time pricing.
- The displayed price comes from 1-minute extended-hours data (`period="5d"`, `interval="1m"`, `prepost=True`) and covers pre-market, regular and after-hours sessions.
- The chart is a 1-day/5-minute aggregation of that same extended-hours data, not a separate regular-hours endpoint. It covers pre-market, regular and after-hours, ends at the latest quote point, and uses green for rising segments, red for falling segments and gray for flat segments.
- When the chart cannot be derived, the quote is sent without a chart rather than failing the command.
- Quotes and charts are delayed and not guaranteed to be real-time; the embed footer labels them that way.
- No database, caching, or background jobs — one bot file and one slash command.
- Never commit your actual `.env` file with the token.

## Testing

Tests mock Yahoo Finance and the Discord interaction; no token or network access is required.

```bash
pip install -r requirements.txt
python -m pytest test_bot.py -v
```

The suite exercises the real `/price` callback, including defer behavior, one extended-hours fetch plus one daily lookback and no separate chart fetch, quote/chart consistency (the chart ends at the extended-hours quote timestamp and value), pre-market/regular/after-hours chart coverage, 5-minute bin aggregation, weekend quotes charting their own session (with a Friday-only fallback when no weekend bars exist), chartless quotes when aggregation is unavailable, no stale chart fallback when 1-minute data is missing, closed-session and pre-market previous closes, invalid tickers, empty/error data, and chart buffer cleanup on send failure. Chart tests inspect rendered pixels for a dark background and per-segment green/red/gray colours, including a net-positive day with falling segments. Command registration is covered with a mocked command tree: global sync, explicit guild sync (copy-before-sync, exactly one guild-scoped sync, no global clear or global sync), invalid `DISCORD_GUILD_ID` handling, and that startup sync runs once rather than on every reconnect. A real `CommandTree` test verifies that the global command registration survives explicit guild mode while the guild copy exists. Automatic registration is covered with mocked ready/join/leave events: every cached server is registered on ready, reconnects do not repeat REST syncs, joined servers register once, a failing server does not block the others and is retried, and global commands are never cleared.

## Deployment

Every push to `main` is deployed automatically by the **Deploy to server** GitHub Actions
workflow (`.github/workflows/deploy.yml`) onto the server's self-hosted runner
(`stkbot-server`). No SSH key, GitHub secret, or bot token is required: the runner is the
deploy host, so the job updates the existing `/home/server/stkbot` clone in place.

On every push to `main` the workflow:

1. Checks out the exact pushed commit (kept **outside** the deploy directory).
2. In `/home/server/stkbot`: fetches `origin/main`, verifies the pushed SHA exists and
   is on `origin/main`, and **refuses to deploy if tracked files are dirty** (untracked
   and ignored files such as `.env` and `.venv` are left untouched).
3. Checks out the exact SHA as a detached HEAD.
4. Installs `requirements.txt` into the persistent `/home/server/stkbot/.venv`.
5. Runs the test suite **before** touching the service (`PYTHONDONTWRITEBYTECODE=1`,
   pytest cache disabled).
6. Restarts `stkbot.service` via `systemctl --user`, then verifies the service is active
   and the clone's HEAD equals the pushed SHA.

Any failure fails the job and leaves diagnostics in the log; secrets and full service
logs are never printed. The workflow only triggers on pushes to `main` plus manual
`workflow_dispatch` — pull-request jobs from forks are never executed. The service
unit and the server's `.env` are never modified.

### Checking deployment status

- **Runner**: repository → **Actions** → **Runners** — `stkbot-server` should be
  **online** (labels `self-hosted, Linux, X64, stkbot`).
- **Workflow runs**: **Actions** → **Deploy to server**, or
  `gh run list -R sinisterchiller/stkbot --limit 5`.
- **Service** (on the server): `systemctl --user status stkbot.service` and
  `systemctl --user is-active stkbot.service`.
- **Deployed commit** (on the server): `git -C /home/server/stkbot rev-parse HEAD`
  should match the pushed `main` HEAD.

### Local bot vs. server bot

Only one bot process should be connected with the same Discord token at a time. Stop any
local `python bot.py` run before the server deploys or restarts, otherwise the two
processes compete for the same Discord session (duplicate gateway connections and
conflicting command syncs).
