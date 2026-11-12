"""Tests for the Discord stock price bot."""

import sys
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
    REMINDER_EMBED_CHAR_BUDGET,
    REMINDER_EMBEDS_PER_MESSAGE,
    REMINDER_FIELDS_PER_EMBED,
    REMINDER_MESSAGE_CHAR_BUDGET,
    TICKER_PATTERN,
    check_price_alerts,
    fetch_latest_price,
    fetch_quote,
    generate_chart,
    parse_guild_id,
    price,
    price_alerts_loop,
    remind,
    reminders,
    sync_commands,
    unremind,
)
from reminders import ReminderStore


class FakeTicker:
    """Stand-in for yfinance.Ticker with inspectable history calls."""

    def __init__(
        self,
        extended=None,
        daily=None,
        info=None,
        *,
        info_error=None,
        extended_error=None,
        daily_error=None,
    ):
        self.extended = extended
        self.daily = daily
        self.info_data = info if info is not None else {}
        self.info_error = info_error
        self.extended_error = extended_error
        self.daily_error = daily_error
        self.history_calls = []
        self.history_thread_ids = []

    @property
    def info(self):
        if self.info_error is not None:
            raise self.info_error
        return self.info_data

    def history(self, period, interval, prepost=False, auto_adjust=False):
        self.history_calls.append((period, interval, prepost, auto_adjust))
        self.history_thread_ids.append(threading.get_ident())
        if period == "5d" and interval == "1m":
            if self.extended_error is not None:
                raise self.extended_error
            if self.extended is None:
                return pd.DataFrame()
            return self.extended
        if period == "10d" and interval == "1d":
            if self.daily_error is not None:
                raise self.daily_error
            if self.daily is None:
                return pd.DataFrame()
            return self.daily
        raise AssertionError(
            "The chart must be derived from the extended-hours fetch; "
            f"unexpected history call: {period=} {interval=}"
        )


def chart_frame(
    start="2024-01-12 09:30",
    periods=5,
    closes=None,
    tz="America/New_York",
):
    """A ready-made 5-minute session frame for generate_chart tests."""
    if closes is not None:
        periods = len(closes)
    dates = pd.date_range(start=start, periods=periods, freq="5min", tz=tz)
    if closes is None:
        closes = [100.0, 101.0, 101.0, 99.0, 102.0]
    return pd.DataFrame({"Close": closes}, index=dates)


def extended_frame(
    start="2024-01-12 15:27",
    periods=39,
    closes=None,
    tz="America/New_York",
):
    """Extended-hours 5-day/1-minute frame spanning pre-market through after-hours."""
    if closes is not None:
        periods = len(closes)
    dates = pd.date_range(start=start, periods=periods, freq="1min", tz=tz)
    if closes is None:
        closes = np.linspace(145.0, 148.5, periods)
    return pd.DataFrame({"Close": closes}, index=dates)


