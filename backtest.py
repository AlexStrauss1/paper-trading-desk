#!/usr/bin/env python3
"""
Strategy lab: backtests several strategies on the same stocks, same period, same costs.

- Daily bars, 5 years (first year = indicator warm-up, so ~4 years are traded).
- Decisions at the close, orders filled at the NEXT day's open (no look-ahead).
- Stops/targets checked against each day's high/low; if both could have hit, the stop is
  assumed to hit first (conservative).
- Costs: COST_BPS per side (commission + slippage), default 5 bps = 0.05%.
- Sizing: each position = 10% of equity, max 10 positions (fractional shares).

Usage:  python3 backtest.py            # writes backtest.json, prints a summary
Standard library only.
"""
import gzip
import json
import math
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from itertools import accumulate
from operator import truediv
from statistics import mean, pstdev

import scanner

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "backtest.json"
CACHE_FILE = HERE / "backtest_data.json.gz"
CAPITAL = 10_000.0
COST = float(os.environ.get("COST_BPS", "5")) / 10_000
WARMUP = 252

PAIRS = [("KO", "PEP"), ("V", "MA"), ("XOM", "CVX"), ("CVX", "COP"), ("HD", "LOW"), ("JPM", "BAC"),
         ("GS", "MS"), ("UPS", "FDX"), ("MCD", "YUM"), ("WMT", "COST"), ("PG", "CL"), ("AMD", "NVDA"),
         ("JPM", "WFC"), ("MRK", "PFE"), ("GOOGL", "META")]


# ---------------------------------------------------------------- data
def fetch_ohlc(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5y&interval=1d"
    res = scanner.get_json(url, timeout=20)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    rows = {}
    for k, ts in enumerate(res["timestamp"]):
        o, h, l, c, a = q["open"][k], q["high"][k], q["low"][k], q["close"][k], adj[k]
        if None in (o, h, l, c, a) or c <= 0:
            continue
        f = a / c  # split/dividend adjustment
        rows[datetime.fromtimestamp(ts, timezone.utc).date().isoformat()] = (o * f, h * f, l * f, a)
    return rows


def load_data(tickers):
    if CACHE_FILE.exists() and time.time() - CACHE_FILE.stat().st_mtime < 12 * 3600:
        cached = json.loads(gzip.decompress(CACHE_FILE.read_bytes()))
        if set(tickers) <= set(cached["raw"]) and "SPY" in cached["raw"]:
            raw = cached["raw"]
            return align(raw, tickers)
    raw = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, fut in [(t, pool.submit(fetch_ohlc, t)) for t in [*tickers, "SPY"]]:
            try:
                raw[t] = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"skip {t}: {e}", file=sys.stderr)
    CACHE_FILE.write_bytes(gzip.compress(json.dumps({"raw": raw}, separators=(",", ":")).encode(), 6))
    return align(raw, tickers)


def align(raw, tickers):
    # Drop tickers with a shorter history than SPY (recent IPOs) so they don't shrink the test window.
    tickers = [t for t in tickers if t in raw and len(raw[t]) >= len(raw["SPY"]) * 0.98]
    dates = sorted(set.intersection(*(set(raw[t]) for t in tickers + ["SPY"])))
    D = {}
    for t in tickers + ["SPY"]:
        rows = [raw[t][d] for d in dates]
        D[t] = {"o": [r[0] for r in rows], "h": [r[1] for r in rows], "l": [r[2] for r in rows], "c": [r[3] for r in rows]}
    return tickers, dates, D


# ---------------------------------------------------------------- indicators
def sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n):
    out, k, e = [None] * len(x), 2 / (n + 1), None
    for i, v in enumerate(x):
        e = v if e is None else v * k + e * (1 - k)
        if i >= n - 1:
            out[i] = e
    return out


def ema_raw(x, n):
    out, k, e = [], 2 / (n + 1), None
    for v in x:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(c, n):
    out, g, l = [None] * len(c), 0.0, 0.0
    d = [0.0] + [c[i] - c[i - 1] for i in range(1, len(c))]
    for i in range(1, len(c)):
        g += max(d[i], 0)
        l += max(-d[i], 0)
        if i > n:
            g -= max(d[i - n], 0)
            l -= max(-d[i - n], 0)
        if i >= n:
            out[i] = 100.0 if l <= 1e-12 else 100 - 100 / (1 + g / l)
    return out


