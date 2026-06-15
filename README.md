# degeneratr

An options day-trading tool: scans a universe, generates signals from
technical + volatility strategies, gates them through a risk manager, and routes
orders to a broker — with an event-driven backtester for offline validation.

> Backend only. No frontend.

## Architecture

```
scanner ──► strategies ──► risk ──► broker
   │            │            │         │
   └── data provider (Tiger) shared across the pipeline
```

- **Data** is abstracted behind `MarketDataProvider` (ABC). The Tiger
  implementation (`tigeropen`) is selected via the `MARKET_DATA_PROVIDER` env
  var by a factory. All SDK calls are synchronous, so each is wrapped in
  `asyncio.to_thread(...)` and the client is lazily initialized under a lock.
- **Execution** is abstracted behind `BrokerProvider` (ABC) with two
  implementations: `MooMooBroker` (`moomoo-api`, via a local OpenD gateway) and
  `PaperBroker` (local simulation, no external calls). Selected via
  `BROKER_PROVIDER`.
- **Credentials** load from `.env` through pydantic-settings — nothing is
  hardcoded.

## Layout

```
degeneratr/
├── config.py            # pydantic Settings
├── data/                # MarketDataProvider ABC + Tiger impl + factory
├── indicators/          # RSI, MACD, VWAP, EMA, ATR, Bollinger (pandas-ta)
├── scanner/             # TickerScanner (market_scanner + IV + flow + earnings)
├── strategies/          # MomentumBreakout, IVRankStrategy, ZeroDTE
├── risk/                # RiskManager (per-trade/daily loss, delta, sizing)
├── broker/              # BrokerProvider ABC + MooMoo + Paper
├── backtester/          # event-driven engine over Tiger option bars
├── engine.py            # TradingEngine: run_paper() / run_live()
└── __main__.py          # CLI entrypoint
```

## Setup

1. **Python 3.10+** is required.

2. Install (editable, with dev tools):

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   ```

3. Configure environment:

   ```powershell
   Copy-Item .env.example .env
   # then edit .env with your credentials
   ```

   See `.env.example` for every variable and what it does.

### Tiger (market data)

- Create an app in the Tiger OpenAPI portal, generate an RSA keypair, and point
  `TIGER_PRIVATE_KEY_PATH` at your private key `.pem`.
- Set `TIGER_ID`, `TIGER_ACCOUNT`, and `TIGER_TRADE_ENV` (`PAPER` or `LIVE`).

### MooMoo (execution)

- Install and run the **OpenD** gateway locally; it listens on
  `MOOMOO_HOST:MOOMOO_PORT` (default `127.0.0.1:11111`).
- Set `MOOMOO_SECURITY_FIRM`, `MOOMOO_UNLOCK_TRADE` (6-digit trade password) and
  `MOOMOO_TRADE_ENV` (`SIMULATE` for built-in paper trading, `REAL` for live).
- For a no-gateway dry run, leave `BROKER_PROVIDER=paper`.

## Usage

```powershell
# Paper-trade one tick of the full pipeline
python -m degeneratr paper --ticks 1

# Choose strategies explicitly
python -m degeneratr paper --strategies momentum_breakout iv_rank

# Just see what the scanner surfaces
python -m degeneratr scan --limit 15

# Backtest a strategy on a ticker over the last 5 days
python -m degeneratr backtest --ticker SPY --strategy zero_dte --days 5

# Launch the web dashboard (backtest console + scanner)
python -m degeneratr serve            # then open http://127.0.0.1:8000
python -m degeneratr serve --port 8080 --reload
```

## Web dashboard

`serve` starts a FastAPI app that serves a JSON API and a static single-page
dashboard (vanilla JS + Chart.js, no build step). From the **Backtest** tab you
pick a strategy/ticker/window and risk-and-exit parameters, run it, and see the
metric cards, equity curve, per-trade P&L and trade table render from a live
Tiger-data backtest. The **Scanner** tab runs the universe scan.

API surface (all under `/api`):

| method | path           | purpose                                    |
|--------|----------------|--------------------------------------------|
| GET    | `/api/health`  | liveness check                             |
| GET    | `/api/strategies` | list registered strategies              |
| POST   | `/api/backtest`| run a backtest, returns metrics + curves   |
| GET    | `/api/scan`    | run the universe scan                      |

The backtest endpoint accepts per-request risk overrides (max loss/trade,
%-equity sizing, take-profit/stop-loss %, max concurrent, cooldown), so you can
tune limits without editing `.env`.

`run_live()` (live execution) refuses to start while `BROKER_PROVIDER=paper`, and
the risk manager's daily kill-switch halts trading once `RISK_MAX_DAILY_LOSS` is
hit.

## Strategies

| name                 | idea                                                         |
|----------------------|--------------------------------------------------------------|
| `momentum_breakout`  | RSI/MACD/VWAP/EMA alignment → directional ATM call/put       |
| `iv_rank`            | buy premium when IV rank < 30, sell spreads when IV rank > 70 |
| `zero_dte`           | 0DTE/1DTE scalps in the 09:45–11:30 & 14:00–15:45 ET windows |

## Backtester

The backtester replays historical bars, prices fills off Tiger **option bars**
where available, and runs them through the same `PaperBroker` used live so the
commission and position logic match. Commission defaults to **$0.65/contract**
(Tiger/MooMoo typical) and is configurable via `COMMISSION_PER_CONTRACT` or the
`Backtester(commission_per_contract=...)` argument.

## Dependency notes

- `pandas-ta` 0.4.x (current PyPI line) is required — it's the build that works
  with numpy 2.x / pandas 3.x. The older 0.3.14b0 is no longer published.
- `tigeropen` and `moomoo-api` ship synchronous SDKs; all calls are threaded.

## Disclaimer

This is trading software operating on real markets and (optionally) real money.
Test thoroughly in paper/simulate modes. Use at your own risk.
