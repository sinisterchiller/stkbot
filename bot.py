"""Discord stock price bot with /price and one-shot price alerts.

Slash commands: /price, /remind, /reminders, /unremind. A background task
checks pending alerts once every 60 seconds and notifies their owners in the
original guild channel; alerts persist in a local SQLite database.
"""

import asyncio
import math
import os
import re
from dataclasses import dataclass
from datetime import date, tzinfo
from io import BytesIO
from typing import Optional

import discord
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf
from matplotlib.collections import LineCollection
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from reminders import ReminderStore

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

EXTENDED_PERIOD = "5d"
EXTENDED_INTERVAL = "1m"
DAILY_PERIOD = "10d"
DAILY_INTERVAL = "1d"
CHART_INTERVAL = "5min"

# Chart colours are set on the figure and axes instead of through global
# matplotlib styles, so importing this module never changes a user's rcParams.
CHART_BACKGROUND = "#12161c"
CHART_GRID = "#2c3440"
CHART_TEXT = "#e6edf3"
CHART_RISING = "#2ecc71"
CHART_FALLING = "#e74c3c"
CHART_FLAT = "#9aa0a6"

# Tickers may contain letters, digits, dots, dashes, carets and equals signs
# (e.g. BRK-B, ^GSPC, EURUSD=X). Rejecting everything else also keeps the
# ticker safe for embed text and attachment filenames.
TICKER_PATTERN = re.compile(r"^[A-Z0-9^][A-Z0-9.\-^=]{0,14}$")

SYNC_TROUBLESHOOTING = (
    "  - Re-invite the bot with both the `bot` and `applications.commands` scopes "
    "(Developer Portal -> OAuth2 -> URL Generator).\n"
    "  - Confirm DISCORD_TOKEN belongs to the application you invited.\n"
    "  - Confirm the bot is a member of the target server and that slash commands "
    "are enabled for it there (Server Settings -> Integrations).\n"
    "  - Guild commands appear instantly; global commands can take up to an hour "
    "to appear. The bot never deletes existing global registrations.\n"
    "  - Restart your Discord client (Ctrl/Cmd+R) and check you are in the right server."
)

# A guild sync is retried a few times with a fixed delay before the guild is
# left unsynced for the next ready pass (reconnect or restart) to retry.
GUILD_SYNC_ATTEMPTS = 3
GUILD_SYNC_RETRY_DELAY = 5.0

# Seconds between price-alert checks. discord.py does not auto-start ext.tasks
# loops, so this loop is started exactly once in StockPriceBot.setup_hook.
ALERT_CHECK_SECONDS = 60

# Discord caps an embed at 25 fields; one field per reminder keeps /reminders
# readable and chunked safely.
REMINDER_FIELDS_PER_EMBED = 25