def indicators(D, tickers):
    for t in tickers:
        c = D[t]["c"]
        n = len(c)
        r = [0.0] + [math.log(c[i] / c[i - 1]) for i in range(1, n)]
        vol, shp, dd = [None] * n, [None] * n, [None] * n
        s = s2 = 0.0
        for i in range(n):
            s += r[i]
            s2 += r[i] * r[i]
            if i >= WARMUP:
                s -= r[i - WARMUP]
                s2 -= r[i - WARMUP] ** 2
            if i >= WARMUP - 1:
                m = s / WARMUP
                sd = math.sqrt(max(s2 / WARMUP - m * m, 1e-12))
                vol[i] = sd * math.sqrt(252)
                shp[i] = m * 252 / vol[i]
                w = c[i - WARMUP + 1:i + 1]
                dd[i] = min(map(truediv, w, accumulate(w, max))) - 1
        sd20, a, a2 = [None] * n, 0.0, 0.0
        for i in range(n):
            a += c[i]
            a2 += c[i] * c[i]
            if i >= 20:
                a -= c[i - 20]
                a2 -= c[i - 20] ** 2
            if i >= 19:
                m = a / 20
                sd20[i] = math.sqrt(max(a2 / 20 - m * m, 0.0))
        e12, e26 = ema_raw(c, 12), ema_raw(c, 26)
        macd = [a - b for a, b in zip(e12, e26)]
        hi, lo = D[t]["h"], D[t]["l"]
        rng = [h_ - l_ for h_, l_ in zip(hi, lo)]
        s20 = sma(c, 20)
        bw = [4 * sd20[i] / s20[i] if sd20[i] and s20[i] else None for i in range(n)]
        D[t].update(
            hh10=[max(c[i - 10:i]) if i >= 10 else None for i in range(n)],
            ll7=[min(c[i - 6:i + 1]) if i >= 6 else None for i in range(n)],
            hh7=[max(c[i - 6:i + 1]) if i >= 6 else None for i in range(n)],
            nr7=[i >= 6 and rng[i] == min(rng[i - 6:i + 1]) for i in range(n)],
            bw=bw, bw_min120=[min(x for x in bw[i - 119:i + 1] if x is not None) if i >= 140 else None for i in range(n)],
            sma20=s20, sd20=sd20, macd=macd, macd_sig=ema_raw(macd, 9),
            hh20=[max(c[i - 20:i]) if i >= 20 else None for i in range(n)],
            ll10=[min(c[i - 10:i]) if i >= 10 else None for i in range(n)],
            ll20=[min(c[i - 20:i]) if i >= 20 else None for i in range(n)],
            hh252=[max(c[i - 252:i]) if i >= 252 else None for i in range(n)],
            ret5=[c[i] / c[i - 5] - 1 if i >= 5 else None for i in range(n)],
            sma5=sma(c, 5), sma50=sma(c, 50), sma200=sma(c, 200), ema9=ema(c, 9), ema21=ema(c, 21),
            rsi14=rsi(c, 14), rsi2=rsi(c, 2), vol=vol, sharpe=shp, dd=dd,
            ret63=[c[i] / c[i - 63] - 1 if i >= 63 else None for i in range(n)],
            ret126=[c[i] / c[i - 126] - 1 if i >= 126 else None for i in range(n)],
            mom12_1=[c[i - 21] / c[i - 252] - 1 if i >= 252 else None for i in range(n)],
        )


def scores(D, tickers, n):
    """Same composite as scanner.py, recomputed every day using only data up to that day."""
    out = [dict() for _ in range(n)]
    for i in range(WARMUP, n):
        ts = [t for t in tickers if D[t]["ret126"][i] is not None and D[t]["sharpe"][i] is not None]
        cols = {k: [D[t][k][i] for t in ts] for k in ("ret126", "ret63", "sharpe", "dd")}
        z = {}
        for k, vals in cols.items():
            m = sum(vals) / len(vals)
            sd = math.sqrt(max(sum(v * v for v in vals) / len(vals) - m * m, 0.0))
            z[k] = [(v - m) / sd if sd else 0.0 for v in vals]
        for j, t in enumerate(ts):
            out[i][t] = 0.35 * z["ret126"][j] + 0.25 * z["ret63"][j] + 0.25 * z["sharpe"][j] + 0.15 * z["dd"][j]
    return out


# ---------------------------------------------------------------- engine
class Ctx:
    def __init__(self, D, tickers, dates, score):
        self.D, self.tickers, self.dates, self.score = D, tickers, dates, score
        self.last_exit = {}


