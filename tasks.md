# Implementation Plan — Discord Stock Price Bot

Goal: A simple Discord bot that accepts `/price nvda` and returns the latest available stock price with a 1-day intraday chart.

## 1. Tech Stack

| Component        | Technology                 |
| ---------------- | -------------------------- |
| Language         | Python                     |
| Discord bot      | `discord.py`               |
| Stock data       | `yfinance` (Yahoo Finance) |
| Chart generation | `matplotlib`               |
| Configuration    | `.env`                     |

No database, web server, or frontend needed.

## 2. Expected Output

User enters `/price NVDA`.

NVDA — NVIDIA Corporation

# $148.50 +$2.35 (+1.61%)

Illustrative values — not live market data

1D · 5-minute intervals · Regular market hours

## 3. Implementation Steps

1. Create Discord bot

   Create an application in the [Discord Developer Portal](https://discord.com/developers/applications?utm_source=chatgpt.com), add a bot, obtain its token, and invite it to your server with slash-command permissions.
2. Implement `/price` command

   Accept one required string argument: `ticker`. Normalize it to uppercase (e.g., `nvda` → `NVDA`).
3. Fetch stock data

   Use `yfinance` to fetch the latest trading session at 5-minute intervals:
   ```
   df = yf.Ticker(ticker).history(    period="1d",    interval="5m",    auto_adjust=False)
   ```
   Use the last available close as the displayed price. Calculate daily change against the previous trading day's close, fetched separately.
4. Generate chart

   Use `matplotlib` to plot timestamps against close prices. Green for positive daily change, red for negative. Save the chart to an in-memory PNG using `BytesIO`.
5. Send Discord embed

   Include ticker, latest price, dollar/percentage change, and chart attachment. Show the data timestamp, especially when markets are closed.
6. Handle basic errors

   Return a friendly message for invalid symbols, empty market data, and Yahoo Finance failures. Defer the Discord response while fetching data and generating the chart.

## 4. Project Structure

```
stock-bot/
├── bot.py
├── requirements.txt
└── .env
```

`requirements.txt`:

```
discord.py
yfinance
matplotlib
python-dotenv
```

`.env`:

```
DISCORD_TOKEN=your_bot_token
```

## 5. Request Flow

Discord `/price nvda`

Fetch 1D prices from Yahoo Finance

Calculate change + generate PNG chart

Send Discord embed

## 6. Scope

Build:

- `/price <ticker>` slash command
- Latest available price and daily change
- 1D line chart
- Basic error handling

Skip:

- Database
- User accounts
- Price alerts
- Background jobs
- Caching
- Multiple chart timeframes
- Docker or complicated infrastructure

One caveat: `yfinance` is an unofficial Yahoo Finance interface. Quotes may be delayed, and availability isn't guaranteed. Label the data timestamp rather than claiming true real-time pricing. Its intraday history interface supports `period="1d"` and `interval="5m"`.&#x20;

[image](https://www.google.com/s2/favicons?domain=https://ranaroussi.github.io\&sz=32)

yfinance+1



End result: One Python bot, one slash command, one stock-data source, and one generated chart.