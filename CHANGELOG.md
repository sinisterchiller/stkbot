# Changelog

All notable changes to the Discord stock price bot. The project has no tagged
releases; this file records what changed on `dev` before it was merged into
`main`.

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