def simulate(strat, ctx, max_positions=10, position_pct=0.10):
    D, n = ctx.D, len(ctx.dates)
    ctx.last_exit = {}
    cash, pos = CAPITAL, {}
    pending_sells, pending_buys = {}, []
    curve, trades, invested = [], [], []
    prev_equity = CAPITAL

    def close_pos(t, px, i, why):
        nonlocal cash
        p = pos.pop(t)
        if p["side"] > 0:
            fill = px * (1 - COST)
            cash += p["qty"] * fill
            ret = fill / p["entry"] - 1
        else:  # buy back a short
            fill = px * (1 + COST)
            cash -= p["qty"] * fill
            ret = p["entry"] / fill - 1
        trades.append({"ticker": t, "entry": p["entry"], "exit": fill, "ret": ret, "side": p["side"],
                       "bars": i - p["i"], "why": why, "date": ctx.dates[i]})
        ctx.last_exit[t] = i

    for i in range(WARMUP, n):
        # 1) fills at today's open
        for t, why in pending_sells.items():
            if t in pos:
                close_pos(t, D[t]["o"][i], i, why)
        for t, why, lv in pending_buys:
            if t in pos or len(pos) >= max_positions:
                continue
            side = lv.get("side", 1)
            if side > 0:
                px = D[t]["o"][i] * (1 + COST)
                size = min(prev_equity * position_pct, cash)
                if size < prev_equity * position_pct * 0.5:
                    continue
                cash -= size
            else:  # short: receive the proceeds, owe the shares; total shorts capped at 100% of equity
                px = D[t]["o"][i] * (1 - COST)
                size = prev_equity * position_pct
                short_now = sum(q["qty"] * D[u]["o"][i] for u, q in pos.items() if q["side"] < 0)
                if short_now + size > max(prev_equity, 0):
                    continue
                cash += size
            pos[t] = {"qty": size / px, "entry": px, "i": i, "why": why, "meta": lv.get("meta"), "side": side,
                      "tp": px * (1 + side * lv["tp"]) if lv.get("tp") else None,
                      "sl": px * (1 - side * lv["sl"]) if lv.get("sl") else None,
                      "trail": lv.get("trail"), "max_hold": lv.get("max_hold"), "trailed": False}
        pending_sells, pending_buys = {}, []

        # 2) intraday stops / targets
        for t in list(pos):
            p, o, h, l = pos[t], D[t]["o"][i], D[t]["h"][i], D[t]["l"][i]
            if p["side"] > 0:
                if p["sl"] and l <= p["sl"]:
                    close_pos(t, min(o, p["sl"]), i, "breakeven stop" if p["trailed"] else "stop")
                elif p["tp"] and h >= p["tp"]:
                    close_pos(t, max(o, p["tp"]), i, "target")
            else:
                if p["sl"] and h >= p["sl"]:
                    close_pos(t, max(o, p["sl"]), i, "stop")
                elif p["tp"] and l <= p["tp"]:
                    close_pos(t, min(o, p["tp"]), i, "target")

        # 3) end of day: trail stops, mark to market
        for t, p in pos.items():
            c = D[t]["c"][i]
            if p["side"] > 0 and p["trail"] and not p["trailed"] and c >= p["entry"] * (1 + p["trail"]):
                p["sl"], p["trailed"] = max(p["sl"] or 0, p["entry"]), True
        held_value = sum(p["side"] * p["qty"] * D[t]["c"][i] for t, p in pos.items())
        gross = sum(p["qty"] * D[t]["c"][i] for t, p in pos.items())
        equity = cash + held_value
        curve.append(equity)
        invested.append(gross / equity if equity else 0)
        prev_equity = equity

        # 4) decide (acts at tomorrow's open)
        if i < n - 1:
            for t, p in pos.items():
                if p["max_hold"] and i - p["i"] + 1 >= p["max_hold"]:
                    pending_sells[t] = "time"
            exits, entries = strat.decide(i, ctx, pos)
            for t, why in exits.items():
                pending_sells.setdefault(t, why)
            pending_buys = [e for e in entries if e[0] not in pos]

    for t in list(pos):  # close anything still open at the last close, for trade stats
        close_pos(t, D[t]["c"][n - 1], n - 1, "end of test")
    return curve, trades, invested


# ---------------------------------------------------------------- strategies
def due(ctx, i, every):
    """Rebalance schedule: every N bars in a backtest; the live runner sets ctx.force_rebalance instead."""
    forced = getattr(ctx, "force_rebalance", None)
    return forced if forced is not None else (i - WARMUP) % every == 0


class AutoTrader:
    """The live auto-trader's rules (server.py), replayed day by day."""
    def __init__(self, name, tp_sigma=1.5, sl_sigma=0.75, desc=None):
        self.name, self.tp, self.sl = name, tp_sigma, sl_sigma
        self.desc = desc or (f"Buys top-ranked stocks (score ≥ 0.5) in an uptrend with RSI < 70. Target +{tp_sigma}× and stop "
                             f"−{sl_sigma}× the stock's monthly move, stop to breakeven after +1×, max 21 trading days, "
                             "sell if score < 0.")

    def decide(self, i, ctx, pos):
        D, sc = ctx.D, ctx.score[i]
        exits = {t: "score < 0" for t in pos if sc.get(t, 0) < 0}
        entries = []
        for t in sorted(sc, key=sc.get, reverse=True):
            d = D[t]
            if sc[t] < 0.5:
                break
            if t in pos or i - ctx.last_exit.get(t, -99) < 3:
                continue
            if d["c"][i] > d["sma50"][i] and d["c"][i] > d["sma200"][i] and d["rsi14"][i] < 70:
                m = d["vol"][i] / math.sqrt(12)
                entries.append((t, "momentum", {"tp": self.tp * m if self.tp else None, "sl": self.sl * m,
                                                "trail": m, "max_hold": 21}))
        return exits, entries


class EmaCross:
    name = "EMA 9/21 cross + RSI"
    desc = "Same rules as strategy.pine: buy when EMA 9 crosses above EMA 21 with RSI < 70; sell on the cross back down, 3% stop, 6% target."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "cross down" for t in pos if D[t]["ema9"][i] < D[t]["ema21"][i]}
        entries = []
        for t in ctx.tickers:
            d = D[t]
            if d["ema9"][i] > d["ema21"][i] and d["ema9"][i - 1] <= d["ema21"][i - 1] and d["rsi14"][i] < 70:
                entries.append((t, "cross up", {"tp": 0.06, "sl": 0.03}))
        entries.sort(key=lambda e: ctx.score[i].get(e[0], -9), reverse=True)
        return exits, entries


class MomentumRotation:
    name = "Momentum rotation (top 10)"
    desc = "Every 21 trading days, hold the 10 stocks with the best 12-month return excluding the last month. No stops."

    rebalance_every = 21

    def decide(self, i, ctx, pos):
        if not due(ctx, i, self.rebalance_every):
            return {}, []
        D = ctx.D
        ranked = sorted((t for t in ctx.tickers if D[t]["mom12_1"][i] is not None),
                        key=lambda t: D[t]["mom12_1"][i], reverse=True)[:10]
        exits = {t: "rotated out" for t in pos if t not in ranked}
        return exits, [(t, "top 10 momentum", {}) for t in ranked if t not in pos]


