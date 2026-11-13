# TradingView → Paper Trading Bot

```
TradingView (real-time data + strategy.pine)
      │  alert fires on every strategy order
      ▼  HTTPS webhook (JSON)
server.py ── risk checks ── paper broker (built-in sim, or Alpaca paper)
      │
      └── dashboard at http://localhost:8080 (live TradingView chart + P&L)
```

TradingView has no public API for reading prices. The supported way to act on its
real-time data is to run the strategy **on TradingView** and have its alerts call a
webhook, which is how this project works.

## Files
| File | What it does |
|---|---|
| `strategy.pine` | Pine Script v6: EMA 9/21 crossover, RSI < 70 filter, 3% stop, 6% take-profit. Long only. |
| `server.py` | Webhook receiver, risk engine, and paper portfolio. Standard library only. State goes in `state.json`. |
| `dashboard.html` | Served at `/`. Embedded TradingView chart, equity, positions, trades, and the alert log. |
| `send_test_alert.py` | Sends a fake alert so you can test without TradingView. |
| `price_feed.pine` | Pine indicator that pushes live prices for up to 40 symbols to the server on every bar close. The auto-trader uses these first. |
| `backtest.py` | Swing-trading lab: 16 strategies + benchmarks on 4 years of daily bars. Writes `backtest.json`. |
| `daytrade.py` | Day-trading lab: 9 intraday strategies on the last 60 days of 5-minute bars. Writes `daytrade.json`. |
| `scanner.py` | Compares companies: ranks them, groups the ones that move together, and finds related pairs that have drifted apart. Writes `scan.json`. |

## Market scanner
```bash
python3 scanner.py                # 113 large, liquid US stocks across every sector
python3 scanner.py KO PEP V MA    # your own list
```
The server also runs it every 30 minutes (`SCAN_INTERVAL_MIN`), and the dashboard has a **Rescan** button.

- **Ranking:** a composite score made of 6M and 3M momentum, Sharpe ratio and max drawdown, each scored against the rest of the list. The table also shows volatility, RSI, distance from the 52-week high, and whether the price is above its 50-day and 200-day averages.
- **Clusters:** stocks whose daily returns move together, grouped with average-linkage clustering on 1 − correlation. If you hold three names from one cluster, that is really one bet.
- **Diverged pairs:** for each pair with correlation ≥ 0.6, the scanner regresses log(A) on log(B) and measures how stretched the spread is now (z). It runs an Engle-Granger test to see whether the gap has historically closed (mean reversion) and estimates its half-life. A pair is flagged when |z| ≥ 2. Click a pair to open the A÷B ratio chart on TradingView.

## Strategy lab
```bash
python3 backtest.py     # swing strategies, ~5 s
python3 daytrade.py     # day-trading strategies, ~7 s (first run downloads ~40 MB of 5-minute bars)
```
Or press **Run tests** in the dashboard's Strategy lab. Every strategy trades the same 113 stocks with the same $10,000 and the same costs. Click a row to plot it against the benchmarks.

**Swing (4 years of daily bars, Nov 2022 – Nov 2026).** Decisions happen at the close and fill at the next open. Stops and targets are checked against each day's high and low. Costs are 0.05% per side, with up to 10 positions of 10% each. The test is split in half (at Nov 2024) to see whether a strategy worked in both periods or just got lucky once.

| Strategy | Per year | Worst drop | Sharpe | Beat S&P in both halves? |
|---|---|---|---|---|
| Momentum rotation (top 10) | 44.8% | −39.7% | 1.20 | yes |
| Trend: 50-day above 200-day | 30.0% | −41.9% | 0.94 | yes |
| Momentum + market filter | 21.3% | −38.0% | 0.77 | yes |
| 52-week high breakout | 18.0% | −16.4% | 1.02 | no |
| Auto-trader (live rules, 10 slots) | 17.6% | −20.1% | 0.87 | no |
| RSI(2) dip-buy | 14.6% | −18.8% | 0.78 | no |
| Auto-trader as configured now (5 slots) | 9.0% | −16.6% | 0.68 | no |
| Bollinger / 20-day breakout / EMA cross | 1–6% | | < 0.4 | no |
| *S&P 500 (benchmark)* | *18.9%* | *−18.8%* | *1.22* | |
| *Buy & hold all 113 equally* | *24.0%* | *−20.6%* | *1.34* | |

