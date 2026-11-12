"""Tests for the Discord stock price bot."""

import threading
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pytest
import discord
from discord import app_commands

import bot as bot_module
from bot import (
    TICKER_PATTERN,
    fetch_quote,
    generate_chart,
    parse_guild_id,
    price,
    sync_commands,
)


class FakeTicker:
    """Stand-in for yfinance.Ticker with inspectable history calls."""

    def __init__(
        self,
        intraday,
        daily=None,
        info=None,
        *,
        info_error=None,
        intraday_error=None,
        daily_error=None,
    ):
        self.intraday = intraday
        self.daily = daily
        self.info_data = info if info is not None else {}
        self.info_error = info_error
        self.intraday_error = intraday_error
        self.daily_error = daily_error
        self.history_calls = []
        self.history_thread_ids = []

    @property
    def info(self):
        if self.info_error is not None:
            raise self.info_error
        return self.info_data

    def history(self, period, interval, auto_adjust):
        self.history_calls.append((period, interval, auto_adjust))
        self.history_thread_ids.append(threading.get_ident())
        if period == "1d" and interval == "5m":
            if self.intraday_error is not None:
                raise self.intraday_error
            return self.intraday
        if period == "10d" and interval == "1d":
            if self.daily_error is not None:
                raise self.daily_error
            if self.daily is None:
                return pd.DataFrame()
            return self.daily
        raise AssertionError(f"Unexpected history call: {period=} {interval=}")


def intraday_frame(
    start="2024-01-12 14:30",
    periods=18,
    closes=None,
    tz="America/New_York",
):
    if closes is not None:
        periods = len(closes)
    dates = pd.date_range(start=start, periods=periods, freq="5min", tz=tz)
    if closes is None:
        closes = np.linspace(145.0, 148.5, periods)
    return pd.DataFrame({"Close": closes}, index=dates)


def daily_frame(rows, tz="America/New_York"):
    index = pd.DatetimeIndex([pd.Timestamp(day, tz=tz) for day, _ in rows])
    return pd.DataFrame({"Close": [close for _, close in rows]}, index=index)


def make_interaction():
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def png_stub():
    return BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 128)


def sent_embed(interaction):
    return interaction.followup.send.await_args.kwargs["embed"]


def field_value(embed, name):
    return next(field.value for field in embed.fields if field.name == name)


@pytest.mark.parametrize("ticker", ["NVDA", "BRK-B", "^GSPC", "EURUSD=X", "BTC-USD"])
def test_ticker_pattern_accepts_supported_symbols(ticker):
    assert TICKER_PATTERN.fullmatch(ticker)


@pytest.mark.parametrize(
    "ticker", ["", "BAD TICKER", "NVDA!", "NVDA;DROP", "A/B", "**NVDA**"]
)
def test_ticker_pattern_rejects_unsafe_symbols(ticker):
    assert TICKER_PATTERN.fullmatch(ticker) is None