class Rsi2Dip:
    name = "RSI(2) dip-buy"
    desc = "Buys short, sharp dips in uptrends: price above its 200-day average and 2-day RSI < 10. Sells when price closes above its 5-day average, or after 10 days."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "bounced" for t in pos if D[t]["c"][i] > D[t]["sma5"][i]}
        entries = [(t, "dip", {"max_hold": 10}) for t in ctx.tickers
                   if D[t]["c"][i] > D[t]["sma200"][i] and D[t]["rsi2"][i] < 10]
        entries.sort(key=lambda e: D[e[0]]["rsi2"][i])
        return exits, entries


class PairsLaggard:
    name = "Pairs: buy the laggard"
    desc = "For 15 same-industry pairs, when the price ratio is > 2 std devs from its 60-day mean, buy the laggard. Sell when the gap closes (|z| < 0.5), after 20 days, or at an 8% stop."

    def __init__(self):
        self.z = None

    def prepare(self, ctx):
        D, n, self.z = ctx.D, len(ctx.dates), {}
        for a, b in PAIRS:
            if a not in D or b not in D:
                continue
            lr = [math.log(D[a]["c"][i] / D[b]["c"][i]) for i in range(n)]
            z = [None] * n
            for i in range(60, n):
                w = lr[i - 59:i + 1]
                m, sd = mean(w), pstdev(w)
                z[i] = (lr[i] - m) / sd if sd else 0.0
            self.z[(a, b)] = z

    def decide(self, i, ctx, pos):
        if self.z is None:
            self.prepare(ctx)
        exits, entries = {}, []
        for t, p in pos.items():
            pair = p["meta"]
            if pair and abs(self.z[tuple(pair)][i]) < 0.5:
                exits[t] = "gap closed"
        for (a, b), z in self.z.items():
            if z[i] is None:
                continue
            if z[i] > 2:
                entries.append((b, f"{b} lags {a}", {"sl": 0.08, "max_hold": 20, "meta": (a, b)}))
            elif z[i] < -2:
                entries.append((a, f"{a} lags {b}", {"sl": 0.08, "max_hold": 20, "meta": (a, b)}))
        entries.sort(key=lambda e: -abs(self.z[e[2]["meta"]][i]))
        return exits, entries


class DonchianBreakout:
    name = "Breakout: 20-day high (turtle)"
    desc = "Buys when a stock closes above its highest close of the last 20 days; sells when it closes below its lowest close of the last 10 days. Classic trend-following."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "10-day low" for t in pos if D[t]["c"][i] < D[t]["ll10"][i]}
        entries = [(t, "20-day high", {}) for t in ctx.tickers if D[t]["c"][i] > D[t]["hh20"][i]]
        entries.sort(key=lambda e: ctx.score[i].get(e[0], -9), reverse=True)
        return exits, entries


class GoldenCross:
    name = "Trend: 50-day above 200-day"
    desc = "Holds stocks whose 50-day average is above their 200-day average (a 'golden cross' uptrend); sells when it drops below. Strongest momentum first."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "death cross" for t in pos if D[t]["sma50"][i] < D[t]["sma200"][i]}
        entries = [(t, "golden cross", {}) for t in ctx.tickers
                   if D[t]["sma50"][i] > D[t]["sma200"][i] and D[t]["c"][i] > D[t]["sma50"][i]]
        entries.sort(key=lambda e: D[e[0]]["ret126"][i] or -9, reverse=True)
        return exits, entries


class BollingerReversion:
    name = "Bollinger band dip-buy"
    desc = "In stocks above their 200-day average, buys a close below the lower Bollinger band (20-day average minus 2 std devs); sells back at the 20-day average, after 10 days, or at an 8% stop."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "back to average" for t in pos if D[t]["c"][i] > D[t]["sma20"][i]}
        entries = [(t, "below lower band", {"sl": 0.08, "max_hold": 10}) for t in ctx.tickers
                   if D[t]["c"][i] > D[t]["sma200"][i] and D[t]["c"][i] < D[t]["sma20"][i] - 2 * D[t]["sd20"][i]]
        entries.sort(key=lambda e: (D[e[0]]["c"][i] - D[e[0]]["sma20"][i]) / D[e[0]]["sd20"][i])
        return exits, entries


class MacdTrend:
    name = "MACD cross (in uptrends)"
    desc = "Buys when MACD (12/26) crosses above its 9-day signal line while the stock is above its 200-day average; sells on the cross back down."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "MACD cross down" for t in pos if D[t]["macd"][i] < D[t]["macd_sig"][i]}
        entries = [(t, "MACD cross up", {}) for t in ctx.tickers
                   if D[t]["macd"][i] > D[t]["macd_sig"][i] and D[t]["macd"][i - 1] <= D[t]["macd_sig"][i - 1]
                   and D[t]["c"][i] > D[t]["sma200"][i]]
        entries.sort(key=lambda e: ctx.score[i].get(e[0], -9), reverse=True)
        return exits, entries