def parse_guild_id(raw: Optional[str]) -> Optional[int]:
    """
    Parse DISCORD_GUILD_ID.

    Returns None when the setting is unset or blank, and raises ValueError with a
    user-facing message when it is not a numeric Discord server ID.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if not text.isdigit() or int(text) == 0:
        raise ValueError(
            f"DISCORD_GUILD_ID must be a numeric Discord server ID, got {text!r}. "
            "Enable Developer Mode in Discord, right-click the server, and use "
            "Copy Server ID (leave the value empty for global commands)."
        )
    return int(text)


def get_guild_id() -> Optional[int]:
    """Return the optional development guild ID from the environment."""
    return parse_guild_id(os.getenv("DISCORD_GUILD_ID"))


def _command_names(commands) -> str:
    names = sorted(command.name for command in commands)
    return ", ".join(names) if names else "(none)"


def _guild_label(guild) -> str:
    """Identify a guild in logs by ID, with its name when it is cached."""
    name = getattr(guild, "name", None)
    return f"{guild.id} ({name})" if name else str(guild.id)


def _guild_id_list(guild_ids) -> str:
    return ", ".join(str(guild_id) for guild_id in guild_ids) if guild_ids else "(none)"


async def _sync_global_commands(tree: app_commands.CommandTree) -> None:
    """Register every slash command globally (default registration mode)."""
    try:
        synced = await tree.sync()
    except Exception as exc:
        print(f"ERROR: global slash command sync failed: {exc}")
        print("The bot is online, but /price may be missing or stale in servers.")
        print(SYNC_TROUBLESHOOTING)
        return
    print(f"Synced {len(synced)} global command(s): {_command_names(synced)}")
    print(
        "Global commands can take up to an hour to appear. Every server the bot "
        "is in also receives an instant guild copy once the bot is ready."
    )


async def _sync_guild_commands(tree: app_commands.CommandTree, guild) -> bool:
    """
    Register every global command in one guild and report whether it worked.

    Failures are logged and returned instead of raised, so a guild the bot
    cannot register in never stops the remaining guilds from syncing. Existing
    global registrations are never cleared.
    """
    try:
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
    except Exception as exc:
        print(f"ERROR: slash command sync to guild {_guild_label(guild)} failed: {exc}")
        return False
    print(
        f"Synced {len(synced)} command(s) to guild {_guild_label(guild)}: "
        f"{_command_names(synced)}"
    )
    return True


async def sync_guild_commands(
    tree: app_commands.CommandTree,
    guild,
    *,
    attempts: Optional[int] = None,
    retry_delay: Optional[float] = None,
) -> bool:
    """
    Sync commands into one guild, retrying transient failures with a delay.

    Retry policy defaults to GUILD_SYNC_ATTEMPTS and GUILD_SYNC_RETRY_DELAY.
    Only a successful sync counts as registered; a guild that keeps failing is
    reported as unsynced so a later ready pass retries it.
    """
    attempts = GUILD_SYNC_ATTEMPTS if attempts is None else attempts
    retry_delay = GUILD_SYNC_RETRY_DELAY if retry_delay is None else retry_delay
    for attempt in range(1, attempts + 1):
        if await _sync_guild_commands(tree, guild):
            return True
        if attempt < attempts:
            print(
                f"Retrying slash command sync to guild {_guild_label(guild)} in "
                f"{retry_delay:g}s (attempt {attempt + 1}/{attempts})."
            )
            await asyncio.sleep(retry_delay)
    print(
        f"ERROR: slash command sync to guild {_guild_label(guild)} failed after "
        f"{attempts} attempt(s)."
    )
    print(SYNC_TROUBLESHOOTING)
    return False


async def sync_commands(tree: app_commands.CommandTree) -> bool:
    """
    Register slash commands once at startup.

    Global registration stays the default. An explicit DISCORD_GUILD_ID keeps
    the previous behaviour of registering that one guild before the gateway
    connects; every guild is still registered automatically once ready.
    Returns True when the attempted startup registration succeeded.
    """
    guild_id = get_guild_id()
    if guild_id is None:
        await _sync_global_commands(tree)
        return True
    synced = await sync_guild_commands(tree, discord.Object(id=guild_id), attempts=1)

    # The explicit guild sync only shortens the wait for one server. Global
    # registrations are intentionally left untouched so installed servers keep
    # working; deleting them here would break every other server.
    print(
        "Existing global command registrations were left unchanged; they can "
        "take up to an hour to appear in other servers."
    )
    return synced


class StockPriceBot(commands.Bot):
    """Bot that registers slash commands globally and in every guild it is in."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._synced_guild_ids: set[int] = set()
        self._syncing_guild_ids: set[int] = set()
        self._guild_sync_lock = asyncio.Lock()
        self._price_alerts_started = False

    async def setup_hook(self) -> None:
        guild_id = get_guild_id()
        if await sync_commands(self.tree) and guild_id is not None:
            self._synced_guild_ids.add(guild_id)
        # setup_hook runs once per process (reconnects only fire on_ready),
        # so this guard keeps the alert loop from ever being started twice.
        if not self._price_alerts_started:
            self._price_alerts_started = True
            price_alerts_loop.start()

    async def sync_guild(self, guild) -> bool:
        """
        Register commands in one guild at most once per membership.

        Returns True when the guild is registered or another task is already
        registering it, and False when the sync failed. A failed guild is not
        recorded, so a later ready pass retries it.
        """
        async with self._guild_sync_lock:
            if (
                guild.id in self._synced_guild_ids
                or guild.id in self._syncing_guild_ids
            ):
                return True
            self._syncing_guild_ids.add(guild.id)

        synced = False
        try:
            synced = await sync_guild_commands(self.tree, guild)
            return synced
        finally:
            async with self._guild_sync_lock:
                self._syncing_guild_ids.discard(guild.id)
                if synced:
                    self._synced_guild_ids.add(guild.id)

    async def sync_guilds(self, guilds) -> None:
        """
        Register commands in every guild that is not registered yet.

        Ready fires again after a reconnect and guild joins can race with it;
        already registered guilds are skipped, so no REST sync is repeated.
        """
        before = set(self._synced_guild_ids)
        failed: list[int] = []
        for guild in guilds:
            if not await self.sync_guild(guild):
                failed.append(guild.id)

        registered = sorted(self._synced_guild_ids - before)
        if registered:
            print(
                f"Registered slash commands in {len(registered)} guild(s): "
                f"{_guild_id_list(registered)}"
            )
        if failed:
            print(
                f"WARNING: slash commands are still missing in {len(failed)} "
                f"guild(s): {_guild_id_list(failed)}. They will be retried on the "
                "next gateway reconnect or bot restart."
            )

    def forget_guild(self, guild_id: int) -> None:
        """Drop sync state so rejoining the guild registers commands again."""
        self._synced_guild_ids.discard(guild_id)