def full_session_frame(tz="America/New_York"):
    """One-minute bars from 04:00 pre-market through 19:59 after-hours."""
    dates = pd.date_range("2024-01-12 04:00", "2024-01-12 19:59", freq="1min", tz=tz)
    return pd.DataFrame({"Close": np.linspace(100.0, 110.0, len(dates))}, index=dates)


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
    """Tests for the extended-hours quote fetch and chart-frame separation."""

    def test_valid_ticker_returns_quote_from_one_minute_extended_data(self):
        ext = extended_frame()
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
            ext,
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
        # The chart is aggregated from the same extended-hours fetch and ends
        # at the raw quote point, not at a 5-minute bucket left edge.
        assert quote.intraday is not None
        assert quote.intraday.index[-1] == ext.index[-1]
        assert quote.intraday["Close"].iloc[-1] == pytest.approx(148.5)
        assert ("5d", "1m", True, False) in fake.history_calls
        assert not [call for call in fake.history_calls if call[0] == "1d"]
        # Timestamp is the last extended bar (16:05).
        assert "16:05" in quote.timestamp_label
        assert "EST" in quote.timestamp_label
        assert "UTC" not in quote.timestamp_label

    def test_previous_close_skips_same_session_and_invalid_daily_rows(self):
        ext = extended_frame()
        daily = daily_frame(
            [
                ("2024-01-08", 100.0),
                ("2024-01-09", np.nan),
                ("2024-01-10", 102.0),
                ("2024-01-11", np.nan),
                ("2024-01-12", 999.0),  # same session must not be previous close
            ]
        )
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 102.0
        assert quote.change == pytest.approx(46.5)
        assert quote.change_pct == pytest.approx(45.588, rel=0.001)

    def test_daily_lookback_uses_ten_days(self):
        fake = FakeTicker(
            extended_frame(),
            daily_frame([("2024-01-11", 145.0)]),
            {"longName": "Test Corp"},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            fetch_quote("TEST")

        assert ("10d", "1d", False, False) in fake.history_calls
        assert len([call for call in fake.history_calls if call[0] == "5d"]) == 1

    def test_closed_session_uses_friday_close_for_weekend_data(self):
        # Monday's after-hours session, with Friday as the latest prior daily
        # row and stale weekend rows that must be ignored.
        ext = extended_frame(start="2024-01-16 15:57", periods=4, closes=[101, 102, 103, 104])
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
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 100.0
        assert quote.change_pct == pytest.approx(4.0)

    def test_premarket_quote_on_new_day_uses_previous_regular_day_close(self):
        # Monday 04:00 pre-market bar; the previous trading close is Friday's.
        ext = extended_frame(start="2024-01-16 04:00", periods=3, closes=[100.5, 100.8, 101.0])
        daily = daily_frame(
            [
                ("2024-01-15", 100.0),
                ("2024-01-16", 999.0),
            ]
        )
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 100.0
        assert quote.change == pytest.approx(1.0)

    def test_after_hours_quote_uses_previous_day_close(self):
        # Last extended bar is after the 16:00 close, so the same-day daily
        # row must not be the previous close.
        ext = extended_frame(start="2024-01-12 15:57", periods=4, closes=[147.5, 148.0, 148.2, 148.4])
        daily = daily_frame(
            [
                ("2024-01-11", 145.0),
                ("2024-01-12", 147.0),
            ]
        )
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 145.0
        assert quote.change == pytest.approx(3.4)

    def test_info_failure_does_not_invalidate_history(self):
        ext = extended_frame()
        daily = daily_frame([("2024-01-11", 145.0)])
        fake = FakeTicker(
            ext,
            daily_frame([("2024-01-11", 145.0)]),
            info_error=RuntimeError("info unavailable"),
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.company_name is None
        assert quote.previous_close == 145.0
        assert quote.latest_price == pytest.approx(148.5)

    def test_daily_missing_falls_back_to_info_previous_close(self):
        fake = FakeTicker(
            extended_frame(closes=[145, 146, 147, 148]),
            pd.DataFrame(),
            {"longName": "Test Corp", "previousClose": 142.0},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 142.0

    def test_daily_exception_falls_back_to_info_previous_close(self):
        fake = FakeTicker(
            extended_frame(closes=[145, 146, 147, 148]),
            daily_error=RuntimeError("daily history unavailable"),
            info={"longName": "Test Corp", "previousClose": 142.0},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.previous_close == 142.0

    def test_naive_timestamp_uses_exchange_timezone_not_utc(self):
        naive_ext = extended_frame(tz=None)
        daily = daily_frame([("2024-01-11", 145.0)])
        fake = FakeTicker(
            naive_ext,
            daily,
            {"longName": "Test Corp", "exchangeTimezoneName": "America/New_York"},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert "EST" in quote.timestamp_label
        assert "UTC" not in quote.timestamp_label

    def test_naive_timestamp_without_timezone_does_not_claim_utc(self):
        naive_ext = extended_frame(tz=None)
        daily = daily_frame([("2024-01-11", 145.0)], tz=None)
        fake = FakeTicker(naive_ext, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert "UTC" not in quote.timestamp_label

    def test_timestamp_converts_utc_data_to_exchange_time(self):
        # 20:51 UTC on 2024-01-12 is 15:51 in New York.
        ext = extended_frame(
            start="2024-01-12 20:50",
            periods=2,
            closes=[147.0, 148.0],
            tz="UTC",
        )
        daily = daily_frame([("2024-01-11", 145.0)])
        fake = FakeTicker(
            ext,
            daily,
            {"longName": "Test Corp", "exchangeTimezoneName": "America/New_York"},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.timestamp_label == "2024-01-12 15:51:00 EST"
        # Bins are exchange-local even though the source index is UTC.
        assert quote.intraday is not None
        assert str(quote.intraday.index.tz) == "America/New_York"
        assert quote.intraday.index[-1].strftime("%H:%M") == "15:51"

    @pytest.mark.parametrize(
        "ext",
        [
            pd.DataFrame(),
            pd.DataFrame({"Open": [100.0]}, index=pd.date_range("2024-01-12", periods=1, freq="1min", tz="UTC")),
            pd.DataFrame({"Close": [np.nan, np.nan]}, index=pd.date_range("2024-01-12", periods=2, freq="1min", tz="UTC")),
            pd.DataFrame({"Close": [-1.0, 0.0]}, index=pd.date_range("2024-01-12", periods=2, freq="1min", tz="UTC")),
            pd.DataFrame(
                {"Close": pd.array([pd.NA, pd.NA], dtype="Float64")},
                index=pd.date_range("2024-01-12", periods=2, freq="1min", tz="UTC"),
            ),
            None,
        ],
    )
    def test_invalid_extended_data_returns_none(self, ext):
        fake = FakeTicker(ext, daily_frame([("2024-01-11", 100.0)]), {"previousClose": 100.0})

        with patch("bot.yf.Ticker", return_value=fake):
            assert fetch_quote("BAD") is None

    @pytest.mark.parametrize("trailing", [pd.NA, np.nan, 0.0, -5.0, float("inf")])
    def test_trailing_invalid_uses_last_valid_extended_bar(self, trailing):
        # A bad trailing bar must not invalidate the last valid bar before it.
        ext = pd.DataFrame(
            {"Close": [147.0, 148.0, trailing]},
            index=pd.date_range("2024-01-12 15:57", periods=3, freq="1min", tz="America/New_York"),
        )
        fake = FakeTicker(ext, daily_frame([("2024-01-11", 145.0)]), {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.latest_price == 148.0
        # Timestamp comes from the same valid bar as the price.
        assert "15:58" in quote.timestamp_label
        assert quote.intraday is not None
        assert quote.intraday.index[-1].strftime("%H:%M") == "15:58"
        assert quote.intraday["Close"].iloc[-1] == pytest.approx(148.0)

    @pytest.mark.parametrize(
        "closes, expected",
        [
            ([147.0, np.nan, 148.0], 148.0),
            ([-1.0, 2.0], 2.0),
            ([0.0, 2.0], 2.0),
            ([148.0, -5.0, np.nan, 148.5], 148.5),
            ([np.inf, 147.0, 148.0], 148.0),
        ],
    )
    def test_mixed_invalid_extended_data_keeps_last_valid_bar(self, closes, expected):
        ext = pd.DataFrame(
            {"Close": closes},
            index=pd.date_range("2024-01-12 15:57", periods=len(closes), freq="1min", tz="America/New_York"),
        )
        fake = FakeTicker(ext, daily_frame([("2024-01-11", 145.0)]), {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        # The quote is the last chronologically valid positive close.
        assert quote.latest_price == pytest.approx(expected)

    def test_chart_covers_premarket_regular_and_after_hours(self):
        ext = full_session_frame()
        daily = daily_frame([("2024-01-11", 100.0)])
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        chart = quote.intraday
        assert chart is not None
        hours = {timestamp.hour for timestamp in chart.index}
        assert 4 in hours  # pre-market
        assert 9 in hours and 12 in hours  # regular session
        assert 17 in hours and 18 in hours  # after-hours
        # The last point is the raw quote timestamp, not a 5-minute left edge.
        assert chart.index[-1] == ext.index[-1]
        assert chart["Close"].iloc[-1] == pytest.approx(110.0)

    def test_chart_takes_last_minute_close_per_five_minute_bin(self):
        ext = pd.DataFrame(
            {"Close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]},
            index=pd.date_range(
                "2024-01-12 09:30", periods=6, freq="1min", tz="America/New_York"
            ),
        )
        daily = daily_frame([("2024-01-11", 100.0)])
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        chart = quote.intraday
        assert chart is not None
        # 09:30-09:34 closes into the 09:30 bin; 09:35 starts the next bin and
        # is also the raw quote point because its bucket left edge is 09:35.
        assert chart.index[0].strftime("%H:%M") == "09:30"
        assert chart["Close"].iloc[0] == pytest.approx(104.0)
        assert chart.index[-1].strftime("%H:%M") == "09:35"
        assert chart["Close"].iloc[-1] == pytest.approx(105.0)

    def test_weekend_quote_charts_weekend_session(self):
        # A valid Saturday quote must be charted: the chart ends at that quote
        # timestamp and price instead of falling back to Friday's session.
        friday = extended_frame(
            start="2024-01-12 15:57", periods=4, closes=[101, 102, 103, 104]
        )
        saturday = pd.DataFrame(
            {"Close": [105.0, 106.0]},
            index=pd.date_range(
                "2024-01-13 10:00", periods=2, freq="1min", tz="America/New_York"
            ),
        )
        ext = pd.concat([friday, saturday])
        daily = daily_frame([("2024-01-12", 100.0)])
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.latest_price == pytest.approx(106.0)
        assert quote.intraday is not None
        chart = quote.intraday
        assert {timestamp.date() for timestamp in chart.index} == {
            pd.Timestamp("2024-01-13").date()
        }
        assert chart.index[-1] == ext.index[-1]
        assert chart["Close"].iloc[-1] == pytest.approx(106.0)

    def test_weekend_request_with_only_friday_bars_charts_friday(self):
        # No bars arrive over the weekend, so the latest quote bar is Friday's
        # last bar and the chart naturally keeps that Friday session.
        ext = extended_frame(
            start="2024-01-12 15:57", periods=4, closes=[101, 102, 103, 104]
        )
        daily = daily_frame([("2024-01-11", 100.0)])
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})

        with patch("bot.yf.Ticker", return_value=fake):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.intraday is not None
        chart = quote.intraday
        assert {timestamp.date() for timestamp in chart.index} == {
            pd.Timestamp("2024-01-12").date()
        }
        assert chart.index[-1] == ext.index[-1]
        assert chart["Close"].iloc[-1] == pytest.approx(104.0)

    def test_chart_aggregation_failure_keeps_quote_without_chart(self):
        fake = FakeTicker(
            extended_frame(),
            daily_frame([("2024-01-11", 145.0)]),
            {"longName": "Test Corp"},
        )

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot._session_chart_frame", side_effect=RuntimeError("aggregation failed")
        ):
            quote = fetch_quote("TEST")

        assert quote is not None
        assert quote.intraday is None
        assert quote.latest_price == pytest.approx(148.5)

    def test_missing_1m_data_returns_none_without_chart_fallback(self):
        fake = FakeTicker(
            pd.DataFrame(),
            daily_frame([("2024-01-11", 145.0)]),
            {"longName": "Test Corp", "previousClose": 145.0},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            assert fetch_quote("NO1M") is None

    def test_extended_history_exception_returns_none(self):
        fake = FakeTicker(
            None,
            daily_frame([("2024-01-11", 145.0)]),
            extended_error=RuntimeError("Yahoo unavailable"),
        )

        with patch("bot.yf.Ticker", return_value=fake):
            assert fetch_quote("ERROR") is None

    def test_ticker_constructor_exception_returns_none(self):
        with patch("bot.yf.Ticker", side_effect=RuntimeError("network down")):
            assert fetch_quote("ERROR") is None

    @pytest.mark.parametrize("previous_close", [None, np.nan, 0.0, -5.0, "invalid"])
    def test_invalid_previous_close_returns_none(self, previous_close):
        fake = FakeTicker(
            extended_frame(),
            pd.DataFrame(),
            {"longName": "Test Corp", "previousClose": previous_close},
        )

        with patch("bot.yf.Ticker", return_value=fake):
            assert fetch_quote("TEST") is None


class TestPriceCommand:
    """Exercise the real slash-command callback with a mocked interaction."""

    @pytest.mark.asyncio
    async def test_success_sends_extended_quote_and_extended_hours_chart(self):
        ext = extended_frame()
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
            ext,
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
            "Latest: $148.50\nPrevious Close: $145.00\nChange vs prev close: +$3.50 (+2.41%)"
        )
        assert "16:05" in field_value(embed, "Timestamp")
        assert "EST" in field_value(embed, "Timestamp")
        assert "Delayed / latest available" in field_value(embed, "Timestamp")
        assert embed.color == discord.Color.green()
        assert embed.image.url == "attachment://NVDA_chart.png"
        footer = embed.footer.text
        assert "extended hours" in footer
        assert "5-minute" in footer
        assert footer.endswith("Delayed, not guaranteed real-time")

        # One extended-hours fetch plus one daily lookback, no chart fetch.
        assert len([call for call in fake.history_calls if call[0] == "5d"]) == 1
        assert not [call for call in fake.history_calls if call[0] == "1d"]
        assert len([call for call in fake.history_calls if call[0] == "10d"]) == 1
        assert fake.history_thread_ids[0] != threading.get_ident()

        chart_mock.assert_called_once()
        chart_ticker, chart_frame = chart_mock.call_args.args
        assert chart_ticker == "NVDA"
        assert chart_frame is not ext  # derived frame, not the raw response
        assert chart_frame.index[-1] == ext.index[-1]
        assert chart_frame["Close"].iloc[-1] == pytest.approx(148.5)
        assert interaction.followup.send.await_args.kwargs["file"].filename == "NVDA_chart.png"
        assert chart_buffer.closed

    @pytest.mark.asyncio
    async def test_embed_timestamp_and_chart_share_the_latest_quote_point(self):
        ext = extended_frame(start="2024-01-12 15:57", periods=3, closes=[147.9, 148.1, 148.3])
        daily = daily_frame([("2024-01-11", 145.0)])
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})
        interaction = make_interaction()
        chart_buffer = png_stub()

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot.generate_chart", return_value=chart_buffer
        ) as chart_mock:
            await price.callback(interaction, "TEST")

        embed = sent_embed(interaction)
        assert "15:59" in field_value(embed, "Timestamp")
        assert "Latest: $148.30" in field_value(embed, "Data")
        chart_frame = chart_mock.call_args.args[1]
        assert chart_frame.index[-1] == ext.index[-1]
        assert chart_frame["Close"].iloc[-1] == pytest.approx(148.3)
        assert chart_buffer.closed

    @pytest.mark.asyncio
    async def test_chartless_quote_sends_embed_without_file(self):
        fake = FakeTicker(
            extended_frame(),
            daily_frame([("2024-01-11", 145.0)]),
            {"longName": "Test Corp"},
        )
        interaction = make_interaction()

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot._session_chart_frame", return_value=None
        ), patch("bot.generate_chart") as chart_mock:
            await price.callback(interaction, "TEST")

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["file"] is None
        assert kwargs["embed"].image.url is None
        assert "No extended-hours chart" in kwargs["embed"].footer.text
        chart_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_chart_aggregation_exception_sends_quote_without_chart(self):
        # A chart failure must still send the valid extended-hours quote.
        fake = FakeTicker(
            extended_frame(),
            daily_frame([("2024-01-11", 145.0)]),
            {"longName": "Test Corp"},
        )
        interaction = make_interaction()

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot._session_chart_frame", side_effect=RuntimeError("aggregation failed")
        ), patch("bot.generate_chart") as chart_mock:
            await price.callback(interaction, "TEST")

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["file"] is None
        assert kwargs["embed"].image.url is None
        assert "No extended-hours chart" in kwargs["embed"].footer.text
        assert "Latest: $148.50" in field_value(kwargs["embed"], "Data")
        assert "16:05" in field_value(kwargs["embed"], "Timestamp")
        assert "Delayed / latest available" in field_value(kwargs["embed"], "Timestamp")
        chart_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_closed_market_session_uses_latest_prior_valid_close(self):
        ext = extended_frame(start="2024-01-12 15:57", periods=4, closes=[103, 104, 105, 106])
        daily = daily_frame(
            [
                ("2024-01-09", 100.0),
                ("2024-01-10", 101.0),
                ("2024-01-11", np.nan),
                ("2024-01-12", 999.0),
            ]
        )
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})
        interaction = make_interaction()
        chart_buffer = png_stub()

        with patch("bot.yf.Ticker", return_value=fake), patch(
            "bot.generate_chart", return_value=chart_buffer
        ):
            await price.callback(interaction, "TEST")

        embed = sent_embed(interaction)
        assert "Previous Close: $101.00" in field_value(embed, "Data")
        assert "Change vs prev close: +$5.00 (+4.95%)" in field_value(embed, "Data")
        assert embed.color == discord.Color.green()
        assert chart_buffer.closed

    @pytest.mark.asyncio
    async def test_negative_change_uses_red_embed(self):
        ext = extended_frame(closes=[100, 99, 98, 97])
        daily = daily_frame([("2024-01-11", 100.0)])
        fake = FakeTicker(ext, daily, {"longName": "Test Corp"})
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
    async def test_extended_history_exception_sends_friendly_ephemeral_message(self):
        fake = FakeTicker(
            None,
            None,
            extended_error=RuntimeError("Yahoo unavailable"),
        )
        interaction = make_interaction()

        with patch("bot.yf.Ticker", return_value=fake):
            await price.callback(interaction, "NVDA")

        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "Could not fetch data for **NVDA**" in interaction.followup.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_send_failure_still_closes_chart_buffer(self):
        fake = FakeTicker(
            extended_frame(),
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
            extended_frame(),
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
    """Tests for dark chart rendering, slope colours and cleanup."""

    @staticmethod
    def _image(buffer):
        buffer.seek(0)
        return np.asarray(plt.imread(buffer))

    @staticmethod
    def _segment_pixel_counts(image):
        red, green, blue = image[..., 0], image[..., 1], image[..., 2]
        rising = int(np.count_nonzero((green > red + 0.1) & (green > blue + 0.1)))
        falling = int(np.count_nonzero((red > green + 0.1) & (red > blue + 0.1)))
        flat = int(
            np.count_nonzero(
                (np.abs(red - 0x9A / 255) < 0.04)
                & (np.abs(green - 0xA0 / 255) < 0.04)
                & (np.abs(blue - 0xA6 / 255) < 0.04)
            )
        )
        return rising, falling, flat

    def test_generate_chart_returns_dark_png_and_closes_figure(self):
        buffer = generate_chart("TEST", chart_frame())

        try:
            buffer.seek(0)
            data = buffer.read()
            assert data.startswith(b"\x89PNG")
            assert len(data) > 500

            image = self._image(buffer)
            # Both the figure margin and the axes area are dark.
            assert max(image[0, 0, :3]) < 0.25
            dark_pixels = image[..., :3].mean(axis=-1) < 0.25
            assert dark_pixels.mean() > 0.5
            assert plt.get_fignums() == []
        finally:
            buffer.close()

    def test_generate_chart_colours_each_segment_by_direction(self):
        # Rising, flat, falling and rising again in one frame.
        frame = chart_frame(closes=[100.0, 101.0, 101.0, 99.0, 102.0])
        buffer = generate_chart("TEST", frame)
        try:
            rising, falling, flat = self._segment_pixel_counts(self._image(buffer))
            assert rising > 100
            assert falling > 100
            assert flat > 100
            assert plt.get_fignums() == []
        finally:
            buffer.close()

    def test_generate_chart_shows_both_colours_on_a_net_positive_day(self):
        # Overall change is positive, but the falling segments stay red.
        frame = chart_frame(closes=[100.0, 101.0, 100.0, 101.0, 102.0])
        buffer = generate_chart("TEST", frame)
        try:
            rising, falling, _ = self._segment_pixel_counts(self._image(buffer))
            assert rising > 0
            assert falling > 0
        finally:
            buffer.close()

    def test_generate_chart_handles_a_single_point(self):
        buffer = generate_chart(
            "TEST", chart_frame(start="2024-01-12 09:30", closes=[100.0])
        )
        try:
            buffer.seek(0)
            assert buffer.read().startswith(b"\x89PNG")
            assert plt.get_fignums() == []
        finally:
            buffer.close()

    def test_generate_chart_does_not_change_global_matplotlib_style(self):
        keys = ("axes.facecolor", "figure.facecolor", "text.color", "grid.color")
        before = {key: plt.rcParams[key] for key in keys}
        buffer = generate_chart("TEST", chart_frame())
        buffer.close()
        assert {key: plt.rcParams[key] for key in keys} == before


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
        bot_module.bot._price_alerts_started = False
        with patch("bot.sync_commands", new=AsyncMock(return_value=True)) as sync_mock, patch(
            "bot.price_alerts_loop.start"
        ):
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
        # copy_global_to ran first, so each guild holds its own copy of every
        # global command.
        expected = [command.name for command in bot_module.bot.tree.get_commands()]
        for guild in guilds:
            assert [command.name for command in bot_module.bot.tree.get_commands(guild=guild)] == expected

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
        expected = [command.name for command in bot_module.bot.tree.get_commands()]
        assert [
            command.name for command in bot_module.bot.tree.get_commands(guild=guild)
        ] == expected
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


class TestFetchLatestPrice:
    """Lightweight quote helper: one 5d/1m fetch, no info/daily/chart calls."""

    def test_returns_latest_valid_price_and_timestamp(self):
        ext = extended_frame()
        fake = FakeTicker(ext, None, None)

        with patch("bot.yf.Ticker", return_value=fake):
            result = fetch_latest_price("NVDA")

        assert result is not None
        assert result.price == pytest.approx(148.5)
        assert result.timestamp == ext.index[-1]
        # Exactly one lightweight fetch: 5d/1m prepost, no daily history.
        assert fake.history_calls == [("5d", "1m", True, False)]

    def test_trailing_invalid_bar_uses_last_valid_one(self):
        ext = pd.DataFrame(
            {"Close": [147.0, 148.0, np.nan]},
            index=pd.date_range("2024-01-12 15:57", periods=3, freq="1min", tz="America/New_York"),
        )
        fake = FakeTicker(ext, None, None)

        with patch("bot.yf.Ticker", return_value=fake):
            result = fetch_latest_price("TEST")

        assert result is not None
        assert result.price == pytest.approx(148.0)
        assert result.timestamp == ext.index[1]

    @pytest.mark.parametrize(
        "ext",
        [
            pd.DataFrame(),
            pd.DataFrame({"Close": [np.nan, np.nan]}, index=pd.date_range("2024-01-12", periods=2, freq="1min", tz="UTC")),
            pd.DataFrame({"Close": [-1.0, 0.0]}, index=pd.date_range("2024-01-12", periods=2, freq="1min", tz="UTC")),
            None,
        ],
    )
    def test_unusable_data_returns_none(self, ext):
        fake = FakeTicker(ext, None, None)

        with patch("bot.yf.Ticker", return_value=fake):
            assert fetch_latest_price("BAD") is None

    def test_history_exception_returns_none(self):
        fake = FakeTicker(None, None, None, extended_error=RuntimeError("Yahoo unavailable"))

        with patch("bot.yf.Ticker", return_value=fake):
            assert fetch_latest_price("ERROR") is None

    def test_ticker_constructor_exception_returns_none(self):
        with patch("bot.yf.Ticker", side_effect=RuntimeError("network down")):
            assert fetch_latest_price("ERROR") is None


class TestRemindCommand:
    """/remind: validation, direction, persistence and ephemeral confirmation."""

    @staticmethod
    def make_interaction(user_id=42, channel_id=10, guild_id=111):
        interaction = MagicMock()
        interaction.user = SimpleNamespace(id=user_id)
        interaction.channel = SimpleNamespace(
            id=channel_id, guild=SimpleNamespace(id=guild_id) if guild_id is not None else None
        )
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    @staticmethod
    def ok_fake():
        return FakeTicker(extended_frame(), None, None)

    @pytest.mark.asyncio
    async def test_success_saves_alert_and_confirms_ephemeral(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        interaction = self.make_interaction()

        with patch("bot.yf.Ticker", return_value=self.ok_fake()), patch(
            "bot.reminder_store", store
        ):
            await remind.callback(interaction, " nvda ", 150.0)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["ephemeral"] is True

        rows = store.list_by_user(42)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "NVDA"
        assert rows[0]["target_price"] == 150.0
        assert rows[0]["direction"] == "above"
        assert rows[0]["channel_id"] == 10

        embed = kwargs["embed"]
        assert str(rows[0]["id"]) in embed.title
        values = {field.name: field.value for field in embed.fields}
        assert values["Ticker"] == "NVDA"
        assert values["Target"] == "$150.00"
        assert values["Current"] == "$148.50"
        assert values["Direction"] == "above"
        # Timestamp plus delayed caveat, no real-time claim.
        assert "16:05" in embed.description
        assert "delayed / latest available" in embed.description

    @pytest.mark.asyncio
    async def test_lower_target_sets_below_direction(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        interaction = self.make_interaction()

        with patch("bot.yf.Ticker", return_value=self.ok_fake()), patch(
            "bot.reminder_store", store
        ):
            await remind.callback(interaction, "NVDA", 140.0)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        rows = store.list_by_user(42)
        assert rows[0]["direction"] == "below"

    @pytest.mark.asyncio
    async def test_target_equal_to_current_price_is_rejected(self, tmp_path):
        # The stale-price guard: a target at the current displayed price
        # cannot be created, so it cannot auto-fire on that stale value.
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        interaction = self.make_interaction()

        with patch("bot.yf.Ticker", return_value=self.ok_fake()), patch(
            "bot.reminder_store", store
        ):
            await remind.callback(interaction, "NVDA", 148.50)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "already trading at" in interaction.followup.send.await_args.args[0]
        assert store.list_all() == []

    @pytest.mark.asyncio
    async def test_invalid_ticker_rejected_before_fetching(self):
        interaction = self.make_interaction()

        with patch("bot.yf.Ticker") as ticker_mock, patch(
            "bot.reminder_store", MagicMock()
        ):
            await remind.callback(interaction, "bad ticker!", 150.0)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        ticker_mock.assert_not_called()
        interaction.followup.send.assert_awaited_once()
        assert "not a valid ticker" in interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_non_positive_target_rejected_before_fetching(self):
        interaction = self.make_interaction()

        with patch("bot.yf.Ticker") as ticker_mock, patch(
            "bot.reminder_store", MagicMock()
        ):
            await remind.callback(interaction, "NVDA", -5.0)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        ticker_mock.assert_not_called()
        interaction.followup.send.assert_awaited_once()
        assert "positive number" in interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_dm_channel_rejected_without_saving(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        interaction = self.make_interaction(guild_id=None)

        with patch("bot.yf.Ticker") as ticker_mock, patch(
            "bot.reminder_store", store
        ):
            await remind.callback(interaction, "NVDA", 150.0)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        ticker_mock.assert_not_called()
        interaction.followup.send.assert_awaited_once()
        assert store.list_all() == []
        assert "server channels" in interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_save(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        fake = FakeTicker(None, None, None, extended_error=RuntimeError("Yahoo unavailable"))
        interaction = self.make_interaction()

        with patch("bot.yf.Ticker", return_value=fake), patch("bot.reminder_store", store):
            await remind.callback(interaction, "NVDA", 150.0)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        assert store.list_all() == []
        interaction.followup.send.assert_awaited_once()
        assert "Could not fetch a latest price" in interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_fetch_and_store_run_off_the_event_loop(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        fake = self.ok_fake()
        interaction = self.make_interaction()

        with patch("bot.yf.Ticker", return_value=fake), patch("bot.reminder_store", store):
            await remind.callback(interaction, "NVDA", 150.0)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        assert fake.history_thread_ids[0] != threading.get_ident()
        assert len(store.list_by_user(42)) == 1


class TestRemindersCommand:
    """/reminders: owner-only listing, empty list and embed chunking."""

    @staticmethod
    def make_interaction(user_id=42):
        interaction = MagicMock()
        interaction.user = SimpleNamespace(id=user_id)
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    @pytest.mark.asyncio
    async def test_empty_list_is_friendly_ephemeral(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        interaction = self.make_interaction()

        with patch("bot.reminder_store", store):
            await reminders.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert "no pending price alerts" in interaction.followup.send.await_args.args[0]

    @pytest.mark.asyncio
    async def test_lists_only_the_invoking_users_alerts(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        store.add_reminder(42, 10, "NVDA", 100.0, "above")
        store.add_reminder(42, 10, "AAPL", 200.0, "below")
        store.add_reminder(99, 10, "TSLA", 300.0, "above")  # other user
        interaction = self.make_interaction(user_id=42)

        with patch("bot.reminder_store", store):
            await reminders.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs["ephemeral"] is True
        embeds = kwargs["embeds"]
        fields = [(field.name, field.value) for embed in embeds for field in embed.fields]
        assert ("#1 NVDA", "$100.00 (above)") in fields
        assert ("#2 AAPL", "$200.00 (below)") in fields
        assert not any(name.startswith("#3") for name, _ in fields)

    @staticmethod
    def sent_messages(interaction):
        """Every follow-up send as an (ephemeral, embeds) pair, in order."""
        return [
            (call.kwargs["ephemeral"], call.kwargs["embeds"])
            for call in interaction.followup.send.await_args_list
        ]

    @staticmethod
    def embed_chars(embed):
        """Discord's total embed-text count: title, description, fields, footer, author."""
        total = 0
        if embed.title:
            total += len(embed.title)
        if embed.description:
            total += len(embed.description)
        for field in embed.fields:
            total += len(field.name) + len(field.value)
        if embed.footer is not None and embed.footer.text:
            total += len(embed.footer.text)
        if embed.author is not None and embed.author.name:
            total += len(embed.author.name)
        return total

    @staticmethod
    def assert_discord_limits(messages):
        """Simulate Discord's caps on every sent /reminders message.

        Each message must hold at most 10 embeds whose combined text stays
        within the safety-margin message budget, and each embed must stay
        within the 25-field count, 256-character name, 1024-character value
        and safety-margin total-text budgets.
        """
        for ephemeral, embeds in messages:
            assert ephemeral is True
            assert len(embeds) <= REMINDER_EMBEDS_PER_MESSAGE
            assert (
                sum(TestRemindersCommand.embed_chars(embed) for embed in embeds)
                <= REMINDER_MESSAGE_CHAR_BUDGET
            )
            for embed in embeds:
                assert len(embed.fields) <= REMINDER_FIELDS_PER_EMBED
                assert (
                    TestRemindersCommand.embed_chars(embed) <= REMINDER_EMBED_CHAR_BUDGET
                )
                for field in embed.fields:
                    assert len(field.name) <= 256
                    assert len(field.value) <= 1024

    @staticmethod
    def all_field_names(messages):
        """Every field name across all sent messages, in order."""
        return [
            field.name
            for _, embeds in messages
            for embed in embeds
            for field in embed.fields
        ]

    @pytest.mark.asyncio
    async def test_many_alerts_chunk_into_bounded_embeds(self, tmp_path):
        # 30 alerts fit in one message (two embeds), each inside the limits.
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        for index in range(30):
            store.add_reminder(42, 10, "NVDA", 100.0 + index, "above")
        interaction = self.make_interaction()

        with patch("bot.reminder_store", store):
            await reminders.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        messages = self.sent_messages(interaction)
        assert len(messages) == 1
        assert all(ephemeral for ephemeral, _ in messages)
        self.assert_discord_limits(messages)
        embeds = messages[0][1]
        assert len(embeds) == 2
        assert [len(embed.fields) for embed in embeds] == [
            REMINDER_FIELDS_PER_EMBED,
            30 - REMINDER_FIELDS_PER_EMBED,
        ]

    @pytest.mark.asyncio
    async def test_251_alerts_split_across_multiple_messages(self, tmp_path):
        # 251 alerts need 11 embeds; the per-message character budget caps
        # each message at 8 embeds, so the list splits into [8, 3], in order,
        # with every message inside Discord's limits.
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        for index in range(251):
            store.add_reminder(42, 10, "NVDA", 100.0 + index, "above")
        interaction = self.make_interaction()

        with patch("bot.reminder_store", store):
            await reminders.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        messages = self.sent_messages(interaction)
        assert len(messages) == 2
        assert all(ephemeral for ephemeral, _ in messages)
        assert [len(embeds) for _, embeds in messages] == [8, 3]
        self.assert_discord_limits(messages)

        names = self.all_field_names(messages)
        assert names[0] == "#1 NVDA"
        assert names[-1] == "#251 NVDA"
        assert len(names) == 251

    @pytest.mark.asyncio
    async def test_250_alerts_split_across_char_budgeted_messages(self, tmp_path):
        # 250 alerts are 10 full embeds whose combined text exceeds the
        # per-message character budget, so the list splits into [8, 2]
        # messages, in order, with no alert missing.
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        for index in range(250):
            store.add_reminder(42, 10, "NVDA", 100.0 + index, "above")
        interaction = self.make_interaction()

        with patch("bot.reminder_store", store):
            await reminders.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        messages = self.sent_messages(interaction)
        assert len(messages) == 2
        assert all(ephemeral for ephemeral, _ in messages)
        assert [len(embeds) for _, embeds in messages] == [8, 2]
        self.assert_discord_limits(messages)

        names = self.all_field_names(messages)
        assert names[0] == "#1 NVDA"
        assert names[-1] == "#250 NVDA"
        assert len(names) == 250

    @pytest.mark.asyncio
    async def test_120_alerts_chunk_into_five_embeds_in_one_message(self, tmp_path):
        # 120 alerts fit in one message as 5 embeds (25 fields each, last 20),
        # in order.
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        for index in range(120):
            store.add_reminder(42, 10, "NVDA", 100.0 + index, "above")
        interaction = self.make_interaction()

        with patch("bot.reminder_store", store):
            await reminders.callback(interaction)

        messages = self.sent_messages(interaction)
        assert len(messages) == 1
        assert all(ephemeral for ephemeral, _ in messages)
        self.assert_discord_limits(messages)
        embeds = messages[0][1]
        assert [len(embed.fields) for embed in embeds] == [25, 25, 25, 25, 20]
        names = [field.name for embed in embeds for field in embed.fields]
        assert names[0] == "#1 NVDA"
        assert names[-1] == "#120 NVDA"

    @pytest.mark.asyncio
    async def test_260_alerts_keep_every_message_within_discord_limits(
        self, tmp_path
    ):
        # 260 normal alerts: every sent message must stay inside Discord's
        # simulated limits (embed count, per-embed and per-message character
        # budgets) and no alert may be missing, in order.
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        for index in range(260):
            store.add_reminder(42, 10, "NVDA", 100.0 + index, "above")
        interaction = self.make_interaction()

        with patch("bot.reminder_store", store):
            await reminders.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        messages = self.sent_messages(interaction)
        assert len(messages) >= 2
        self.assert_discord_limits(messages)

        names = self.all_field_names(messages)
        assert len(names) == 260
        assert names == [f"#{index} NVDA" for index in range(1, 261)]

    @pytest.mark.asyncio
    async def test_huge_price_alerts_stay_within_limits_without_exception(
        self, tmp_path
    ):
        # 1e300 formats to ~310 digits with %.2f: the field value must stay
        # inside Discord's 1024-character cap, the char budgets must split
        # the embeds, and no alert may be dropped.
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        for index in range(30):
            store.add_reminder(42, 10, "NVDA", 1e300, "above")
        interaction = self.make_interaction()

        with patch("bot.reminder_store", store):
            await reminders.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        messages = self.sent_messages(interaction)
        assert all(ephemeral for ephemeral, _ in messages)
        self.assert_discord_limits(messages)

        names = self.all_field_names(messages)
        assert len(names) == 30
        assert names[0] == "#1 NVDA"
        assert names[-1] == "#30 NVDA"

    def test_build_reminder_embeds_splits_on_char_budget_for_extreme_rows(self):
        # 15-character tickers and the largest finite price make each field
        # ~340 characters, so one 25-field embed would exceed the embed
        # character budget: the builder must split and keep every row.
        rows = [
            {
                "id": index,
                "ticker": "ABCDEFGHIJKLMNOPQ",
                "target_price": sys.float_info.max,
                "direction": "above",
            }
            for index in range(1, 26)
        ]
        embeds = bot_module._build_reminder_embeds(rows)

        assert len(embeds) == 2
        assert [len(embed.fields) for embed in embeds] == [16, 9]
        for embed in embeds:
            assert bot_module._reminder_embed_char_count(embed) <= REMINDER_EMBED_CHAR_BUDGET
            for field in embed.fields:
                assert len(field.name) <= 256
                assert len(field.value) <= 1024
        names = [field.name for embed in embeds for field in embed.fields]
        assert names == [
            f"#{index} ABCDEFGHIJKLMNOPQ" for index in range(1, 26)
        ]

    def test_reminder_embed_messages_respects_embed_and_char_limits(self):
        # Twelve ~600-character embeds: both the 10-embed cap and the
        # per-message character budget must be enforced, in order, with no
        # embed lost.
        embeds = [
            discord.Embed(title="t" * 500, description="d" * 100)
            for _ in range(12)
        ]
        messages = bot_module._reminder_embed_messages(embeds)

        assert [len(message) for message in messages] == [9, 3]
        flat = [embed for message in messages for embed in message]
        assert flat == embeds
        for message in messages:
            assert len(message) <= REMINDER_EMBEDS_PER_MESSAGE
            assert (
                sum(self.embed_chars(embed) for embed in message)
                <= REMINDER_MESSAGE_CHAR_BUDGET
            )


class TestUnremindCommand:
    """/unremind: owner-only cancellation."""

    @staticmethod
    def make_interaction(user_id=42):
        interaction = MagicMock()
        interaction.user = SimpleNamespace(id=user_id)
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    @pytest.mark.asyncio
    async def test_owner_can_cancel(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        reminder_id = store.add_reminder(42, 10, "NVDA", 100.0, "above")
        interaction = self.make_interaction(user_id=42)

        with patch("bot.reminder_store", store):
            await unremind.callback(interaction, reminder_id)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        assert "cancelled" in interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert store.get_by_id(reminder_id) is None

    @pytest.mark.asyncio
    async def test_cross_user_cancellation_prevented(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        reminder_id = store.add_reminder(42, 10, "NVDA", 100.0, "above")
        interaction = self.make_interaction(user_id=99)

        with patch("bot.reminder_store", store):
            await unremind.callback(interaction, reminder_id)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        assert "belongs to you" in interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert store.get_by_id(reminder_id) is not None

    @pytest.mark.asyncio
    async def test_unknown_id_reports_failure(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        interaction = self.make_interaction()

        with patch("bot.reminder_store", store):
            await unremind.callback(interaction, 9999)

        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        assert "belongs to you" in interaction.followup.send.await_args.args[0]
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


class TestPriceAlertsChecker:
    """60-second cycle: unique quotes, one-shot notify, safe failures, persistence."""

    @staticmethod
    def make_channel(channel_id=10, guild_id=111, send=None):
        channel = MagicMock()
        channel.id = channel_id
        channel.guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        channel.send = send if send is not None else AsyncMock()
        return channel

    @staticmethod
    def ticker_factory(payloads):
        """yf.Ticker stand-in keyed by symbol; missing symbols look unavailable."""

        def factory(ticker):
            extended = payloads.get(ticker)
            if extended is None:
                return FakeTicker(
                    None, None, None, extended_error=RuntimeError("Yahoo unavailable")
                )
            return FakeTicker(extended, None, None)

        return factory

    @pytest.mark.asyncio
    async def test_due_alert_notifies_owner_and_deletes(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        reminder_id = store.add_reminder(42, 10, "NVDA", 100.0, "above")
        channel = self.make_channel()

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", return_value=channel):
            await check_price_alerts()

        channel.send.assert_awaited_once()
        content = channel.send.await_args.args[0]
        assert "<@42>" in content
        assert "NVDA" in content
        assert "16:05" in content
        assert "delayed / latest available" in content
        assert "not real-time" in content
        mentions = channel.send.await_args.kwargs["allowed_mentions"]
        # Mentions are restricted to the owner's user ID exactly: no
        # @everyone, @role or replied-user mentions can fire from the text.
        assert mentions.users == [discord.Object(id=42)]
        assert mentions.everyone is False
        assert mentions.roles is False
        assert mentions.replied_user is False
        assert store.get_by_id(reminder_id) is None

    @pytest.mark.asyncio
    async def test_upward_alert_below_target_is_retained(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        store.add_reminder(42, 10, "NVDA", 150.0, "above")  # price 148.5 < 150
        channel = self.make_channel()

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", return_value=channel):
            await check_price_alerts()

        channel.send.assert_not_awaited()
        assert store.list_all() != []

    @pytest.mark.asyncio
    async def test_downward_alert_fires_when_price_at_or_below_target(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        reminder_id = store.add_reminder(42, 10, "NVDA", 149.0, "below")  # 148.5 <= 149
        channel = self.make_channel()

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", return_value=channel):
            await check_price_alerts()

        channel.send.assert_awaited_once()
        assert store.get_by_id(reminder_id) is None

    @pytest.mark.asyncio
    async def test_equal_price_triggers_per_spec_comparison(self, tmp_path):
        # Checker comparisons are >=/<=; equal targets fire. The stale-price
        # guarantee comes from /remind refusing to create such an alert.
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        reminder_id = store.add_reminder(42, 10, "NVDA", 148.5, "above")
        channel = self.make_channel()

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", return_value=channel):
            await check_price_alerts()

        channel.send.assert_awaited_once()
        assert store.get_by_id(reminder_id) is None

    @pytest.mark.asyncio
    async def test_notification_happens_only_once(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        store.add_reminder(42, 10, "NVDA", 100.0, "above")
        channel = self.make_channel()

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", return_value=channel):
            await check_price_alerts()
            await check_price_alerts()  # next cycle: nothing pending

        assert channel.send.await_count == 1
        assert store.list_all() == []

    @pytest.mark.asyncio
    async def test_unique_tickers_fetch_one_quote_each(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        store.add_reminder(42, 10, "NVDA", 100.0, "above")
        store.add_reminder(42, 10, "NVDA", 90.0, "below")
        store.add_reminder(42, 10, "AAPL", 200.0, "above")

        seen = []
        factory = self.ticker_factory({"NVDA": extended_frame(), "AAPL": extended_frame()})

        def recording_factory(ticker):
            seen.append(ticker)
            return factory(ticker)

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", side_effect=recording_factory
        ), patch.object(bot_module.bot, "get_channel", return_value=self.make_channel()):
            await check_price_alerts()

        # One quote per unique ticker: NVDA once, AAPL once.
        assert sorted(seen) == ["AAPL", "NVDA"]

    @pytest.mark.asyncio
    async def test_failed_fetch_retains_only_its_own_reminders(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        bad_id = store.add_reminder(42, 10, "NVDA", 100.0, "above")
        good_id = store.add_reminder(42, 10, "AAPL", 140.0, "above")  # 148.5 >= 140

        factory = self.ticker_factory({"AAPL": extended_frame()})  # NVDA unavailable

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", side_effect=factory
        ), patch.object(bot_module.bot, "get_channel", return_value=self.make_channel()):
            await check_price_alerts()

        rows = store.list_all()
        assert [row["id"] for row in rows] == [bad_id]  # NVDA retained
        assert store.get_by_id(good_id) is None  # AAPL notified

    @pytest.mark.asyncio
    async def test_failed_send_retains_reminder(self, tmp_path, capsys):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        reminder_id = store.add_reminder(42, 10, "NVDA", 100.0, "above")
        channel = self.make_channel(send=AsyncMock(side_effect=RuntimeError("no permission")))

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", return_value=channel):
            await check_price_alerts()

        assert store.get_by_id(reminder_id) is not None
        out = capsys.readouterr().out
        assert f"alert #{reminder_id} (NVDA) send failed" in out
        assert "no permission" not in out  # sanitized: exception type only

    @pytest.mark.asyncio
    async def test_get_channel_falls_back_to_fetch_channel(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        reminder_id = store.add_reminder(42, 10, "NVDA", 100.0, "above")
        channel = self.make_channel()

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", return_value=None), patch.object(
            bot_module.bot, "fetch_channel", new=AsyncMock(return_value=channel)
        ) as fetch_mock:
            await check_price_alerts()

        fetch_mock.assert_awaited_once_with(10)
        channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unavailable_channel_retains_reminder(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        store.add_reminder(42, 10, "NVDA", 100.0, "above")

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", side_effect=RuntimeError("HTTP 404")), patch.object(
            bot_module.bot, "fetch_channel", new=AsyncMock(side_effect=RuntimeError("HTTP 404"))
        ):
            await check_price_alerts()

        assert store.list_all() != []

    @pytest.mark.asyncio
    async def test_dm_channel_never_receives_alerts(self, tmp_path):
        store = ReminderStore(db_path=tmp_path / "reminders.db")
        store.add_reminder(42, 10, "NVDA", 100.0, "above")
        channel = self.make_channel(guild_id=None)

        with patch("bot.reminder_store", store), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", return_value=channel):
            await check_price_alerts()

        channel.send.assert_not_awaited()
        assert store.list_all() != []

    @pytest.mark.asyncio
    async def test_persistence_across_restart_fires_once(self, tmp_path):
        db_path = tmp_path / "reminders.db"
        store_before = ReminderStore(db_path=db_path)
        reminder_id = store_before.add_reminder(42, 10, "NVDA", 100.0, "above")

        # Simulate a bot restart: a fresh store over the same database file.
        store_after = ReminderStore(db_path=db_path)
        channel = self.make_channel()

        with patch("bot.reminder_store", store_after), patch(
            "bot.yf.Ticker", return_value=FakeTicker(extended_frame(), None, None)
        ), patch.object(bot_module.bot, "get_channel", return_value=channel):
            await check_price_alerts()

        channel.send.assert_awaited_once()
        assert store_after.get_by_id(reminder_id) is None

        store_verifier = ReminderStore(db_path=db_path)
        assert store_verifier.list_all() == []


@pytest.mark.asyncio
class TestPriceAlertsLoopLifecycle:
    """Loop lifecycle: started once, ready guard, clean stop."""

    @pytest.fixture(autouse=True)
    def reset_loop_state(self):
        # Leftover lifecycle state from another test must not hide a second
        # start (or a missing one).
        bot_module.bot._price_alerts_started = False
        yield
        bot_module.bot._price_alerts_started = False

    @pytest.mark.asyncio
    async def test_setup_hook_starts_the_loop_only_once(self, monkeypatch):
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
        with patch("bot.sync_commands", new=AsyncMock(return_value=True)), patch(
            "bot.price_alerts_loop.start"
        ) as start_mock:
            await bot_module.bot.setup_hook()
            await bot_module.bot.setup_hook()  # simulated repeat/restart

        start_mock.assert_called_once()
        assert bot_module.bot._price_alerts_started is True

    @pytest.mark.asyncio
    async def test_loop_skips_cycle_when_not_ready(self):
        with patch.object(bot_module.bot, "is_ready", return_value=False), patch(
            "bot.check_price_alerts", new=AsyncMock()
        ) as check_mock:
            await price_alerts_loop()

        check_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_loop_runs_cycle_when_ready(self):
        with patch.object(bot_module.bot, "is_ready", return_value=True), patch(
            "bot.check_price_alerts", new=AsyncMock()
        ) as check_mock:
            await price_alerts_loop()

        check_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_loop_failure_is_logged_and_does_not_raise(self, capsys):
        with patch.object(bot_module.bot, "is_ready", return_value=True), patch(
            "bot.check_price_alerts", new=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            await price_alerts_loop()

        out = capsys.readouterr().out
        assert "price alert cycle failed: RuntimeError" in out

    @staticmethod
    def make_bot():
        """A bot instance that can be closed without any network access."""
        return bot_module.StockPriceBot(
            command_prefix="!", intents=discord.Intents.default()
        )

    @pytest.mark.asyncio
    async def test_close_cancels_the_loop_and_awaits_super_close(self):
        test_bot = self.make_bot()
        with patch("bot.price_alerts_loop.cancel") as cancel_mock, patch(
            "discord.ext.commands.Bot.close", new=AsyncMock()
        ) as close_mock:
            await test_bot.close()

        cancel_mock.assert_called_once()
        close_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_is_idempotent_when_the_bot_never_started(self):
        # Closing a bot that never connected (loop never started) must still
        # cancel the loop and await super's close, and repeat safely.
        test_bot = self.make_bot()
        with patch("bot.price_alerts_loop.cancel") as cancel_mock, patch(
            "discord.ext.commands.Bot.close", new=AsyncMock()
        ) as close_mock:
            await test_bot.close()
            await test_bot.close()

        assert cancel_mock.call_count == 2
        assert close_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_close_does_not_restart_the_loop(self):
        # Shutdown must never start the loop again (no double start after a
        # reconnect/restart).
        test_bot = self.make_bot()
        with patch("bot.price_alerts_loop.cancel"), patch(
            "discord.ext.commands.Bot.close", new=AsyncMock()
        ), patch("bot.price_alerts_loop.start") as start_mock:
            await test_bot.close()

        start_mock.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