class DualMomentum(MomentumRotation):
    name = "Momentum rotation + market filter"
    desc = "Same as momentum rotation, but moves everything to cash whenever the S&P 500 is below its 200-day average (to dodge bear markets)."

    def decide(self, i, ctx, pos):
        spy = ctx.D["SPY"]
        if spy["c"][i] < spy["sma200"][i]:
            return {t: "market below 200-day" for t in pos}, []
        return super().decide(i, ctx, pos)


class LowVolatility:
    name = "Low volatility (10 calmest)"
    desc = "Every 21 trading days, holds the 10 stocks with the lowest 1-year volatility. A defensive factor strategy."

    rebalance_every = 21

    def decide(self, i, ctx, pos):
        if not due(ctx, i, self.rebalance_every):
            return {}, []
        D = ctx.D
        ranked = sorted((t for t in ctx.tickers if D[t]["vol"][i]), key=lambda t: D[t]["vol"][i])[:10]
        return {t: "rotated out" for t in pos if t not in ranked}, [(t, "low vol", {}) for t in ranked if t not in pos]


class NewHighs:
    name = "52-week high breakout + 10% trailing stop"
    desc = "Buys stocks closing at a new 52-week high; rides them with a stop 10% below the highest close since entry, for up to 60 days."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {}
        for t, p in pos.items():
            peak = max(D[t]["c"][p["i"]:i + 1])
            if D[t]["c"][i] < peak * 0.90:
                exits[t] = "trailing stop"
        entries = [(t, "new 52w high", {"max_hold": 60}) for t in ctx.tickers if D[t]["c"][i] > D[t]["hh252"][i]]
        entries.sort(key=lambda e: ctx.score[i].get(e[0], -9), reverse=True)
        return exits, entries


class WeeklyReversal:
    name = "Weekly losers bounce"
    desc = "Every 5 days, buys the 10 stocks that fell the most over the past week (only if still above their 200-day average) and holds them 5 days."

    rebalance_every = 5

    def decide(self, i, ctx, pos):
        if not due(ctx, i, self.rebalance_every):
            return {}, []
        D = ctx.D
        cands = sorted((t for t in ctx.tickers if D[t]["c"][i] > D[t]["sma200"][i]), key=lambda t: D[t]["ret5"][i])[:10]
        return ({t: "rotated out" for t in pos if t not in cands},
                [(t, "weekly loser", {}) for t in cands if t not in pos])


class ThreeDownDays:
    name = "Swing: 3 down days in an uptrend"
    desc = "Buys a stock above its 200-day average after 3 lower closes in a row; sells when it closes above the previous day's high, after 7 days, or at an 8% stop."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "closed above prior high" for t in pos if D[t]["c"][i] > D[t]["h"][i - 1]}
        entries = [(t, "3 down days", {"sl": 0.08, "max_hold": 7}) for t in ctx.tickers
                   if D[t]["c"][i] > D[t]["sma200"][i] and D[t]["c"][i] < D[t]["c"][i - 1] < D[t]["c"][i - 2] < D[t]["c"][i - 3]]
        entries.sort(key=lambda e: ctx.score[i].get(e[0], -9), reverse=True)
        return exits, entries


class DoubleSevens:
    name = "Swing: 7-day low / 7-day high"
    desc = "Buys a stock above its 200-day average when it closes at a 7-day low; sells when it closes at a 7-day high, or after 15 days."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "7-day high" for t in pos if D[t]["c"][i] >= D[t]["hh7"][i]}
        entries = [(t, "7-day low", {"max_hold": 15}) for t in ctx.tickers
                   if D[t]["c"][i] > D[t]["sma200"][i] and D[t]["c"][i] <= D[t]["ll7"][i]]
        entries.sort(key=lambda e: ctx.score[i].get(e[0], -9), reverse=True)
        return exits, entries


class MomentumPullback:
    name = "Swing: buy pullbacks in leaders"
    desc = "Among the 20 highest-scoring stocks, buys one that dips below its 21-day EMA while staying above its 50-day average; sells when it makes a new 10-day high, after 15 days, or at a 7% stop."

    def decide(self, i, ctx, pos):
        D, sc = ctx.D, ctx.score[i]
        leaders = sorted(sc, key=sc.get, reverse=True)[:20]
        exits = {t: "new 10-day high" for t in pos if D[t]["c"][i] > D[t]["hh10"][i]}
        entries = [(t, "leader pullback", {"sl": 0.07, "max_hold": 15}) for t in leaders
                   if D[t]["c"][i] < D[t]["ema21"][i] and D[t]["c"][i] > D[t]["sma50"][i]]
        return exits, entries


class Nr7Breakout:
    name = "Swing: tight-range (NR7) breakout"
    desc = "In uptrends (price > 50-day > 200-day), after the narrowest daily range of the last 7 days, buys a close above that day's high. 6% target, 3% stop, 10 days max."

    def decide(self, i, ctx, pos):
        D = ctx.D
        entries = [(t, "NR7 breakout", {"tp": 0.06, "sl": 0.03, "max_hold": 10}) for t in ctx.tickers
                   if D[t]["nr7"][i - 1] and D[t]["c"][i] > D[t]["h"][i - 1]
                   and D[t]["c"][i] > D[t]["sma50"][i] > D[t]["sma200"][i]]
        entries.sort(key=lambda e: ctx.score[i].get(e[0], -9), reverse=True)
        return {}, entries