**Day trading (60 days of 5-minute bars, Aug 21 – Nov 12 2026).** Long only and flat at every close. At most 5 trades are open at once. Costs are 0.03% per side. **Every day-trading strategy lost money after costs**, and most were roughly flat before costs. The more often a strategy traded, the worse it did: intraday RSI(2) made 84 trades a day and lost 46% after costs versus 2.5% before. The S&P 500 itself fell 1.2% over this window, and 60 days is a short sample.

**How to read this:**
- Nothing here simply beat buying and holding on a risk-adjusted basis (Sharpe). Momentum made the most money, but it had −40% drops along the way.
- The stock list is today's large companies, so every swing result is inflated by survivorship bias. Compare strategies with each other, not with real-world expectations.
- In the US, margin accounts under $25,000 may be limited in how many day trades they can make (pattern-day-trader rules). Check your broker's current rules.

## Auto-trader (simulator only)

**Default strategy: momentum rotation** (`AUTO_STRATEGY=momentum`). It was the best performer in the Strategy lab.
- It holds the 10 stocks with the best 12-month return excluding the latest month, at 10% of the account each.
- Every 28 days (about 21 trading days), it sells anything that dropped out of the top 10 and buys the newcomers. Between rebalances it only retries buys that were missed.
- There are no stops or targets. In the backtest that meant drops of up to −40%.
- If more than 5% of the account sits in cash, it tops up the most underweight holdings back to their 10% slot. That stays within the 20-trades-a-day limit.
- Momentum bunches into whatever is hot. The first live rebalance (Nov 13 2026) put 6 of 10 slots in chip stocks.

| Var | Default | Meaning |
|---|---|---|
| `AUTO_STRATEGY` | momentum | `momentum` or `original` (the rules below) |
| `AUTO_HOLD_COUNT` | 10 | How many top-momentum stocks to hold |
| `AUTO_REBALANCE_DAYS` | 28 | Calendar days between rebalances |
| `MAX_POSITIONS` | 10 | Maximum number of positions across the auto-trader and webhook trades |
| `FRACTIONAL_SHARES` | 1 | Buy fractional shares (e.g. 1.87 AMD) so every slot is exactly 10%; 0 = whole shares only |

### `original` strategy
It acts on every TradingView price push (each bar close), and also checks every 60 s (`AUTO_INTERVAL_SEC`) while the market is open. It uses TradingView prices for any symbol pushed in the last 3 minutes and Yahoo for the rest. Each trade's "Why" says which source it used. On each check it:
1. **Buys** names from the latest scan that it likes: score ≥ 0.5, above the 50-day and 200-day averages, and RSI < 70. It also buys the lagging leg of a stretched pair, but only if the pair passed the mean-reversion test. It holds up to `MAX_POSITIONS` names, each sized at `POSITION_PCT` of equity.
2. **Sets exit prices** from each stock's own volatility. One "move" is the typical 1-month swing: annual volatility ÷ √12. The target is entry + 1.5 moves and the stop is entry − 0.75 moves, so it risks $1 to make $2.
3. **Sells** when the target or stop is hit, after 30 days, or if the stock's score turns negative. Once a position is up one full move, the stop rises to the entry price so a winner can't turn into a loss. After an exit, it waits 3 days before buying the same name again.

It never sends orders to Alpaca, even if keys are set. The dashboard's Auto-trader card lists every name it considered and why it bought, waited or skipped.

| Var | Default | Meaning |
|---|---|---|
| `PRICE_SOURCE` | auto | `auto` = TradingView first, Yahoo fallback; `tradingview` = never use Yahoo (no trading when the feed is silent); `yahoo` |
| `TV_STALE_SEC` | 180 | A TradingView price older than this is ignored |
| `AUTO_TRADE` | 1 | Set to 0 to turn the auto-trader off |
| `AUTO_INTERVAL_SEC` | 60 | Seconds between price checks |
| `AUTO_MARKET_HOURS_ONLY` | 1 | Set to 0 to also trade on stale prices outside regular hours |
| `AUTO_MIN_SCORE` / `AUTO_MAX_RSI` | 0.5 / 70 | Buy filters |
| `AUTO_TP_SIGMA` / `AUTO_SL_SIGMA` | 1.5 / 0.75 | Target and stop distance, in monthly moves |
| `AUTO_MAX_HOLD_DAYS` / `AUTO_COOLDOWN_DAYS` | 30 / 3 | |

