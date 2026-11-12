# Changelog

All notable changes to the Discord stock price bot. The project has no tagged
releases; this file records what changed on `dev` before it was merged into
`main`.

## Unreleased — `dev-feature-price-alerts` merged into `main` (2026-11-11)

This merge brings one-shot stock price alerts and a private `/help` guide
from `dev-feature-price-alerts` (`7fe6d80..7f1af9c`) onto `main`. Everything
below is a change relative to the previous `main` commit `7fe6d80`; the
baseline behavior that already existed on `main` is listed separately at
the end.

### Added

- **`/remind <ticker> <target>` — one-shot price alerts (server channels
  only).** The command defers ephemerally, validates the ticker
  (`TICKER_PATTERN`) and a positive finite target, then fetches one
  lightweight latest price: a single 5-day/1-minute `prepost=True` history
  request covering pre-market, regular and after-hours — no `info`, no daily
  history and no chart request. The alert direction is derived from the
  current price: **above** when the target is above it, **below** when it is
  below. A target equal to the current price at displayed 2-decimal
  precision is rejected, so an alert created on a stale closed-market quote
  can never fire on that same stale value. The confirmation is ephemeral and
  shows the alert ID, ticker, current price, target, direction and the quote
  timestamp with a delayed-data disclaimer.
- **`/reminders` — owner-only pending alert list (ephemeral).** Lists only
  the invoking user's alerts (ID, ticker, target, direction). Embeds are
  chunked to stay inside Discord's per-embed limits (at most 25 fields and a
  5500-character text budget below the 6000-character cap), and each message
  holds at most 10 embeds whose combined text stays within a 5500-character
  per-message budget, so very long lists are split across several ephemeral
  follow-up messages with no alert missing.
- **`/unremind <id>` — owner-only cancellation (ephemeral).** Deletes the
  alert only when the invoking user owns it; other users' alerts are never
  visible or deletable.
- **`/help` — private command guide (ephemeral).** A single ephemeral embed
  describing `/price`, `/remind`, `/reminders` and `/unremind` plus how
  alerts work, including which responses are private and which are public.
- **SQLite persistence layer (`reminders.py`).** A thin CRUD store
  (`ReminderStore`) over a SQLite database at `reminders.db` next to
  `reminders.py` — a path stable under systemd regardless of the working
  directory. Every call opens its own short-lived connection with
  `PRAGMA journal_mode=WAL`. The `reminders` table stores the alert ID, owner
  user ID, channel ID, ticker, target price, direction and creation
  timestamp. The database file and its `-wal`, `-shm` and `-journal` side
  files are gitignored, so the file stays in the server's
  `/home/server/stkbot` checkout and pending alerts survive restarts and
  deploys.
- **60-second alert checker.** A single `@tasks.loop(seconds=60)` background
  task is started exactly once in `setup_hook` (a `_price_alerts_started`
  guard prevents a double start on reconnect) and is cancelled in the bot's
  `close()`, so shutdown is clean. Each cycle loads every pending reminder,
  fetches one lightweight quote per unique ticker in executor threads (the
  event loop is never blocked), and fires an alert once its price reaches the
  target: direction `above` fires at `price >= target`, direction `below` at
  `price <= target`. The owner is mentioned in the original guild channel
  with user-only `allowed_mentions` (`users=[owner]`, `everyone=False`,
  `roles=False`, `replied_user=False`), so `@everyone`, role and
  replied-user mentions stay off. The channel is resolved via
  `bot.get_channel` with a `bot.fetch_channel` REST fallback, and DM channels
  never receive alerts.
- **Send-then-delete semantics.** A reminder is deleted only after its
  notification message has been sent successfully; any channel-lookup, quote
  fetch or send failure retains the reminder for the next cycle.
- **Expanded tests.** The suite grew from 61 test functions (96 collected
  test cases) on the previous `main` baseline to 138 test functions (174
  collected test cases including parameterization): 115 in `test_bot.py` and
  23 in the new `test_reminders.py`. New coverage includes the lightweight
  quote helper (exactly one 5d/1m fetch), `/remind` validation (invalid
  ticker, non-positive target, DM channel, equal current price, executor
  threading) and persistence, `/reminders` owner-only listing with embed
  chunking and multi-message character-budget batching (250+ alerts),
  `/unremind` owner-only cancellation, the 60-second cycle (unique-ticker
  quote fetches, upward/downward/equal triggers, one-time notification,
  failed fetch/send retention, DM and unavailable-channel retention,
  `get_channel`→`fetch_channel` fallback, persistence across a simulated
  restart, loop started exactly once, ready guard and clean stop on close),
  and the SQLite storage layer (schema, validation, user isolation,
  owner-checked and ID-only deletes).

### Changed

- `bot.py` now imports `reminders.ReminderStore` and `discord.ext.tasks` and
  creates a module-level `reminder_store`; `StockPriceBot.setup_hook` starts
  the alert loop once, and a new `StockPriceBot.close()` cancels it before
  shutting the client down.