class SqueezeBreakout:
    name = "Swing: Bollinger squeeze breakout"
    desc = "When a stock's Bollinger bands are the tightest in 6 months and it closes above the upper band, buys; sells on a close below the 20-day average, after 20 days, or at an 8% stop."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "below 20-day" for t in pos if D[t]["c"][i] < D[t]["sma20"][i]}
        entries = []
        for t in ctx.tickers:
            d = D[t]
            if d["bw_min120"][i] is None or d["bw"][i - 1] is None:
                continue
            if d["bw"][i - 1] <= d["bw_min120"][i - 1] * 1.1 and d["c"][i] > d["sma20"][i] + 2 * d["sd20"][i]:
                entries.append((t, "squeeze breakout", {"sl": 0.08, "max_hold": 20}))
        entries.sort(key=lambda e: ctx.score[i].get(e[0], -9), reverse=True)
        return exits, entries


class Rsi2Leaders:
    name = "Swing: RSI(2) dips in strong stocks"
    desc = "Like the RSI(2) dip-buy, but only in the 30 highest-scoring stocks: buys when 2-day RSI < 10, sells on a close above the 5-day average or after 10 days."

    def decide(self, i, ctx, pos):
        D, sc = ctx.D, ctx.score[i]
        leaders = sorted(sc, key=sc.get, reverse=True)[:30]
        exits = {t: "bounced" for t in pos if D[t]["c"][i] > D[t]["sma5"][i]}
        entries = [(t, "leader dip", {"max_hold": 10}) for t in leaders if D[t]["rsi2"][i] < 10]
        entries.sort(key=lambda e: D[e[0]]["rsi2"][i])
        return exits, entries


SHORT = {"side": -1}


class DoubleSevensShort:
    name = "Short: 7-day high in a downtrend"
    desc = "Mirror of the 7-day low/high swing: shorts a stock below its 200-day average when it closes at a 7-day high; covers at a 7-day low, or after 15 days."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "7-day low" for t in pos if D[t]["c"][i] <= D[t]["ll7"][i]}
        entries = [(t, "7-day high in downtrend", {**SHORT, "max_hold": 15}) for t in ctx.tickers
                   if D[t]["c"][i] < D[t]["sma200"][i] and D[t]["c"][i] >= D[t]["hh7"][i]]
        entries.sort(key=lambda e: ctx.score[i].get(e[0], 9))
        return exits, entries


class DoubleSevensBoth:
    name = "Long/short: 7-day low & high"
    desc = "Runs both sides at once: buys 7-day lows in uptrends and shorts 7-day highs in downtrends. Exits at the opposite 7-day extreme or after 15 days."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {}
        for t, p in pos.items():
            if p["side"] > 0 and D[t]["c"][i] >= D[t]["hh7"][i]:
                exits[t] = "7-day high"
            elif p["side"] < 0 and D[t]["c"][i] <= D[t]["ll7"][i]:
                exits[t] = "7-day low"
        entries = []
        for t in ctx.tickers:
            d = D[t]
            if d["c"][i] > d["sma200"][i] and d["c"][i] <= d["ll7"][i]:
                entries.append((t, "7-day low in uptrend", {"max_hold": 15}))
            elif d["c"][i] < d["sma200"][i] and d["c"][i] >= d["hh7"][i]:
                entries.append((t, "7-day high in downtrend", {**SHORT, "max_hold": 15}))
        entries.sort(key=lambda e: abs(ctx.score[i].get(e[0], 0)), reverse=True)
        return exits, entries


class MomentumLongShort:
    name = "Long/short momentum (10 long, 10 short)"
    desc = "Every 21 trading days: long the 10 best 12-month performers, short the 10 worst. Roughly market-neutral."
    rebalance_every = 21

    def decide(self, i, ctx, pos):
        if not due(ctx, i, self.rebalance_every):
            return {}, []
        D = ctx.D
        ranked = sorted((t for t in ctx.tickers if D[t]["mom12_1"][i] is not None), key=lambda t: D[t]["mom12_1"][i], reverse=True)
        longs, shorts = ranked[:10], ranked[-10:]
        want = {**{t: 1 for t in longs}, **{t: -1 for t in shorts}}
        exits = {t: "rotated out" for t, p in pos.items() if want.get(t) != p["side"]}
        entries = [(t, "top 10", {}) for t in longs if t not in pos or t in exits]
        entries += [(t, "bottom 10", dict(SHORT)) for t in shorts if t not in pos or t in exits]
        return exits, entries


class BreakdownShort:
    name = "Short: 20-day low breakdown"
    desc = "Shorts a stock below its 200-day average when it closes under its lowest close of the last 20 days; covers on a close above its 10-day high, after 20 days, or at an 8% stop."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "10-day high" for t in pos if D[t]["c"][i] > D[t]["hh10"][i]}
        entries = [(t, "20-day low breakdown", {**SHORT, "sl": 0.08, "max_hold": 20}) for t in ctx.tickers
                   if D[t]["c"][i] < D[t]["sma200"][i] and D[t]["c"][i] < D[t]["ll20"][i]]
        entries.sort(key=lambda e: ctx.score[i].get(e[0], 9))
        return exits, entries