class TestFetchQuote:
    """Tests for fetching and sanitizing one intraday history response."""

    def test_valid_ticker_returns_sanitized_quote(self):
        hist = intraday_frame()
        daily = daily_frame(
            [
                ("2024-01-08", 140.0),
                ("2024-01-09", 141.0),
                ("2024-01-10", 142.0),
                ("2024-01-11", 145.0),
                ("2024-01-12", 147.0),
            ]
        )
        fake = FakeTicker(
            hist,
            daily,
            {"longName": "NVIDIA Corporation", "exchangeTimezoneName": "America/New_York"},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("NVDA")

        assert quote is not None
        assert quote.ticker == "NVDA"
        assert quote.company_name == "NVIDIA Corporation"
        assert quote.latest_price == pytest.approx(148.5)
        assert quote.previous_close == 145.0
        assert quote.change == pytest.approx(3.5)
        assert quote.change_pct == pytest.approx(3.5 / 145.0 * 100)
        assert quote.intraday.equals(hist[["Close"]])
        assert "EST" in quote.timestamp_label
        assert "UTC" not in quote.timestamp_label

    def test_previous_close_skips_same_session_and_invalid_daily_rows(self):
        hist = intraday_frame()
        daily = daily_frame(
            [
                ("2024-01-08", 100.0),
                ("2024-01-09", np.nan),
                ("2024-01-10", 102.0),
                ("2024-01-11", np.nan),
                ("2024-01-12", 999.0),  # same session must not be previous close
            ]
        )
        fake = FakeTicker(hist, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 102.0
        assert quote.change == pytest.approx(46.5)
        assert quote.change_pct == pytest.approx(45.588, rel=0.001)

    def test_daily_lookback_uses_ten_days(self):
        fake = FakeTicker(
            intraday_frame(),
            daily_frame([("2024-01-11", 145.0)]),
            {"longName": "Test Corp"},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            fetch_quote("TEST")

        assert ("10d", "1d", False) in fake.history_calls
        assert len([call for call in fake.history_calls if call[0] == "1d"]) == 1

    def test_closed_session_uses_friday_close_for_weekend_data(self):
        # Monday's intraday session, with Friday as the latest prior daily row
        # and stale weekend rows that must be ignored.
        hist = intraday_frame(start="2024-01-16 09:30", periods=4, closes=[101, 102, 103, 104])
        daily = daily_frame(
            [
                ("2024-01-11", 99.0),
                ("2024-01-12", 100.0),
                ("2024-01-13", np.nan),
                ("2024-01-14", np.nan),
                ("2024-01-15", np.nan),
                ("2024-01-16", 104.0),
            ]
        )
        fake = FakeTicker(hist, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 100.0
        assert quote.change_pct == pytest.approx(4.0)

    def test_info_failure_does_not_invalidate_history(self):
        hist = intraday_frame()
        daily = daily_frame([("2024-01-11", 145.0)])
        fake = FakeTicker(hist, daily, info_error=RuntimeError("info unavailable"))

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.company_name is None
        assert quote.previous_close == 145.0
        assert quote.latest_price == pytest.approx(148.5)

    def test_daily_missing_falls_back_to_info_previous_close(self):
        fake = FakeTicker(
            intraday_frame(closes=[145, 146, 147, 148]),
            pd.DataFrame(),
            {"longName": "Test Corp", "previousClose": 142.0},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 142.0

    def test_daily_exception_falls_back_to_info_previous_close(self):
        fake = FakeTicker(
            intraday_frame(closes=[145, 146, 147, 148]),
            daily_error=RuntimeError("daily history unavailable"),
            info={"longName": "Test Corp", "previousClose": 142.0},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 142.0

    def test_naive_timestamp_uses_exchange_timezone_not_utc(self):
        naive_hist = intraday_frame(tz=None)
        daily = daily_frame([("2024-01-11", 145.0)])
        fake = FakeTicker(
            naive_hist,
            daily,
            {"longName": "Test Corp", "exchangeTimezoneName": "America/New_York"},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert "EST" in quote.timestamp_label
        assert "UTC" not in quote.timestamp_label

    def test_naive_timestamp_without_timezone_does_not_claim_utc(self):
        naive_hist = intraday_frame(tz=None)
        daily = daily_frame([("2024-01-11", 145.0)], tz=None)
        fake = FakeTicker(naive_hist, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert "UTC" not in quote.timestamp_label

    def test_timestamp_converts_utc_data_to_exchange_time(self):
        # 20:55 UTC on 2024-01-12 is 15:55 in New York.
        hist = intraday_frame(
            start="2024-01-12 20:50",
            periods=2,
            closes=[147.0, 148.0],
            tz="UTC",
        )
        daily = daily_frame([("2024-01-11", 145.0)])
        fake = FakeTicker(
            hist,
            daily,
            {"longName": "Test Corp", "exchangeTimezoneName": "America/New_York"},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.timestamp_label == "2024-01-12 15:55:00 EST"

    @pytest.mark.parametrize(
        "hist",
        [
            pd.DataFrame(),
            pd.DataFrame({"Open": [100.0]}, index=pd.date_range("2024-01-12", periods=1, freq="5min", tz="UTC")),
            pd.DataFrame({"Close": [np.nan, np.nan]}, index=pd.date_range("2024-01-12", periods=2, freq="5min", tz="UTC")),
            pd.DataFrame({"Close": [-1.0, 2.0]}, index=pd.date_range("2024-01-12", periods=2, freq="5min", tz="UTC")),
            pd.DataFrame({"Close": [0.0, 2.0]}, index=pd.date_range("2024-01-12", periods=2, freq="5min", tz="UTC")),
            pd.DataFrame(
                {"Close": pd.array([pd.NA, pd.NA], dtype="Float64")},
                index=pd.date_range("2024-01-12", periods=2, freq="5min", tz="UTC"),
            ),
            None,
        ],
    )
    def test_invalid_intraday_data_returns_none(self, hist):
        fake = FakeTicker(hist, daily_frame([("2024-01-11", 100.0)]), {"previousClose": 100.0})

        with patch("bot.yf.Ticker", return_value=fake):
            assert fetch_quote("BAD") is None

    def test_nullable_dtype_with_trailing_na_uses_last_valid_close(self):
        hist = pd.DataFrame(
            {"Close": pd.array([147.0, 148.0, pd.NA], dtype="Float64")},
            index=pd.date_range("2024-01-12", periods=3, freq="5min", tz="America/New_York"),
        )
        fake = FakeTicker(hist, daily_frame([("2024-01-11", 145.0)]), {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.latest_price == 148.0
        assert len(quote.intraday) == 2

    def test_intraday_exception_returns_none(self):
        fake = FakeTicker(None, intraday_error=RuntimeError("network down"))

        with patch("bot.yf.Ticker", return_value=fake):
            assert fetch_quote("ERROR") is None

    def test_ticker_constructor_exception_returns_none(self):
        with patch("bot.yf.Ticker", side_effect=RuntimeError("network down")):
            assert fetch_quote("ERROR") is None

    @pytest.mark.parametrize("previous_close", [None, np.nan, 0.0, -5.0, "invalid"])
    def test_invalid_previous_close_returns_none(self, previous_close):
        fake = FakeTicker(
            intraday_frame(),
            pd.DataFrame(),
            {"longName": "Test Corp", "previousClose": previous_close},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            assert fetch_quote("TEST") is None


class TestPriceCommand:
    """Exercise the real slash-command callback with a mocked interaction."""

    @pytest.mark.asyncio
    async def test_success_defers_once_and_reuses_one_intraday_fetch(self):
        hist = intraday_frame()
        daily = daily_frame(
            [
                ("2024-01-08", 140.0),
                ("2024-01-09", 141.0),
                ("2024-01-10", 142.0),
                ("2024-01-11", 145.0),
                ("2024-01-12", 147.0),
            ]
        )
        fake = FakeTicker(
            hist,
            daily,
            {"longName": "NVIDIA Corporation", "exchangeTimezoneName": "America/New_York"},
        )
        interaction = make_interaction()
        chart_buffer = png_stub()

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot.generate_chart", return_value=chart_buffer
        ) as chart_mock:
            await price.callback(interaction, " nvda ")

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.followup.send.assert_awaited_once()

        embed = sent_embed(interaction)
        assert embed.title == "NVDA — NVIDIA Corporation"
        assert embed.description == "# $148.50 +$3.50 (+2.41%)"
        assert field_value(embed, "Data") == (
            "Latest: $148.50\nPrevious Close: $145.00\nChange: +$3.50 (+2.41%)"
        )
        assert "EST" in field_value(embed, "Timestamp")
        assert "Delayed / latest available" in field_value(embed, "Timestamp")
        assert embed.color == discord.Color.green()
        assert embed.image.url == "attachment://NVDA_chart.png"
        assert sent_embed(interaction).footer.text.endswith("Not real-time")

        assert len([call for call in fake.history_calls if call[0] == "1d"]) == 1
        assert len([call for call in fake.history_calls if call[0] == "10d"]) == 1
        assert fake.history_thread_ids[0] != threading.get_ident()

        chart_mock.assert_called_once()
        chart_ticker, chart_frame, chart_change_pct = chart_mock.call_args.args
        assert chart_ticker == "NVDA"
        assert chart_frame is not hist  # sanitized copy, not the raw response
        assert chart_frame["Close"].iloc[-1] == pytest.approx(148.5)
        assert chart_frame.index.tolist() == hist.index.tolist()
        assert chart_change_pct == pytest.approx(3.5 / 145.0 * 100)
        assert interaction.followup.send.await_args.kwargs["file"].filename == "NVDA_chart.png"
        assert chart_buffer.closed

    @pytest.mark.asyncio
    async def test_closed_market_session_uses_latest_prior_valid_close(self):
        hist = intraday_frame(start="2024-01-12 14:30", periods=4, closes=[103, 104, 105, 106])
        daily = daily_frame(
            [
                ("2024-01-09", 100.0),
                ("2024-01-10", 101.0),
                ("2024-01-11", np.nan),
                ("2024-01-12", 999.0),
            ]
        )
        fake = FakeTicker(hist, daily, {"longName": "Test Corp"})
        interaction = make_interaction()
        chart_buffer = png_stub()

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot.generate_chart", return_value=chart_buffer
        ):
            await price.callback(interaction, "TEST")

        embed = sent_embed(interaction)
        assert "Previous Close: $101.00" in field_value(embed, "Data")
        assert "Change: +$5.00 (+4.95%)" in field_value(embed, "Data")
        assert embed.color == discord.Color.green()
        assert chart_buffer.closed

    @pytest.mark.asyncio
    async def test_negative_change_uses_red_embed(self):
        hist = intraday_frame(closes=[100, 99, 98, 97])
        daily = daily_frame([("2024-01-11", 100.0)])
        fake = FakeTicker(hist, daily, {"longName": "Test Corp"})
        interaction = make_interaction()
        chart_buffer = png_stub()

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot.generate_chart", return_value=chart_buffer
        ):
            await price.callback(interaction, "TEST")

        embed = sent_embed(interaction)
        assert embed.description == "# $97.00 -$3.00 (-3.00%)"
        assert embed.color == discord.Color.red()
        assert chart_buffer.closed

    @pytest.mark.asyncio
    async def test_invalid_ticker_is_rejected_before_fetching(self):
        interaction = make_interaction()

        with patch("bot.yf.Ticker") as ticker_mock:
            await price.callback(interaction, "bad ticker!")

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        ticker_mock.assert_not_called()
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "not a valid ticker" in interaction.followup.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_fetch_failure_sends_friendly_ephemeral_message(self):
        fake = FakeTicker(pd.DataFrame())
        interaction = make_interaction()

        with patch("bot.yf.Ticker", return_value=fake), patch("bot.generate_chart") as chart_mock:
            await price.callback(interaction, "NOPE")

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "Could not fetch data for **NOPE**" in interaction.followup.send.await_args.args[0]
        chart_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_history_exception_sends_friendly_ephemeral_message(self):
        fake = FakeTicker(None, intraday_error=RuntimeError("Yahoo unavailable"))
        interaction = make_interaction()

        with patch("bot.yf.Ticker", return_value=fake):
            await price.callback(interaction, "NVDA")

        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "Could not fetch data for **NVDA**" in interaction.followup.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_send_failure_still_closes_chart_buffer(self):
        fake = FakeTicker(
            intraday_frame(),
            daily_frame([("2024-01-11", 145.0)]),
            {"longName": "Test Corp"},
        )
        interaction = make_interaction()
        interaction.followup.send = AsyncMock(side_effect=[RuntimeError("send failed"), None])
        chart_buffer = png_stub()

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot.generate_chart", return_value=chart_buffer
        ):
            await price.callback(interaction, "TEST")

        assert interaction.followup.send.await_count == 2
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "Could not build or send the chart" in interaction.followup.send.await_args.args[0]
        assert chart_buffer.closed

    @pytest.mark.asyncio
    async def test_chart_failure_sends_friendly_ephemeral_message(self):
        fake = FakeTicker(
            intraday_frame(),
            daily_frame([("2024-01-11", 145.0)]),
            {"longName": "Test Corp"},
        )
        interaction = make_interaction()

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot.generate_chart", side_effect=RuntimeError("plot failed")
        ):
            await price.callback(interaction, "TEST")

        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "Could not build or send the chart" in interaction.followup.send.await_args.args[0]


class TestGenerateChart:
    """Tests for in-memory chart generation and cleanup."""

    @staticmethod
    def _green_red_pixel_counts(buffer):
        buffer.seek(0)
        image = np.asarray(plt.imread(buffer))
        red, green, blue = image[..., 0], image[..., 1], image[..., 2]
        green_pixels = int(np.count_nonzero((green > red + 0.1) & (green > blue + 0.1)))
        red_pixels = int(np.count_nonzero((red > green + 0.1) & (red > blue + 0.1)))
        return green_pixels, red_pixels

    def test_generate_chart_returns_png_and_closes_figure(self):
        hist = intraday_frame()
        buffer = generate_chart("TEST", hist, 5.0)

        try:
            buffer.seek(0)
            data = buffer.read()
            assert data.startswith(b"\x89PNG")
            assert len(data) > 500
            assert plt.get_fignums() == []
        finally:
            buffer.close()

    def test_generate_chart_is_green_for_positive_change(self):
        buffer = generate_chart("TEST", intraday_frame(), 2.5)
        try:
            green_pixels, red_pixels = self._green_red_pixel_counts(buffer)
            assert green_pixels > 0
            assert red_pixels == 0
            assert plt.get_fignums() == []
        finally:
            buffer.close()

    def test_generate_chart_is_red_for_negative_change(self):
        buffer = generate_chart("TEST", intraday_frame(), -2.5)
        try:
            green_pixels, red_pixels = self._green_red_pixel_counts(buffer)
            assert red_pixels > 0
            assert green_pixels == 0
            assert plt.get_fignums() == []
        finally:
            buffer.close()


class TestCommandRegistration:
    """Startup slash-command sync paths, exercised without Discord credentials."""

    @staticmethod
    def _sync_results(*results):
        tree = MagicMock()
        tree.sync = AsyncMock(side_effect=results)
        return tree

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_parse_guild_id_treats_unset_or_blank_as_global(self, raw):
        assert parse_guild_id(raw) is None

    def test_parse_guild_id_strips_and_accepts_numeric_id(self):
        assert parse_guild_id(" 123456789012345678\n") == 123456789012345678

    @pytest.mark.parametrize("raw", ["abc", "12 34", "-123", "0", "123abc", "1e18"])
    def test_parse_guild_id_rejects_invalid_values_with_friendly_error(self, raw):
        with pytest.raises(ValueError) as excinfo:
            parse_guild_id(raw)

        message = str(excinfo.value)
        assert "DISCORD_GUILD_ID" in message
        assert "Copy Server ID" in message

    @pytest.mark.asyncio
    async def test_sync_commands_syncs_globally_by_default(self, monkeypatch, capsys):
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
        tree = self._sync_results([SimpleNamespace(name="price")])

        await sync_commands(tree)

        tree.sync.assert_awaited_once_with()
        tree.copy_global_to.assert_not_called()
        tree.clear_commands.assert_not_called()

        out = capsys.readouterr().out
        assert "Synced 1 global command(s): price" in out
        assert "up to an hour" in out
        assert "ERROR" not in out

    @pytest.mark.asyncio
    async def test_sync_commands_guild_mode_syncs_only_the_guild(
        self, monkeypatch, capsys
    ):
        guild_id = 123456789012345678
        monkeypatch.setenv("DISCORD_GUILD_ID", str(guild_id))
        tree = self._sync_results([SimpleNamespace(name="price")])

        await sync_commands(tree)

        tree.copy_global_to.assert_called_once()
        assert tree.copy_global_to.call_args.kwargs["guild"].id == guild_id

        sync_calls = tree.sync.await_args_list
        assert len(sync_calls) == 1
        assert sync_calls[0].kwargs["guild"].id == guild_id
        tree.clear_commands.assert_not_called()

        out = capsys.readouterr().out
        assert f"Synced 1 command(s) to guild {guild_id}: price" in out
        assert "global command registrations were left unchanged" in out
        assert "up to an hour" in out
        assert "registered only in guild" not in out
        assert "ERROR" not in out

    @pytest.mark.asyncio
    async def test_guild_sync_keeps_global_registration_and_adds_guild_command(
        self, monkeypatch
    ):
        # Real CommandTree semantics: global commands must still be registered
        # after guild mode, and the guild must receive its own copy.
        client = MagicMock()
        client._connection._command_tree = None
        tree = app_commands.CommandTree(client)

        @tree.command(name="price", description="Get latest stock price")
        async def _price(interaction):
            pass

        guild_id = 123456789012345678
        monkeypatch.setenv("DISCORD_GUILD_ID", str(guild_id))
        tree.sync = AsyncMock(return_value=[SimpleNamespace(name="price")])

        await sync_commands(tree)

        guild = discord.Object(id=guild_id)
        assert [command.name for command in tree.get_commands()] == ["price"]
        assert [command.name for command in tree.get_commands(guild=guild)] == ["price"]
        tree.sync.assert_awaited_once()
        assert tree.sync.await_args.kwargs["guild"].id == guild_id

    @pytest.mark.asyncio
    async def test_global_sync_failure_logs_hints_and_does_not_raise(
        self, monkeypatch, capsys
    ):
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
        tree = MagicMock()
        tree.sync = AsyncMock(side_effect=RuntimeError("boom"))

        await sync_commands(tree)

        out = capsys.readouterr().out
        assert "ERROR: global slash command sync failed: boom" in out
        assert "applications.commands" in out
        assert "Ctrl/Cmd+R" in out

    @pytest.mark.asyncio
    async def test_guild_sync_failure_logs_guild_id_and_leaves_globals_alone(
        self, monkeypatch, capsys
    ):
        guild_id = 123456789012345678
        monkeypatch.setenv("DISCORD_GUILD_ID", str(guild_id))
        forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access")
        tree = MagicMock()
        tree.sync = AsyncMock(side_effect=forbidden)

        await sync_commands(tree)

        tree.sync.assert_awaited_once()
        tree.clear_commands.assert_not_called()
        out = capsys.readouterr().out
        assert f"ERROR: slash command sync to guild {guild_id} failed" in out
        assert "applications.commands" in out

    @pytest.mark.asyncio
    async def test_setup_hook_registers_commands_once(self, monkeypatch):
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
        with patch("bot.sync_commands", new=AsyncMock(return_value=True)) as sync_mock:
            await bot_module.bot.setup_hook()

        sync_mock.assert_awaited_once_with(bot_module.bot.tree)

    @pytest.mark.asyncio
    async def test_on_ready_with_no_cached_guilds_does_not_sync(self):
        connection = SimpleNamespace(user=SimpleNamespace(id=123456789012345678), guilds=[])
        with patch.object(bot_module.bot, "_connection", connection), patch.object(
            bot_module.bot.tree, "sync", new=AsyncMock()
        ) as sync_mock:
            await bot_module.on_ready()

        sync_mock.assert_not_awaited()

    def test_main_rejects_invalid_guild_id_before_connecting(self, monkeypatch):
        monkeypatch.setattr(bot_module, "TOKEN", "test-token")
        monkeypatch.setenv("DISCORD_GUILD_ID", "not-a-server-id")

        with patch.object(bot_module.bot, "run") as run_mock:
            with pytest.raises(SystemExit) as excinfo:
                bot_module.main()

        run_mock.assert_not_called()
        assert "DISCORD_GUILD_ID" in str(excinfo.value)

    def test_main_runs_bot_when_guild_id_is_valid(self, monkeypatch):
        monkeypatch.setattr(bot_module, "TOKEN", "test-token")
        monkeypatch.setenv("DISCORD_GUILD_ID", "123456789012345678")

        with patch.object(bot_module.bot, "run") as run_mock:
            bot_module.main()

        run_mock.assert_called_once_with("test-token")


class TestAutomaticGuildSync:
    """Automatic per-guild registration on ready, join and reconnect."""

    @pytest.fixture(autouse=True)
    def reset_sync_state(self, monkeypatch):
        # Retries must not sleep for real in tests, and leftover state from
        # another test must not hide a guild that still needs syncing.
        monkeypatch.setattr(bot_module, "GUILD_SYNC_RETRY_DELAY", 0)
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
        bot_module.bot._synced_guild_ids.clear()
        bot_module.bot._syncing_guild_ids.clear()
        yield
        bot_module.bot._synced_guild_ids.clear()
        bot_module.bot._syncing_guild_ids.clear()

    @staticmethod
    def make_bot():
        """A bot with a real command tree holding the same price command."""
        test_bot = bot_module.StockPriceBot(
            command_prefix="!", intents=discord.Intents.default()
        )

        @test_bot.tree.command(name="price", description="Get latest stock price")
        async def _price(interaction):
            pass

        return test_bot

    @staticmethod
    def guild(guild_id, name=""):
        return SimpleNamespace(id=guild_id, name=name)

    @staticmethod
    def ok_commands():
        return [SimpleNamespace(name="price")]

    @pytest.mark.asyncio
    async def test_ready_registers_every_cached_guild(self, capsys):
        guilds = [self.guild(111, "Alpha"), self.guild(222, "Beta")]
        connection = SimpleNamespace(user=SimpleNamespace(id=999), guilds=guilds)

        with patch.object(bot_module.bot, "_connection", connection), patch.object(
            bot_module.bot.tree, "sync", new=AsyncMock(return_value=self.ok_commands())
        ) as sync_mock:
            await bot_module.on_ready()

        assert [call.kwargs["guild"].id for call in sync_mock.await_args_list] == [111, 222]
        # copy_global_to ran first, so each guild holds its own price copy.
        for guild in guilds:
            assert [command.name for command in bot_module.bot.tree.get_commands(guild=guild)] == [
                "price"
            ]

        out = capsys.readouterr().out
        assert "Registered slash commands in 2 guild(s): 111, 222" in out
        assert "ERROR" not in out

    @pytest.mark.asyncio
    async def test_reconnect_ready_does_not_repeat_guild_syncs(self, capsys):
        guilds = [self.guild(111), self.guild(222)]
        connection = SimpleNamespace(user=SimpleNamespace(id=999), guilds=guilds)

        with patch.object(bot_module.bot, "_connection", connection), patch.object(
            bot_module.bot.tree, "sync", new=AsyncMock(return_value=self.ok_commands())
        ) as sync_mock:
            await bot_module.on_ready()
            await bot_module.on_ready()

        assert sync_mock.await_count == 2  # one per guild, none on reconnect
        out = capsys.readouterr().out
        assert out.count("Registered slash commands in 2 guild(s)") == 1

    @pytest.mark.asyncio
    async def test_guild_join_registers_the_new_guild_once(self):
        guild = self.guild(333, "Gamma")

        with patch.object(
            bot_module.bot.tree, "sync", new=AsyncMock(return_value=self.ok_commands())
        ) as sync_mock:
            await bot_module.on_guild_join(guild)
            await bot_module.on_guild_join(guild)

        assert [call.kwargs["guild"].id for call in sync_mock.await_args_list] == [333]
        assert [
            command.name for command in bot_module.bot.tree.get_commands(guild=guild)
        ] == ["price"]
        assert 333 in bot_module.bot._synced_guild_ids

    @pytest.mark.asyncio
    async def test_failed_guild_is_isolated_and_not_marked_synced(
        self, capsys, monkeypatch
    ):
        monkeypatch.setattr(bot_module, "TOKEN", "super-secret-token")
        test_bot = self.make_bot()
        guilds = [self.guild(111, "Broken"), self.guild(222, "Healthy")]
        forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access")
        ok = self.ok_commands()

        def sync_side_effect(*, guild):
            if guild.id == 111:
                raise forbidden
            return ok

        with patch.object(
            test_bot.tree, "sync", new=AsyncMock(side_effect=sync_side_effect)
        ) as sync_mock:
            await test_bot.sync_guilds(guilds)

        assert 222 in test_bot._synced_guild_ids
        assert 111 not in test_bot._synced_guild_ids
        assert (
            len([call for call in sync_mock.await_args_list if call.kwargs["guild"].id == 111])
            == bot_module.GUILD_SYNC_ATTEMPTS
        )

        out = capsys.readouterr().out
        assert "Registered slash commands in 1 guild(s): 222" in out
        assert "ERROR: slash command sync to guild 111 (Broken) failed" in out
        assert "WARNING: slash commands are still missing in 1 guild(s): 111" in out
        assert "applications.commands" in out
        assert "super-secret-token" not in out

    @pytest.mark.asyncio
    async def test_transient_guild_failure_is_retried_until_it_succeeds(self, capsys):
        test_bot = self.make_bot()
        guild = self.guild(444)
        results = [RuntimeError("temporary"), self.ok_commands()]

        with patch.object(
            test_bot.tree, "sync", new=AsyncMock(side_effect=results)
        ) as sync_mock:
            await test_bot.sync_guilds([guild])

        assert sync_mock.await_count == 2
        assert 444 in test_bot._synced_guild_ids
        out = capsys.readouterr().out
        assert "Retrying slash command sync to guild 444 in 0s (attempt 2/3)" in out
        assert "Registered slash commands in 1 guild(s): 444" in out

    @pytest.mark.asyncio
    async def test_guild_that_keeps_failing_is_retried_on_the_next_ready_pass(self):
        test_bot = self.make_bot()
        guild = self.guild(555)

        with patch.object(test_bot.tree, "sync", new=AsyncMock(side_effect=RuntimeError("down"))):
            await test_bot.sync_guilds([guild])

        assert 555 not in test_bot._synced_guild_ids

        with patch.object(
            test_bot.tree, "sync", new=AsyncMock(return_value=self.ok_commands())
        ) as sync_mock:
            await test_bot.sync_guilds([guild])

        assert sync_mock.await_count == 1
        assert 555 in test_bot._synced_guild_ids

    @pytest.mark.asyncio
    async def test_rejoined_guild_is_registered_again(self):
        guild = self.guild(666, "Returning")

        with patch.object(
            bot_module.bot.tree, "sync", new=AsyncMock(return_value=self.ok_commands())
        ) as sync_mock:
            await bot_module.on_guild_join(guild)
            await bot_module.on_guild_remove(guild)
            await bot_module.on_guild_join(guild)

        assert sync_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_explicit_guild_id_is_synced_at_startup_and_not_repeated(
        self, monkeypatch
    ):
        guild_id = 123456789012345678
        monkeypatch.setenv("DISCORD_GUILD_ID", str(guild_id))
        test_bot = self.make_bot()

        with patch("bot.sync_commands", new=AsyncMock(return_value=True)) as startup_sync:
            await test_bot.setup_hook()

        startup_sync.assert_awaited_once_with(test_bot.tree)
        assert guild_id in test_bot._synced_guild_ids

        with patch.object(test_bot.tree, "sync", new=AsyncMock()) as sync_mock:
            await test_bot.sync_guilds([self.guild(guild_id)])

        sync_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_guild_sync_never_clears_global_commands(self):
        test_bot = self.make_bot()

        with patch.object(
            test_bot.tree, "sync", new=AsyncMock(return_value=self.ok_commands())
        ) as sync_mock, patch.object(
            test_bot.tree, "clear_commands", wraps=test_bot.tree.clear_commands
        ) as clear_mock:
            await test_bot.sync_guilds([self.guild(777), self.guild(888)])

        clear_mock.assert_not_called()
        assert all("guild" in call.kwargs for call in sync_mock.await_args_list)
        assert [command.name for command in test_bot.tree.get_commands()] == ["price"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