intents = discord.Intents.default()
bot = StockPriceBot(command_prefix="!", intents=intents)


@dataclass(frozen=True)
class StockQuote:
    """Sanitized quote data reused by both the embed and the chart."""

    ticker: str
    company_name: Optional[str]
    latest_price: float
    previous_close: float
    change: float
    change_pct: float
    timestamp_label: str
    # Aggregated 5-minute closes for the quote's exchange-local session,
    # spanning pre-market, regular trading and after-hours. None when the
    # chart cannot be derived from the extended-hours data.
    intraday: Optional[pd.DataFrame]


def _positive_finite(value) -> Optional[float]:
    """Return value as a positive finite float, or None when unusable."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _is_finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _safe_info(ticker: yf.Ticker) -> dict:
    """Read ticker.info without letting info failures invalidate history."""
    try:
        info = ticker.info
    except Exception:
        return {}
    return info if isinstance(info, dict) else {}


def _exchange_timezone(info: dict) -> Optional[tzinfo]:
    name = info.get("exchangeTimezoneName")
    if not isinstance(name, str) or not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _sanitize_intraday(hist) -> Optional[pd.DataFrame]:
    """Return a chronologically sorted close-price frame with valid rows only."""
    if hist is None or getattr(hist, "empty", True) or "Close" not in hist.columns:
        return None

    frame = hist.loc[:, ["Close"]].copy()
    closes = pd.to_numeric(frame["Close"], errors="coerce")
    valid = closes.map(_is_finite) & (closes > 0)
    frame = frame[valid]
    if frame.empty:
        return None

    try:
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    except TypeError:
        return None
    return frame if not frame.empty else None


def _previous_close(
    ticker: yf.Ticker,
    session_date: date,
    session_tz: Optional[tzinfo],
) -> Optional[float]:
    """Latest valid daily close strictly before the intraday session date."""
    try:
        daily = ticker.history(
            period=DAILY_PERIOD, interval=DAILY_INTERVAL, auto_adjust=False
        )
    except Exception:
        return None

    if daily is None or getattr(daily, "empty", True) or "Close" not in daily.columns:
        return None

    closes = pd.to_numeric(daily["Close"], errors="coerce").tolist()
    for index_value, raw_close in reversed(list(zip(daily.index, closes))):
        try:
            row_date = index_value.date()
            if session_tz is not None and index_value.tzinfo is not None:
                row_date = index_value.astimezone(session_tz).date()
        except (AttributeError, ValueError, TypeError):
            continue

        if row_date >= session_date:
            continue

        close = _positive_finite(raw_close)
        if close is not None:
            return close

    return None


def _session_chart_frame(
    extended: pd.DataFrame,
    latest_ts,
    latest_price: float,
    display_tz: Optional[tzinfo],
) -> Optional[pd.DataFrame]:
    """
    Aggregate extended-hours 1-minute closes into the quote's 5-minute session.

    The charted session is the exchange-local date of the latest quote bar,
    whatever weekday it is, so a valid weekend quote keeps its own bars instead
    of being replaced by an earlier weekday session. When the requested day has
    no new bars the latest quote bar is itself the last available bar, so its
    date is already the last session with data. Each 5-minute bin keeps its
    last valid close, which yields the same 1-day/5-minute view Yahoo Finance
    serves for regular hours but extended to pre-market and after-hours. When
    the quote sits inside a partial final bin, the raw quote point is appended
    so the chart ends at the real quote timestamp rather than at the bin's left
    edge.
    """
    if extended.empty:
        return None

    try:
        if getattr(latest_ts, "tzinfo", None) is not None and display_tz is not None:
            session_date = latest_ts.astimezone(display_tz).date()
        else:
            session_date = latest_ts.date()
    except (AttributeError, TypeError, ValueError):
        return None

    session = extended
    if getattr(session.index, "tz", None) is not None and display_tz is not None:
        session = session.tz_convert(display_tz)
    session = session[[day == session_date for day in session.index.date]]
    if session.empty:
        return None

    try:
        closes = session["Close"].resample(CHART_INTERVAL).last().dropna()
    except (TypeError, ValueError):
        return None
    closes = closes[closes > 0]
    if closes.empty:
        return None

    quote_ts = latest_ts
    chart_tz = getattr(closes.index, "tz", None)
    if getattr(quote_ts, "tzinfo", None) is not None:
        if chart_tz is None:
            return None
        quote_ts = quote_ts.tz_convert(chart_tz)

    if quote_ts.date() == session_date:
        last_bucket = closes.index[-1]
        if quote_ts > last_bucket:
            closes.loc[quote_ts] = latest_price
        else:
            closes.iloc[-1] = latest_price

    return closes.sort_index().to_frame("Close")


def _format_timestamp(ts, display_tz: Optional[tzinfo]) -> str:
    """Format a data timestamp without falsely claiming a naive time is UTC."""
    if getattr(ts, "tzinfo", None) is not None:
        local_ts = ts.astimezone(display_tz) if display_tz is not None else ts
        return local_ts.strftime("%Y-%m-%d %H:%M:%S %Z")

    if display_tz is not None:
        # yfinance intraday indexes are normally tz-aware; if one is naive it
        # is exchange-local time, so label it with the exchange timezone.
        return ts.replace(tzinfo=display_tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    return ts.strftime("%Y-%m-%d %H:%M:%S")


def fetch_quote(ticker: str) -> Optional[StockQuote]:
    """
    Fetch the latest valid 1-minute extended-hours bar for a ticker.

    The displayed price and timestamp come from a 5-day/1-minute
    ``prepost=True`` fetch that spans pre-market, regular and after-hours
    sessions; the chosen bar is the last chronologically sorted bar with a
    finite positive close, and its own timestamp is the quote timestamp. The
    chart is aggregated from that same fetch (no second chart request): the
    quote's exchange-local session is resampled into 5-minute bins and the raw
    quote point is preserved. If the chart cannot be derived the quote is
    returned without one. Daily history is only used to find the previous
    trading close, with ``info.previousClose`` as a fallback.
    """
    try:
        ticker_client = yf.Ticker(ticker)
        extended_hist = ticker_client.history(
            period=EXTENDED_PERIOD,
            interval=EXTENDED_INTERVAL,
            prepost=True,
            auto_adjust=False,
        )
    except Exception:
        return None

    extended = _sanitize_intraday(extended_hist)
    if extended is None:
        return None

    latest_price = _positive_finite(extended["Close"].iloc[-1])
    if latest_price is None:
        return None

    info = _safe_info(ticker_client)
    latest_ts = extended.index[-1]
    exchange_tz = _exchange_timezone(info)

    if getattr(latest_ts, "tzinfo", None) is not None:
        display_tz = exchange_tz or latest_ts.tzinfo
        session_date = latest_ts.astimezone(display_tz).date()
    else:
        display_tz = exchange_tz
        session_date = latest_ts.date()

    previous_close = _previous_close(ticker_client, session_date, display_tz)
    if previous_close is None:
        previous_close = _positive_finite(info.get("previousClose"))
    if previous_close is None:
        return None

    change = latest_price - previous_close
    change_pct = change / previous_close * 100.0
    if not math.isfinite(change) or not math.isfinite(change_pct):
        return None

    # The chart is optional: an aggregation failure must not invalidate the
    # quote that was already selected from the same fetch.
    try:
        intraday = _session_chart_frame(
            extended, latest_ts, latest_price, display_tz
        )
    except Exception:
        intraday = None

    company_name = info.get("longName") or info.get("shortName")
    if not isinstance(company_name, str) or not company_name.strip():
        company_name = None

    return StockQuote(
        ticker=ticker,
        company_name=company_name,
        latest_price=latest_price,
        previous_close=previous_close,
        change=change,
        change_pct=change_pct,
        timestamp_label=_format_timestamp(latest_ts, display_tz),
        intraday=intraday,
    )


@dataclass(frozen=True)
class LatestPrice:
    """Lightweight quote result: the last valid bar and its timestamp."""

    price: float
    timestamp: object


def fetch_latest_price(ticker: str) -> Optional[LatestPrice]:
    """
    Lightweight latest-price lookup for /remind and the alert checker.

    Exactly one 5-day/1-minute extended-hours history fetch is made (no
    ``info``, no daily history, no chart request). The quote is the last
    chronologically valid finite positive close selected by the existing
    sanitizer, paired with that bar's own timestamp. Returns None on a
    Yahoo Finance error, empty data, or no usable bar. Synchronous and
    blocking: callers must run it via ``asyncio.to_thread``.
    """
    try:
        client = yf.Ticker(ticker)
        extended_hist = client.history(
            period=EXTENDED_PERIOD,
            interval=EXTENDED_INTERVAL,
            prepost=True,
            auto_adjust=False,
        )
    except Exception:
        return None

    extended = _sanitize_intraday(extended_hist)
    if extended is None:
        return None

    price = _positive_finite(extended["Close"].iloc[-1])
    if price is None:
        return None

    return LatestPrice(price=price, timestamp=extended.index[-1])


def _chart_time_label(ts) -> str:
    """Format a chart timestamp in its own exchange-local timezone."""
    if getattr(ts, "tzinfo", None) is not None:
        return ts.strftime("%Y-%m-%d %H:%M %Z")
    return ts.strftime("%Y-%m-%d %H:%M")


def generate_chart(ticker: str, intraday: pd.DataFrame) -> BytesIO:
    """Render an extended-hours session frame as a dark in-memory PNG."""
    if intraday is None or intraday.empty or "Close" not in intraday.columns:
        raise ValueError("chart frame must contain at least one close")

    fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
    buffer = BytesIO()
    try:
        fig.patch.set_facecolor(CHART_BACKGROUND)
        ax.set_facecolor(CHART_BACKGROUND)

        closes = intraday["Close"].to_numpy(dtype=float)
        elapsed = (intraday.index - intraday.index[0]) / pd.Timedelta(minutes=1)
        positions = np.asarray(elapsed, dtype=float)

        if len(closes) == 1:
            ax.plot(
                positions,
                closes,
                linestyle="none",
                marker="o",
                markersize=5,
                color=CHART_FLAT,
            )
            ax.set_xlim(-1.0, 1.0)
        else:
            points = np.column_stack([positions, closes])
            segments = np.stack([points[:-1], points[1:]], axis=1)
            changes = np.diff(closes)
            colors = np.where(
                changes > 0,
                CHART_RISING,
                np.where(changes < 0, CHART_FALLING, CHART_FLAT),
            )
            ax.add_collection(
                LineCollection(segments, colors=colors, linewidths=1.6, zorder=3)
            )
            ax.scatter(
                [positions[-1]],
                [closes[-1]],
                s=22,
                color=colors[-1],
                edgecolors=CHART_BACKGROUND,
                linewidths=0.6,
                zorder=4,
            )
            x_span = positions[-1] - positions[0]
            x_pad = max(x_span * 0.02, 0.5)
            ax.set_xlim(positions[0] - x_pad, positions[-1] + x_pad)

        low, high = float(np.min(closes)), float(np.max(closes))
        y_span = high - low
        y_pad = y_span * 0.12 if y_span > 0 else max(abs(high) * 0.01, 0.5)
        ax.set_ylim(low - y_pad, high + y_pad)

        tick_count = min(6, len(closes))
        tick_indices = np.unique(
            np.linspace(0, len(closes) - 1, tick_count).astype(int)
        )
        ax.set_xticks(positions[tick_indices])
        ax.set_xticklabels(
            [intraday.index[i].strftime("%H:%M") for i in tick_indices]
        )

        ax.set_title(
            f"{ticker} — 1D · 5-min · extended hours (pre/regular/after)",
            fontsize=11,
            fontweight="bold",
            color=CHART_TEXT,
            pad=10,
        )
        ax.set_xlabel("Exchange-local session time", fontsize=9, color=CHART_TEXT)
        ax.set_ylabel("Price", fontsize=9, color=CHART_TEXT)
        ax.tick_params(axis="both", colors=CHART_TEXT, labelsize=8)
        for spine in ax.spines.values():
            spine.set_color(CHART_GRID)
        ax.grid(True, color=CHART_GRID, linewidth=0.6, alpha=0.5)
        ax.set_axisbelow(True)

        ax.annotate(
            f"Last ${closes[-1]:.2f}\n{_chart_time_label(intraday.index[-1])}",
            xy=(positions[-1], closes[-1]),
            xytext=(-10, 10),
            textcoords="offset points",
            ha="right",
            va="bottom",
            fontsize=8,
            color=CHART_TEXT,
            bbox=dict(
                boxstyle="round,pad=0.25",
                facecolor=CHART_BACKGROUND,
                edgecolor=CHART_GRID,
            ),
        )

        fig.tight_layout()
        fig.savefig(
            buffer,
            format="png",
            bbox_inches="tight",
            facecolor=fig.get_facecolor(),
            edgecolor="none",
        )
        buffer.seek(0)
        return buffer
    except Exception:
        buffer.close()
        raise
    finally:
        plt.close(fig)


def _build_embed(quote: StockQuote) -> discord.Embed:
    sign = "+" if quote.change >= 0 else "-"
    change_str = f"{sign}${abs(quote.change):.2f} ({quote.change_pct:+.2f}%)"
    company_name = discord.utils.escape_markdown(quote.company_name or quote.ticker)

    embed = discord.Embed(
        title=f"{quote.ticker} — {company_name}",
        description=f"# ${quote.latest_price:.2f} {change_str}",
        color=discord.Color.green() if quote.change_pct >= 0 else discord.Color.red(),
    )
    embed.add_field(
        name="Data",
        value=(
            f"Latest: ${quote.latest_price:.2f}\n"
            f"Previous Close: ${quote.previous_close:.2f}\n"
            f"Change vs prev close: {change_str}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Timestamp",
        value=f"{quote.timestamp_label}\n*Delayed / latest available*",
        inline=True,
    )
    if quote.intraday is not None:
        embed.set_footer(
            text=(
                "Quote: 5d · 1-minute extended hours · Chart: same-session 5-minute "
                "bins (pre-market → after-hours) · Delayed, not guaranteed real-time"
            )
        )
    else:
        embed.set_footer(
            text=(
                "Quote: 5d · 1-minute extended hours · No extended-hours chart "
                "available · Delayed, not guaranteed real-time"
            )
        )
    return embed


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # setup_hook runs before the gateway connects, so this is the first point
    # where the guild list is cached. Guilds synced in setup_hook are skipped.
    await bot.sync_guilds(bot.guilds)


@bot.event
async def on_guild_join(guild):
    print(f"Joined guild {_guild_label(guild)}; registering slash commands.")
    if not await bot.sync_guild(guild):
        print(
            "WARNING: slash commands are not registered in guild "
            f"{_guild_label(guild)}. They will be retried on the next gateway "
            "reconnect or bot restart."
        )


@bot.event
async def on_guild_remove(guild):
    # Discord drops guild command registrations when the bot leaves, so a
    # rejoin has to register the guild copy again from scratch.
    bot.forget_guild(guild.id)


@bot.event
async def on_close():
    # discord.py does not cancel ext.tasks loops on close, so stop the alert
    # checker cleanly when the bot shuts down.
    price_alerts_loop.cancel()


@bot.tree.command(
    name="price",
    description="Latest stock price (extended hours) with 1D 5-min extended-hours chart",
)
@app_commands.describe(ticker="Stock ticker symbol (e.g., NVDA)")
async def price(interaction: discord.Interaction, ticker: str):
    """Slash command to fetch and display stock price with chart."""
    normalized = ticker.strip().upper()
    await interaction.response.defer(thinking=True)

    if not TICKER_PATTERN.fullmatch(normalized):
        await interaction.followup.send(
            f"❌ **{discord.utils.escape_markdown(normalized) or 'Input'}** is not a valid ticker symbol. "
            "Try something like `NVDA`, `BRK-B`, or `^GSPC`.",
            ephemeral=True,
        )
        return

    loop = asyncio.get_running_loop()
    quote = await loop.run_in_executor(None, fetch_quote, normalized)
    if quote is None:
        await interaction.followup.send(
            f"❌ Could not fetch data for **{normalized}**. Invalid symbol, no market data, "
            "or a temporary Yahoo Finance error. Try again shortly.",
            ephemeral=True,
        )
        return

    chart_buffer: Optional[BytesIO] = None
    chart_file: Optional[discord.File] = None
    try:
        embed = _build_embed(quote)
        if quote.intraday is not None:
            chart_buffer = await loop.run_in_executor(
                None, generate_chart, quote.ticker, quote.intraday
            )
            filename = f"{quote.ticker}_chart.png"
            embed.set_image(url=f"attachment://{filename}")
            chart_file = discord.File(chart_buffer, filename=filename)
        await interaction.followup.send(embed=embed, file=chart_file)
    except Exception:
        try:
            await interaction.followup.send(
                f"❌ Could not build or send the chart for **{normalized}**. "
                "Please try again shortly.",
                ephemeral=True,
            )
        except Exception:
            pass
    finally:
        # discord.File stubs out fp.close(), so restore it before closing the
        # buffer we own.
        if chart_file is not None:
            chart_file.close()
        if chart_buffer is not None:
            chart_buffer.close()


# ---------------------------------------------------------------------------
# Price alerts: /remind, /reminders, /unremind and the 60-second checker.

reminder_store = ReminderStore()


def _build_remind_embed(
    reminder_id: int,
    ticker: str,
    direction: str,
    target_price: float,
    quote: LatestPrice,
) -> discord.Embed:
    """Ephemeral confirmation embed for a newly created price alert."""
    direction_text = "rising above" if direction == "above" else "falling below"
    embed = discord.Embed(
        title=f"Price alert #{reminder_id} set",
        description=(
            f"**{ticker}** {direction_text} **${target_price:.2f}**\n"
            f"Current: ${quote.price:.2f}\n"
            f"Quote as of {_format_timestamp(quote.timestamp, None)} — "
            "delayed / latest available, not real-time"
        ),
        color=discord.Color.green() if direction == "above" else discord.Color.red(),
    )
    embed.add_field(name="ID", value=str(reminder_id), inline=True)
    embed.add_field(name="Ticker", value=ticker, inline=True)
    embed.add_field(name="Target", value=f"${target_price:.2f}", inline=True)
    embed.add_field(name="Current", value=f"${quote.price:.2f}", inline=True)
    embed.add_field(name="Direction", value=direction, inline=True)
    return embed


def _build_reminder_embeds(rows: list[dict]) -> list[discord.Embed]:
    """One embed per chunk of at most REMINDER_FIELDS_PER_EMBED reminders.

    Chunking keeps /reminders inside Discord's 25-field-per-embed limit.
    """
    embeds: list[discord.Embed] = []
    for start in range(0, len(rows), REMINDER_FIELDS_PER_EMBED):
        embed = discord.Embed(
            title="Your pending price alerts",
            color=discord.Color.blue(),
        )
        for row in rows[start : start + REMINDER_FIELDS_PER_EMBED]:
            embed.add_field(
                name=f"#{row['id']} {row['ticker']}",
                value=f"${row['target_price']:.2f} ({row['direction']})",
                inline=True,
            )
        embeds.append(embed)
    return embeds


def _alert_message(row: dict, quote: LatestPrice) -> str:
    """Alert text mentioning the owner; user mentions only, delayed caveat."""
    direction_text = "rose above" if row["direction"] == "above" else "fell below"
    return (
        f"<@{row['user_id']}> 🔔 **{row['ticker']}** {direction_text} its target: "
        f"latest ${quote.price:.2f} vs target ${row['target_price']:.2f}.\n"
        f"Quote as of {_format_timestamp(quote.timestamp, None)} — "
        "delayed / latest available, not real-time."
    )


async def _resolve_alert_channel(channel_id: int):
    """Cached channel lookup first, then a REST fetch; None on any failure."""
    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
    except Exception:
        return None
    return channel


async def check_price_alerts() -> None:
    """
    Run one 60-second alert cycle.

    Loads every pending reminder, fetches one lightweight quote per unique
    ticker, and for each due reminder (price >= target for 'above',
    <= target for 'below') sends a user-mentioning message to the original
    guild channel. The reminder is deleted only after a successful send;
    any channel lookup, quote fetch, or send failure retains it for the
    next cycle. DM channels never receive alerts.
    """
    pending = await asyncio.to_thread(reminder_store.list_all)
    if not pending:
        return

    unique_tickers = {row["ticker"] for row in pending}
    quotes: dict[str, Optional[LatestPrice]] = {}
    for ticker in sorted(unique_tickers):
        try:
            quotes[ticker] = await asyncio.to_thread(fetch_latest_price, ticker)
        except Exception as exc:
            print(f"WARNING: quote fetch failed for {ticker}: {type(exc).__name__}")
            quotes[ticker] = None

    for row in pending:
        quote = quotes.get(row["ticker"])
        if quote is None:
            # Yahoo unavailable for this ticker; keep the alert for next cycle.
            continue

        target = row["target_price"]
        if row["direction"] == "above":
            due = quote.price >= target
        else:
            due = quote.price <= target
        if not due:
            # Stale closed-market quotes persist, and a direction set away
            # from the current price never fires on that stale value.
            continue

        channel = await _resolve_alert_channel(row["channel_id"])
        if channel is None or getattr(channel, "guild", None) is None:
            print(
                f"WARNING: alert #{row['id']} ({row['ticker']}) retained: "
                "channel unavailable or not a guild channel."
            )
            continue

        try:
            await channel.send(
                _alert_message(row, quote),
                allowed_mentions=discord.AllowedMentions(users=True),
            )
        except Exception as exc:
            print(
                f"WARNING: alert #{row['id']} ({row['ticker']}) send failed: "
                f"{type(exc).__name__}"
            )
            continue

        deleted = await asyncio.to_thread(reminder_store.delete_by_id_only, row["id"])
        if not deleted:
            print(
                f"WARNING: alert #{row['id']} sent but delete failed; it may be "
                "re-sent on the next cycle."
            )


@tasks.loop(seconds=ALERT_CHECK_SECONDS)
async def price_alerts_loop():
    """
    60-second alert cycle with a ready guard.

    Started once in setup_hook; skipped while the bot is not ready, so a
    reconnect can never spawn a second loop. One cycle at a time (the loop
    waits for each cycle to finish), so reminders are never notified twice.
    """
    if not bot.is_ready():
        return
    try:
        await check_price_alerts()
    except Exception as exc:
        print(f"WARNING: price alert cycle failed: {type(exc).__name__}")


@bot.tree.command(
    name="remind",
    description="Set a one-shot price alert for a ticker",
)
@app_commands.describe(ticker="Stock ticker symbol (e.g., NVDA)")
@app_commands.describe(target="Target price to alert on")
async def remind(interaction: discord.Interaction, ticker: str, target: float):
    """Slash command to set a one-shot price alert.

    The direction is derived from the fetched price (target above current
    means 'above', below means 'below'), and a target equal to the current
    price at displayed precision is rejected, so an alert created on a
    stale closed-market quote cannot fire on that same stale value.
    """
    await interaction.response.defer(thinking=True)

    normalized = ticker.strip().upper()
    channel = interaction.channel
    guild = getattr(channel, "guild", None)
    if guild is None:
        await interaction.followup.send(
            "Price alerts only work in server channels, not DMs.",
            ephemeral=True,
        )
        return

    if not TICKER_PATTERN.fullmatch(normalized):
        await interaction.followup.send(
            f"❌ **{discord.utils.escape_markdown(normalized) or 'Input'}** is not a valid ticker symbol. "
            "Try something like `NVDA`, `BRK-B`, or `^GSPC`.",
            ephemeral=True,
        )
        return

    try:
        target_price = float(target)
    except (TypeError, ValueError):
        target_price = None
    if target_price is None or not math.isfinite(target_price) or target_price <= 0:
        await interaction.followup.send(
            "❌ The target price must be a positive number (e.g., `150`).",
            ephemeral=True,
        )
        return

    quote = await asyncio.to_thread(fetch_latest_price, normalized)
    if quote is None:
        await interaction.followup.send(
            f"❌ Could not fetch a latest price for **{normalized}**. Invalid symbol, "
            "no market data, or a temporary Yahoo Finance error. Try again shortly.",
            ephemeral=True,
        )
        return

    if round(quote.price, 2) == round(target_price, 2):
        await interaction.followup.send(
            f"❌ **{normalized}** is already trading at ${target_price:.2f}. "
            "Choose a target away from the current price.",
            ephemeral=True,
        )
        return

    direction = "above" if target_price > quote.price else "below"
    reminder_id = await asyncio.to_thread(
        reminder_store.add_reminder,
        interaction.user.id,
        channel.id,
        normalized,
        target_price,
        direction,
    )
    await interaction.followup.send(
        embed=_build_remind_embed(reminder_id, normalized, direction, target_price, quote),
        ephemeral=True,
    )


@bot.tree.command(name="reminders", description="List your pending price alerts")
async def reminders(interaction: discord.Interaction):
    """Slash command to list the invoking user's pending alerts (ephemeral)."""
    await interaction.response.defer(thinking=True)

    rows = await asyncio.to_thread(reminder_store.list_by_user, interaction.user.id)
    if not rows:
        await interaction.followup.send(
            "You have no pending price alerts. Use /remind to create one.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        embeds=_build_reminder_embeds(rows), ephemeral=True
    )


@bot.tree.command(name="unremind", description="Cancel one of your price alerts by ID")
@app_commands.describe(id="Alert ID shown by /reminders")
async def unremind(interaction: discord.Interaction, id: int):
    """Slash command to cancel one of the invoking user's alerts (owner only)."""
    await interaction.response.defer(thinking=True)

    deleted = await asyncio.to_thread(reminder_store.delete_by_id, id, interaction.user.id)
    if deleted:
        await interaction.followup.send(f"✅ Alert #{id} cancelled.", ephemeral=True)
    else:
        await interaction.followup.send(
            f"❌ No pending alert with ID {id} belongs to you. Check /reminders.",
            ephemeral=True,
        )


def main():
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN not set. Copy .env.example to .env and add your bot token."
        )
    try:
        get_guild_id()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from None
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