class OverboughtShort:
    name = "Short: overbought pops in downtrends"
    desc = "Shorts a stock below its 200-day average when its 2-day RSI is above 90; covers on a close below its 5-day average, or after 10 days."

    def decide(self, i, ctx, pos):
        D = ctx.D
        exits = {t: "faded" for t in pos if D[t]["c"][i] < D[t]["sma5"][i]}
        entries = [(t, "overbought pop", {**SHORT, "max_hold": 10}) for t in ctx.tickers
                   if D[t]["c"][i] < D[t]["sma200"][i] and D[t]["rsi2"][i] > 90]
        entries.sort(key=lambda e: -D[e[0]]["rsi2"][i])
        return exits, entries


class PairsLongShort(PairsLaggard):
    name = "Pairs: long laggard / short leader"
    desc = "Classic pairs trade on 15 same-industry pairs: when the price ratio is > 2 std devs from its 60-day mean, buys the laggard AND shorts the leader. Both legs close when |z| < 0.5, after 20 days, or at an 8% stop."

    def decide(self, i, ctx, pos):
        if self.z is None:
            self.prepare(ctx)
        exits = {t: "gap closed" for t, p in pos.items() if p["meta"] and abs(self.z[tuple(p["meta"])][i]) < 0.5}
        entries = []
        for (a, b), z in self.z.items():
            if z[i] is None or a in pos or b in pos:
                continue
            if z[i] > 2:
                entries += [(b, f"{b} lags {a}", {"sl": 0.08, "max_hold": 20, "meta": (a, b)}),
                            (a, f"{a} leads {b}", {**SHORT, "sl": 0.08, "max_hold": 20, "meta": (a, b)})]
            elif z[i] < -2:
                entries += [(a, f"{a} lags {b}", {"sl": 0.08, "max_hold": 20, "meta": (a, b)}),
                            (b, f"{b} leads {a}", {**SHORT, "sl": 0.08, "max_hold": 20, "meta": (a, b)})]
        return exits, entries


class BuyHoldEqual:
    name = "Buy & hold every stock (equal weight)"
    desc = "Benchmark: buy every stock in the list on day one in equal amounts and never trade again."

    def decide(self, i, ctx, pos):
        return ({}, [(t, "hold", {}) for t in ctx.tickers]) if i == WARMUP else ({}, [])


# ---------------------------------------------------------------- metrics
def metrics(curve, trades, invested):
    rets = [b / a - 1 for a, b in zip(curve, curve[1:])]
    sd = pstdev(rets)
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    years = len(curve) / 252
    wins = [t for t in trades if t["ret"] > 0]
    gross_win = sum(t["ret"] for t in wins)
    gross_loss = -sum(t["ret"] for t in trades if t["ret"] <= 0)
    return {
        "final": round(curve[-1], 2),
        "total_return": round(curve[-1] / CAPITAL - 1, 4),
        "cagr": round((curve[-1] / CAPITAL) ** (1 / years) - 1, 4) if curve[-1] > 0 else -1.0,
        "max_drawdown": round(mdd, 4),
        "volatility": round(sd * math.sqrt(252), 4),
        "sharpe": round(mean(rets) / sd * math.sqrt(252), 2) if sd else 0,
        "trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "avg_trade": round(mean(t["ret"] for t in trades), 4) if trades else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "avg_hold_days": round(mean(t["bars"] for t in trades), 1) if trades else None,
        "exposure": round(mean(invested), 3),
    }


def yearly(curve, dates):
    out, start_v, year = {}, CAPITAL, dates[0][:4]
    for d, v, prev in zip(dates, curve, [CAPITAL] + curve[:-1]):
        if d[:4] != year:
            out[year] = round(prev / start_v - 1, 4)
            start_v, year = prev, d[:4]
    out[year] = round(curve[-1] / start_v - 1, 4)
    return out


def halves(curve):
    h = len(curve) // 2
    yrs = h / 252
    g = lambda x: round(x ** (1 / yrs) - 1, 4) if x > 0 else -1.0  # noqa: E731
    return {"first_half_cagr": g(curve[h - 1] / CAPITAL), "second_half_cagr": g(curve[-1] / curve[h - 1]) if curve[h - 1] > 0 else -1.0}