- `.gitignore` now ignores `reminders.db` plus its `-wal`, `-shm` and
  `-journal` side files.
- `README.md` documents the new commands, alert persistence, batching
  limits and the expanded test coverage.

### Behavior notes and limitations

- **Privacy.** `/price` answers remain public in the channel, and fired
  alerts are posted publicly in the channel where they were created,
  mentioning the owner. The management surface — `/remind` confirmations,
  `/reminders` lists, `/unremind` results and `/help` — is private
  (ephemeral), so one user's alerts are never visible to other users.
- **Delivery is at-least-once, not exactly-once.** The reminder is deleted
  only after a successful send, which prevents the usual duplicate path. If
  the message is sent but the database delete fails, the alert may be
  re-sent on the next cycle; that is the only known duplicate path.
- **Timing and data caveats.** Alerts are checked once every 60 seconds
  against Yahoo Finance data via yfinance, an unofficial interface: quotes
  may be delayed, extended-hours availability varies by ticker, and no
  tick-perfect trigger is guaranteed. Fired alerts carry the quote timestamp
  and a "delayed / latest available, not real-time" caveat. When the market
  is closed the same stale quote persists, and because alert directions are
  always set away from the current price at creation, a stale quote can never
  trigger its own alert automatically.
- **Existing `/price` and chart behavior is unchanged.** The extended-hours
  quote, previous-close selection, dark per-segment 5-minute chart and
  chartless-quote degradation from the previous release all work exactly as
  before; the alert path reuses the same quote sanitization with a lighter
  fetch.

### Commits merged

| Commit | Summary |
| --- | --- |
| `35f3c13` | SQLite persistence layer for stock price reminders |
| `fb038d5` | One-shot price alerts with /remind, /reminders, /unremind and 60-second checker |
| `1c73a5e` | Private alerts, clean loop shutdown, restricted mentions, batched /reminders lists |
| `4731e4d` | Size /reminders embed batches to Discord's 6000-character embed limits |
| `2163ec2` | Private /help slash command with tests and README docs |
| `a30c110` | Correct /help privacy claims to match actual visibility |
| `7f1af9c` | Fix /help crash: use InteractionResponse.send_message |

### Baseline already on `main` before this merge

These behaviors existed at `7fe6d80` and are not part of this merge's changes:

- The `/price` slash command with its extended-hours quote, previous-close
  selection, dark per-segment 5-minute chart and chartless-quote degradation.
- Per-server command registration and startup sync logging.
- Error handling, executor-based non-blocking fetches/chart rendering, chart
  buffer cleanup, and the GitHub Actions self-hosted deployment workflow.

## Unreleased — `dev` merged into `main` (2026-11-11)

This merge brings the extended-hours quote and chart work from `dev`
(`c68ab9d..a995e94`) onto `main`. Everything below is a change relative to the
previous `main` commit `c68ab9d`; the baseline behavior that already existed on
`main` is listed separately at the end.

### Added

- **Extended-hours quote data.** The displayed price and timestamp now come
  from one `yfinance` history request with `period="5d"`, `interval="1m"` and
  `prepost=True`, so the quote covers pre-market, regular trading and
  after-hours instead of only regular-session 5-minute bars.
- **Last valid bar selection.** Rows with a missing, infinite, zero or negative
  close are dropped individually; the quote is the last chronologically sorted
  bar that has a finite positive close, and that bar's own timestamp is the
  timestamp shown in the embed. A trailing bad row no longer discards the
  otherwise valid history.
- **Dark, per-segment chart.** The 1-day 5-minute chart is rendered on a dark
  background (`#12161c`) with muted grid/text colors and a highlighted last
  point. Each line segment is colored by its own direction — green when price
  rises, red when it falls, gray when flat — instead of coloring the whole line
  by the day's net change. A single-point session renders as one marker.
- **Same-session extended-hours chart.** The chart is aggregated from the same
  5-day/1-minute extended-hours frame used for the quote; no second chart
  request is made. The session is the quote bar's exchange-local date, and its
  1-minute closes are resampled into 5-minute bins using each bin's last valid
  close. When the quote falls inside a partial final bin, the raw quote point is
  appended so the chart ends at the real quote timestamp and value rather than
  at the bin edge.
- **Weekend and holiday handling.** A valid weekend quote charts that quote
  bar's own session date (including weekend bars when Yahoo returns them)
  rather than substituting an earlier weekday. When no newer bars exist, the
  latest quote bar is itself the last available bar, so its own session date is
  charted; if a weekend's newest data is Friday's session, Friday is charted.
- **Chart aggregated from the quote fetch.** No separate chart request is
  made, so a chart problem cannot invalidate a quote that was already
  selected. When aggregation fails, the quote is sent without an image.
  Rendering and the Discord attachment/send happen after the quote is ready;
  when either fails, the command replies with a friendly ephemeral "could not
  build or send the chart" message instead of sending the quote alone.
