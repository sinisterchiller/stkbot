"""Discord stock price bot with /price slash command."""

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
import pandas as pd
import yfinance as yf
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

INTRADAY_PERIOD = "1d"
INTRADAY_INTERVAL = "5m"
DAILY_PERIOD = "10d"
DAILY_INTERVAL = "1d"

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

    async def setup_hook(self) -> None:
        guild_id = get_guild_id()
        if await sync_commands(self.tree) and guild_id is not None:
            self._synced_guild_ids.add(guild_id)

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
    intraday: pd.DataFrame


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
    finite = closes.map(_is_finite)
    valid_closes = closes[finite].astype(float)
    if len(valid_closes) and (valid_closes <= 0).any():
        return None
    frame = frame[finite]
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
    Fetch and sanitize one intraday history response for a ticker.

    The same returned frame is used for the displayed price, timestamp and
    chart. Daily history is only used to find the previous trading close, with
    ``info.previousClose`` as a fallback.
    """
    try:
        ticker_client = yf.Ticker(ticker)
        intraday_hist = ticker_client.history(
            period=INTRADAY_PERIOD,
            interval=INTRADAY_INTERVAL,
            auto_adjust=False,
        )
    except Exception:
        return None

    intraday = _sanitize_intraday(intraday_hist)
    if intraday is None:
        return None

    latest_price = _positive_finite(intraday["Close"].iloc[-1])
    if latest_price is None:
        return None

    info = _safe_info(ticker_client)
    latest_ts = intraday.index[-1]
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


def generate_chart(
    ticker: str,
    intraday: pd.DataFrame,
    change_pct: float,
) -> BytesIO:
    """Render the sanitized intraday frame as an in-memory PNG."""
    fig, ax = plt.subplots(figsize=(8, 4), dpi=100)
    buffer = BytesIO()
    try:
        color = "#1a9850" if change_pct >= 0 else "#d73027"
        ax.plot(intraday.index, intraday["Close"], color=color, linewidth=1.5)
        ax.set_title(f"{ticker} — 1D · 5-min intervals", fontsize=12, fontweight="bold")
        ax.set_xlabel("Time", fontsize=10)
        ax.set_ylabel("Price", fontsize=10)
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(buffer, format="png", bbox_inches="tight")
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
            f"Change: {change_str}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Timestamp",
        value=f"{quote.timestamp_label}\n*Delayed / latest available*",
        inline=True,
    )
    embed.set_footer(
        text="1D · 5-minute intervals · Regular market hours · Not real-time"
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


@bot.tree.command(name="price", description="Get latest stock price with 1D 5-min chart")
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
        chart_buffer = await loop.run_in_executor(
            None, generate_chart, quote.ticker, quote.intraday, quote.change_pct
        )
        filename = f"{quote.ticker}_chart.png"
        embed = _build_embed(quote)
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