## 1. Run locally
```bash
export WEBHOOK_SECRET="pick-a-long-random-string"
python3 server.py
```
Open http://localhost:8080. To test:
```bash
python3 send_test_alert.py buy AAPL 228.40
python3 send_test_alert.py sell AAPL 231.10
```

## 2. Expose it to TradingView
TradingView must be able to reach the server over the public internet on port 80/443. For example:
```bash
ngrok http 8080
```
Your webhook URL is `https://<your-ngrok-id>.ngrok-free.app/webhook`.

## 3. Set up TradingView
1. Open a chart, go to **Pine Editor**, paste `strategy.pine`, then **Add to chart**. Use the Strategy Tester tab to check the backtest first.
2. Go to **Alerts**, then **Create alert**. For Condition, choose the strategy and "Order fills only".
3. In **Notifications**, tick **Webhook URL** and paste the URL from step 2.
4. Set the **Message** to this, replacing the secret with yours:
   ```json
   {"secret":"pick-a-long-random-string","ticker":"{{ticker}}","action":"{{strategy.order.action}}","price":{{close}},"time":"{{timenow}}"}
   ```
5. Repeat for each symbol you want traded. Webhook alerts require a paid TradingView plan.

### 3b. Live prices from TradingView (for the auto-trader)
1. Open a **1-minute** chart of any symbol, paste `price_feed.pine` into the Pine Editor, then **Add to chart**.
2. In the indicator's settings, set **Webhook secret** to your `WEBHOOK_SECRET`. Edit the symbol list if you like (40 max, comma-separated).
3. Create an alert. For Condition, choose "Price feed -> webhook" and "Any alert() function call". Tick **Webhook URL** and use the same URL as above. The message box is ignored.

That's one alert for all 40 symbols. On every 1-minute bar close, TradingView sends all their prices, and the server checks targets and stops right away. The dashboard header shows **TradingView feed · 40 symbols · Ns ago** when it's working.

**Faster than 1 minute?** A seconds chart (e.g. 15S) needs a TradingView plan with second-based intervals. TradingView also stops alerts that fire more than 15 times in 3 minutes, so 15 seconds is about the fastest safe setting.

## Risk settings (environment variables)
| Var | Default | Meaning |
|---|---|---|
| `STARTING_CASH` | 10000 | Starting balance for the simulator |
| `POSITION_PCT` | 10 | % of equity put into each new position |
| `MAX_POSITIONS` | 10 | Maximum number of positions open at once |
| `MAX_TRADES_PER_DAY` | 20 | Fills per UTC day |
| `MAX_DAILY_LOSS_PCT` | 3 | Blocks new buys for the rest of the day after this drawdown. Sells always go through. |
| `ALLOWED_TICKERS` | (any) | e.g. `AAPL,MSFT,NVDA` |
| `PORT` | 8080 | |

Delete `state.json` to reset the paper account (or point `STATE_FILE` somewhere else).

## Optional: Alpaca paper account
Set `ALPACA_KEY` and `ALPACA_SECRET` (from a **paper** account at alpaca.markets). Orders are then
also sent to Alpaca's paper endpoint as market orders. The URL is hard-coded to paper on purpose.
Before you point anything at real money, run it on paper for a while and compare the fills with the backtest.

## Limitations
- The fill price is the bar `close` from the alert. Real fills will differ (slippage, latency).
- Alerts can arrive late or more than once. The server ignores a duplicate buy when it is already long, and ignores a sell when it has no position.
- Positions are marked to market only when a new alert for that ticker arrives.
- This is a demo strategy, not a profitable one.
- TradingView has no API for pulling prices, so live prices arrive as pushes from `price_feed.pine`. The scanner's one-year daily history still comes from Yahoo's unofficial endpoint, where speed doesn't matter. Yahoo is also the live fallback when the TradingView feed is silent.
- On a basic TradingView plan, US stock prices may come from Cboe One rather than the full consolidated feed. That's fine for a 1-minute bot, but not tick-accurate.
- The auto-trader fills at the last traded price with no slippage or commission. Its rules ("momentum + uptrend, volatility-sized exits") are a reasonable starting point, not a proven edge, so judge them by weeks of results, not one day.