- **Explicit delayed-data labeling.** The embed footer now names the quote
  source (`5d · 1-minute extended hours`), the chart derivation (same-session
  5-minute bins, pre-market → after-hours), and the delayed / not-guaranteed-
  real-time disclaimer; the chartless footer says no chart is available. The
  embed field is labeled `Change vs prev close`, and the slash-command
  description mentions extended hours.
- **Expanded tests.** The suite grew from 47 test functions on the previous
  `main` baseline to 61 test functions (96 collected test cases including
  parameterization) on `dev`. New coverage includes extended quote vs. chart
  separation, pre-market and after-hours previous closes, invalid
  trailing/mixed/entirely-invalid bar handling, 5-minute bin aggregation, full
  pre-market→after-hours chart coverage, weekend charting (including a weekend
  whose newest bars are Friday's), chartless quotes, chart aggregation
  failures, friendly ephemeral chart render/send failures, missing 1-minute
  data without a stale fallback, rendered pixel checks for the dark background
  and green/red/gray segments, a net-positive day with falling segments, a
  single-point chart, and that importing/rendering does not mutate global
  matplotlib style.

### Changed

- `bot.py` constants: `INTRADAY_PERIOD`/`INTRADAY_INTERVAL` (`1d`/`5m`) were
  replaced by `EXTENDED_PERIOD`/`EXTENDED_INTERVAL` (`5d`/`1m`) plus
  `CHART_INTERVAL = "5min"`; chart colors live in module constants set on the
  figure/axes so a user's matplotlib `rcParams` are never modified.
- `fetch_quote()` now performs the extended-hours fetch first, derives the
  optional chart from that frame, and uses daily history only for the previous
  close (`info.previousClose` remains the fallback).
- `_sanitize_intraday()` keeps every valid positive row instead of rejecting
  the whole dataset when any close is non-positive.
- The built-in `yfinance` daily lookback still uses `period="10d"`,
  `interval="1d"`. The previous close is the latest valid daily close strictly
  before the quote bar's exchange-local session date: a pre-market quote on a
  new day uses the previous regular day's close, and an after-hours quote on the
  same day uses the previous day's close.
- `generate_chart()` now takes `(ticker, intraday)` — the per-day change
  percentage no longer selects the line color — and rejects an empty frame
  explicitly.
- `README.md` was updated to describe the extended-hours quote, the dark
  same-session chart, the data-selection pipeline, graceful chart degradation
  and the expanded test coverage.

### Fixed

- The separate 1-day/5-minute chart request was eliminated: the chart is now
  aggregated from the same extended-hours frame as the quote, so there is no
  chart request left whose failure could discard a valid quote. If aggregation
  fails, the quote is still sent, without an image.
- Invalid trailing rows (NaN/zero/negative) no longer cause the command to
  report missing data when an earlier valid 1-minute bar exists.
- A net-positive trading day can now show red falling segments (and vice versa)
  instead of a single misleading all-day color.

### Commits merged

| Commit | Summary |
| --- | --- |
| `5bb85ca` | Quote latest price from 1-minute extended-hours data; keep the quote separate from the chart fetch |
| `7642023` | Keep a valid quote when the optional chart fetch fails; select the last valid 1-minute bar |
| `b2e8e6f` | Dark extended-hours chart with per-segment green/red/gray colors |
| `a995e94` | Chart the latest quote's own exchange-local session, including weekends |

### Behavior notes and limitations

- Data comes from **yfinance**, an unofficial Yahoo Finance interface. Quotes
  may be delayed, extended-hours availability varies by ticker, and neither
  real-time pricing nor continuous data is guaranteed. The bot labels the data
  as delayed / latest available and never claims it is real-time.
- The chart is a 5-minute aggregation of the 1-minute extended-hours data, not
  Yahoo's native 1-day/5-minute chart endpoint. A 5-minute bin uses its last
  valid close, so intra-bin movement is not shown.
- If no 1-minute extended-hours data exists, the command returns a friendly
  error; there is deliberately no stale 5-minute regular-session fallback.
- Only one bot process should use a given Discord token at a time; the server
  deployment restarts `stkbot.service` automatically after a push to `main`.

### Baseline already on `main` before this merge

These behaviors existed at `c68ab9d` and are not part of this merge's changes:

- The `/price` slash command, per-server command registration and startup sync
  logging.
- A regular-hours 1-day/5-minute chart colored green/red by the day's change,
  and regular-hours 5-minute quote selection.
- Previous-close selection from a 10-day daily lookback with
  `info.previousClose` fallback.
- Error handling, executor-based non-blocking fetches/chart rendering, chart
  buffer cleanup, and the GitHub Actions self-hosted deployment workflow.