def exit_mix(trades):
    mix = {}
    for t in trades:
        mix[t["why"]] = mix.get(t["why"], 0) + 1
    return dict(sorted(mix.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------- main
# Position size per trade (fraction of equity) for each strategy, picked in the Nov 2026 sweep
# (2.5/5/10/20%, best Sharpe, no cap on the number of positions). server.py uses the same numbers.
LIVE_SIZES = {
    "Auto-trader (live rules)": 0.20, "Auto-trader, wider exits": 0.20, "Pairs: buy the laggard": 0.20,
    "Breakout: 20-day high (turtle)": 0.10, "Trend: 50-day above 200-day": 0.10, "Bollinger band dip-buy": 0.025,
    "52-week high breakout + 10% trailing stop": 0.025, "Swing: 3 down days in an uptrend": 0.025,
    "Swing: 7-day low / 7-day high": 0.10, "Swing: buy pullbacks in leaders": 0.20,
    "Swing: Bollinger squeeze breakout": 0.10, "Swing: RSI(2) dips in strong stocks": 0.025,
}
UNCAPPED = 10 ** 6


def _kw(strat, default=0.10):
    if isinstance(strat, MomentumLongShort):
        return {"max_positions": 20}
    if hasattr(strat, "rebalance_every"):
        return {}  # rotation strategies hold their own top-N
    return {"max_positions": UNCAPPED, "position_pct": LIVE_SIZES.get(strat.name, default)}


_ALL = [
    AutoTrader("Auto-trader (live rules)"), AutoTrader("Auto-trader, wider exits", 3.0, 1.5),
    EmaCross(), MomentumRotation(), Rsi2Dip(), PairsLaggard(),
    DonchianBreakout(), GoldenCross(), BollingerReversion(), MacdTrend(), DualMomentum(), LowVolatility(),
    NewHighs(), WeeklyReversal(),
    ThreeDownDays(), DoubleSevens(), MomentumPullback(), Nr7Breakout(), SqueezeBreakout(), Rsi2Leaders(),
    DoubleSevensShort(), DoubleSevensBoth(), MomentumLongShort(), BreakdownShort(), OverboughtShort(), PairsLongShort(),
]
STRATEGIES = [(st, _kw(st)) for st in _ALL] + [(BuyHoldEqual(), {"max_positions": "all"})]


def run(tickers=None):
    tickers = tickers or scanner.DEFAULT_UNIVERSE
    t0 = time.time()
    tickers, dates, D = load_data(tickers)
    indicators(D, tickers)
    D["SPY"]["sma200"] = sma(D["SPY"]["c"], 200)
    ctx = Ctx(D, tickers, dates, scores(D, tickers, len(dates)))
    test_dates = dates[WARMUP:]
    spy = D["SPY"]["c"][WARMUP:]
    spy_curve = [CAPITAL * v / spy[0] for v in spy]
    results = []
    for strat, kw in STRATEGIES:
        if hasattr(strat, "z"):
            strat.z = None
        if kw.get("max_positions") == "all":
            kw = {"max_positions": len(tickers), "position_pct": 1 / len(tickers)}
        curve, trades, invested = simulate(strat, ctx, **kw)
        results.append({"name": strat.name, "desc": strat.desc, "metrics": {**metrics(curve, trades, invested), **halves(curve)},
                        "yearly": yearly(curve, test_dates), "exits": exit_mix(trades),
                        "curve": [round(v, 2) for v in curve]})
    spy_m = {**metrics(spy_curve, [], [1.0] * len(spy_curve)), **halves(spy_curve)}
    results.append({"name": "S&P 500 (SPY)", "desc": "Benchmark: the whole market, buy and hold. No costs.",
                    "metrics": spy_m, "yearly": yearly(spy_curve, test_dates), "exits": {},
                    "curve": [round(v, 2) for v in spy_curve], "benchmark": True})
    results[-2]["benchmark"] = True  # equal-weight buy & hold
    for s in results:
        if not s.get("benchmark"):
            s["kind"] = "swing" if (s["metrics"]["avg_hold_days"] or 0) <= 25 else "long-term"
    spy_h = spy_m
    for s in results:
        m = s["metrics"]
        m["beats_spy_both_halves"] = (m["first_half_cagr"] > spy_h["first_half_cagr"] and
                                      m["second_half_cagr"] > spy_h["second_half_cagr"])
    out = {
        "half_split": test_dates[len(test_dates) // 2 - 1],
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "start": test_dates[0], "end": test_dates[-1], "days": len(test_dates),
        "tickers": tickers, "capital": CAPITAL, "cost_bps": round(COST * 10_000, 2),
        "dates": test_dates, "strategies": results, "runtime_sec": round(time.time() - t0, 1),
    }
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")))
    tmp.replace(OUT_FILE)
    return out


if __name__ == "__main__":
    r = run(sys.argv[1:])
    print(f"{r['start']} → {r['end']} ({r['days']} trading days), {len(r['tickers'])} stocks, "
          f"${r['capital']:,.0f} start, {r['cost_bps']:.0f} bps/side, {r['runtime_sec']}s\n")
    hdr = (f"{'Kind':<6}{'Strategy':<38}{'Final':>9}{'CAGR':>7}{'MaxDD':>8}{'Sharpe':>7}{'Trades':>7}{'Win%':>6}{'Avg':>7}{'Expo':>6}"
           f"{'H1':>7}{'H2':>7}  ")
    print(hdr)
    print("-" * len(hdr))
    for s in sorted(r["strategies"], key=lambda s: -s["metrics"]["sharpe"]):
        m = s["metrics"]
        win = f"{m['win_rate'] * 100:.0f}" if m["win_rate"] is not None else "–"
        avg = f"{m['avg_trade'] * 100:+.1f}%" if m["avg_trade"] is not None else "–"
        print(f"{s.get('kind', 'bench')[:5]:<6}{s['name'][:37]:<38}{m['final']:>9,.0f}{m['cagr'] * 100:>6.1f}%{m['max_drawdown'] * 100:>7.1f}%"
              f"{m['sharpe']:>7.2f}{m['trades']:>7}{win:>6}{avg:>7}{m['exposure'] * 100:>5.0f}%"
              f"{m['first_half_cagr'] * 100:>6.1f}%{m['second_half_cagr'] * 100:>6.1f}%  {'✓' if m['beats_spy_both_halves'] else ''}")
    print(f"\nH1/H2 = yearly return in each half of the test (split {r['half_split']}). ✓ = beat SPY in both halves.")
    print(f"\nWrote {OUT_FILE.name}")
