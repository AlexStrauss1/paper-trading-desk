#!/usr/bin/env python3
"""
Day-trading lab: tests intraday strategies on 5-minute bars (last ~60 trading days,
the most Yahoo provides at this resolution).

Rules for every strategy:
- Long only, every position is closed by the final bar of the day (no overnight risk).
- At most MAX_OPEN positions at once, each sized at 1/MAX_OPEN of the day's starting equity.
  Signals are taken first-come-first-served by time of day.
- Entries/exits fill at the next bar's open (or at the stop price, if a stop is hit inside a bar).
- Costs: COST_BPS per side (default 3 bps = 0.03%); results are also shown with zero cost.

Usage:  python3 daytrade.py          # writes daytrade.json, prints a summary
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
from statistics import mean, pstdev

import backtest
import scanner

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "daytrade.json"
CACHE_FILE = HERE / "daytrade_data.json.gz"
CAPITAL = 10_000.0
COST = float(os.environ.get("DAY_COST_BPS", "3")) / 10_000
MAX_OPEN = 5
BARS = 78  # 9:30-16:00 in 5-minute bars


# ---------------------------------------------------------------- data
def fetch_5m(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=60d&interval=5m"
    res = scanner.get_json(url, timeout=30)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    days = {}
    for k, ts in enumerate(res["timestamp"]):
        day = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        days.setdefault(day, []).append((q["open"][k], q["high"][k], q["low"][k], q["close"][k], q["volume"][k] or 0))
    # keep complete, gap-free regular sessions only
    return {d: b for d, b in days.items() if len(b) == BARS and all(None not in x[:4] for x in b)}


def load(tickers):
    if CACHE_FILE.exists() and time.time() - CACHE_FILE.stat().st_mtime < 12 * 3600:
        raw = json.loads(gzip.decompress(CACHE_FILE.read_bytes()))
        if set(tickers) <= set(raw) and "SPY" in raw:
            return raw
    raw = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, fut in [(t, pool.submit(fetch_5m, t)) for t in [*tickers, "SPY"]]:
            try:
                raw[t] = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"skip {t}: {e}", file=sys.stderr)
    CACHE_FILE.write_bytes(gzip.compress(json.dumps(raw, separators=(",", ":")).encode(), 6))
    return raw


def daily_trend(tickers):
    """Prior-day 'uptrend' flags from the daily data: close above its 50- and 200-day averages."""
    tk, dates, D = backtest.load_data(tickers)
    out = {}
    for t in tk:
        c = D[t]["c"]
        s50, s200 = backtest.sma(c, 50), backtest.sma(c, 200)
        # flag for date d uses the PREVIOUS session's values (known before the open)
        out[t] = {dates[i + 1]: {"up50": c[i] > s50[i], "up200": c[i] > s200[i]}
                  for i in range(199, len(dates) - 1)}
    return out


# ---------------------------------------------------------------- per-day helpers
def vwap(bars):
    out, pv, vv = [], 0.0, 0.0
    for o, h, l, c, v in bars:
        pv += (h + l + c) / 3 * v
        vv += v
        out.append(pv / vv if vv else c)
    return out


def rsi2(closes):
    out = [None] * len(closes)
    for k in range(2, len(closes)):
        g = sum(max(closes[j] - closes[j - 1], 0) for j in (k - 1, k))
        l = sum(max(closes[j - 1] - closes[j], 0) for j in (k - 1, k))
        out[k] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return out


def run_to_close(bars, k_entry, entry, stop=None, side=1):
    """Hold from bar k_entry (already filled at `entry`) to the close, honouring a stop (below for longs, above for shorts)."""
    for k in range(k_entry + 1, BARS):
        o, h, l, c, v = bars[k]
        if stop and side > 0 and l <= stop:
            return k, min(o, stop), "stop"
        if stop and side < 0 and h >= stop:
            return k, max(o, stop), "stop"
    return BARS - 1, bars[-1][3], "close"


# ---------------------------------------------------------------- strategies
# Each returns a list of trades for one stock-day: (entry_bar, entry_px, exit_bar, exit_px, why)
def orb(bars, ctx, trend=False, minutes=30):
    n = minutes // 5
    if trend and not ctx["up50"]:
        return []
    hi = max(b[1] for b in bars[:n])
    lo = min(b[2] for b in bars[:n])
    for k in range(n, BARS - 6):  # no new entries in the last 30 minutes
        o, h = bars[k][0], bars[k][1]
        if h > hi:
            entry = max(o, hi)
            kx, px, why = run_to_close(bars, k, entry, stop=lo)
            return [(k, entry, kx, px, why)]
    return []


def orb_short(bars, ctx, trend=False, minutes=30):
    n = minutes // 5
    if trend and ctx["up50"]:
        return []
    hi = max(b[1] for b in bars[:n])
    lo = min(b[2] for b in bars[:n])
    for k in range(n, BARS - 6):
        o, l = bars[k][0], bars[k][2]
        if l < lo:
            entry = min(o, lo)
            kx, px, why = run_to_close(bars, k, entry, stop=hi, side=-1)
            return [(k, entry, kx, px, why, -1)]
    return []


def orb_both(bars, ctx, minutes=30):
    """Trade whichever side of the opening range breaks first: long in uptrends, short in downtrends."""
    return orb(bars, ctx, trend=True, minutes=minutes) or orb_short(bars, ctx, trend=True, minutes=minutes)


def gap_up_fade_short(bars, ctx):
    if not ctx["prev_close"] or ctx["up200"]:
        return []
    o = bars[0][0]
    if o / ctx["prev_close"] - 1 >= 0.02:
        kx, px, why = run_to_close(bars, 0, o, stop=o * 1.03, side=-1)
        return [(0, o, kx, px, why, -1)]
    return []


def vwap_reject_short(bars, ctx):
    vw, closes = vwap(bars), [b[3] for b in bars]
    for k in range(6, BARS - 7):
        if closes[k - 1] > vw[k - 1] and closes[k] < vw[k] and closes[k] < bars[0][0]:
            ke = k + 1
            entry = bars[ke][0]
            for j in range(ke, BARS - 1):
                if closes[j] > vw[j]:
                    return [(ke, entry, j + 1, bars[j + 1][0], "back above VWAP", -1)]
            return [(ke, entry, BARS - 1, closes[-1], "close", -1)]
    return []


def last_hour_short(bars, ctx):
    if not ctx["prev_close"]:
        return []
    k = 65
    c = bars[k][3]
    if c / ctx["prev_close"] - 1 < -0.01 and c < vwap(bars)[k]:
        return [(k + 1, bars[k + 1][0], BARS - 1, bars[-1][3], "close", -1)]
    return []


def vwap_reclaim(bars, ctx):
    vw, closes = vwap(bars), [b[3] for b in bars]
    for k in range(6, BARS - 7):
        if closes[k - 1] < vw[k - 1] and closes[k] > vw[k] and closes[k] > bars[0][0]:
            ke = k + 1
            entry = bars[ke][0]
            for j in range(ke, BARS - 1):
                if closes[j] < vw[j]:
                    return [(ke, entry, j + 1, bars[j + 1][0], "lost VWAP")]
            return [(ke, entry, BARS - 1, closes[-1], "close")]
    return []


def gap_fade(bars, ctx):
    if not ctx["prev_close"] or not ctx["up200"]:
        return []
    o = bars[0][0]
    if o / ctx["prev_close"] - 1 <= -0.02:
        kx, px, why = run_to_close(bars, 0, o, stop=o * 0.97)
        return [(0, o, kx, px, why)]
    return []


def gap_and_go(bars, ctx):
    if not ctx["prev_close"]:
        return []
    o, h, l, c, v = bars[0]
    if o / ctx["prev_close"] - 1 >= 0.02 and c > o:
        entry = bars[1][0]
        kx, px, why = run_to_close(bars, 1, entry, stop=l)
        return [(1, entry, kx, px, why)]
    return []


def rsi2_intraday(bars, ctx):
    if not ctx["prev_close"]:
        return []
    closes = [b[3] for b in bars]
    r = rsi2(closes)
    trades, k = [], 6
    while k < BARS - 7:
        if closes[k] > ctx["prev_close"] and r[k] is not None and r[k] < 10:
            ke = k + 1
            entry = bars[ke][0]
            kx = next((j for j in range(ke, BARS - 1) if r[j] is not None and r[j] > 70), None)
            if kx is None:
                trades.append((ke, entry, BARS - 1, closes[-1], "close"))
                break
            trades.append((ke, entry, kx + 1, bars[kx + 1][0], "RSI > 70"))
            k = kx + 2
        else:
            k += 1
    return trades


def last_hour_momentum(bars, ctx):
    if not ctx["prev_close"]:
        return []
    k = 65  # bar ending 15:00
    c = bars[k][3]
    if c / ctx["prev_close"] - 1 > 0.01 and c > vwap(bars)[k]:
        return [(k + 1, bars[k + 1][0], BARS - 1, bars[-1][3], "close")]
    return []


def spy_open_to_close(bars, ctx):
    return [(0, bars[0][0], BARS - 1, bars[-1][3], "close")] if ctx["ticker"] == "SPY" else []


STRATEGIES = [
    ("Opening range breakout (30 min)", "Buys when price breaks above the first 30 minutes' high; stop at the 30-minute low; out at the close.",
     lambda b, c: orb(b, c)),
    ("Opening range breakout + uptrend filter", "Same, but only for stocks that closed above their 50-day average the day before.",
     lambda b, c: orb(b, c, trend=True)),
    ("Opening range breakout (15 min)", "Faster version: breakout of the first 15 minutes' high, stop at the 15-minute low.",
     lambda b, c: orb(b, c, minutes=15)),
    ("VWAP reclaim", "After 10:00, buys when a stock that's up on the day crosses back above its VWAP; sells if it falls back below, or at the close.",
     vwap_reclaim),
    ("Gap-down fade (buy the gap)", "Buys at the open when a stock in a long-term uptrend opens 2%+ below yesterday's close, betting the gap fills. 3% stop, out at the close.",
     gap_fade),
    ("Gap-and-go", "When a stock opens 2%+ above yesterday's close and the first 5 minutes are green, buys and holds to the close. Stop at the first bar's low.",
     gap_and_go),
    ("Intraday RSI(2) dip-buy", "On green days, buys 5-minute dips (2-bar RSI < 10) and sells the bounce (RSI > 70) or at the close. Can trade several times a day.",
     rsi2_intraday),
    ("Last-hour momentum", "At 3:00 pm, buys stocks up 1%+ on the day and above VWAP; sells at the close.",
     last_hour_momentum),
    ("Short: opening range breakdown (30 min)", "Shorts when price breaks below the first 30 minutes' low; stop at the 30-minute high; covers at the close.",
     lambda b, c: orb_short(b, c)),
    ("Short: opening range breakdown + downtrend", "Same, only for stocks that closed below their 50-day average the day before.",
     lambda b, c: orb_short(b, c, trend=True)),
    ("Long/short opening range (trend-aligned)", "Long the 30-minute breakout in uptrending stocks, short the 30-minute breakdown in downtrending ones.",
     lambda b, c: orb_both(b, c)),
    ("Short: gap-up fade in downtrends", "Shorts at the open when a stock below its 200-day average gaps up 2%+, betting the gap fills. 3% stop, covers at the close.",
     gap_up_fade_short),
    ("Short: VWAP rejection", "After 10:00, shorts a stock that's down on the day when it falls back below VWAP; covers if it gets back above, or at the close.",
     vwap_reject_short),
    ("Short: last-hour weakness", "At 3:00 pm, shorts stocks down 1%+ on the day and below VWAP; covers at the close.",
     last_hour_short),
    ("SPY open-to-close (baseline)", "Baseline: buy the S&P 500 ETF at every open, sell at every close. What 'just being in the market during the day' earns.",
     spy_open_to_close),
]


# ---------------------------------------------------------------- engine
def simulate(fn, raw, tickers, days, trend, size=None):
    """size=None: MAX_OPEN equal slots. size=x: each trade is x of equity, no cap on count, but total open
    exposure (longs + shorts) never exceeds 100% of equity (no leverage)."""
    eq_net = eq_gross = CAPITAL
    curve_net, curve_gross, trades = [], [], []
    for di, day in enumerate(days):
        prev = days[di - 1] if di else None
        signals = []
        for t in [*tickers, "SPY"]:
            bars = raw[t].get(day)
            if not bars:
                continue
            pc = raw[t][prev][-1][3] if prev and prev in raw[t] else None
            tr = trend.get(t, {}).get(day, {"up50": False, "up200": False})
            ctx = {"ticker": t, "prev_close": pc, **tr}
            for tr_ in fn(bars, ctx):
                kb, entry, kx, px, why = tr_[:5]
                side = tr_[5] if len(tr_) > 5 else 1
                signals.append((kb, t, entry, kx, px, why, side))
        signals.sort()
        open_until, pnl_net, pnl_gross = [], 0.0, 0.0
        frac = size if size else 1 / MAX_OPEN
        cap = int(round(1 / frac)) if size else MAX_OPEN  # how many fit inside 100% exposure
        alloc_net, alloc_gross = eq_net * frac, eq_gross * frac
        for kb, t, entry, kx, exit_px, why, side in signals:
            open_until = [x for x in open_until if x > kb]
            if len(open_until) >= cap:
                continue
            open_until.append(kx)
            if side > 0:
                gross = exit_px / entry - 1
                net = exit_px * (1 - COST) / (entry * (1 + COST)) - 1
            else:
                gross = entry / exit_px - 1
                net = entry * (1 - COST) / (exit_px * (1 + COST)) - 1
            pnl_net += alloc_net * net
            pnl_gross += alloc_gross * gross
            trades.append({"day": day, "ticker": t, "ret": net, "gross": gross, "why": why, "bars": kx - kb})
        eq_net += pnl_net
        eq_gross += pnl_gross
        curve_net.append(eq_net)
        curve_gross.append(eq_gross)
    return curve_net, curve_gross, trades


def stats(curve, trades, ndays, gross_curve=None):
    rets = [b / a - 1 for a, b in zip([CAPITAL] + curve[:-1], curve)]
    sd = pstdev(rets)
    peak, mdd = CAPITAL, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    h = len(curve) // 2
    wins = [t for t in trades if t["ret"] > 0]
    gw, gl = sum(t["ret"] for t in wins), -sum(t["ret"] for t in trades if t["ret"] <= 0)
    exits = {}
    for t in trades:
        exits[t["why"]] = exits.get(t["why"], 0) + 1
    return {
        "final": round(curve[-1], 2),
        "total_return": round(curve[-1] / CAPITAL - 1, 4),
        "gross_return": round(gross_curve[-1] / CAPITAL - 1, 4) if gross_curve else None,
        "annualized": round((curve[-1] / CAPITAL) ** (252 / ndays) - 1, 4),
        "max_drawdown": round(mdd, 4),
        "sharpe": round(mean(rets) / sd * math.sqrt(252), 2) if sd else 0,
        "trades": len(trades),
        "trades_per_day": round(len(trades) / ndays, 1),
        "win_rate": round(len(wins) / len(trades), 3) if trades else None,
        "avg_trade_bps": round(mean(t["ret"] for t in trades) * 10_000, 1) if trades else None,
        "avg_gross_bps": round(mean(t["gross"] for t in trades) * 10_000, 1) if trades else None,
        "profit_factor": round(gw / gl, 2) if gl else None,
        "first_half": round(curve[h - 1] / CAPITAL - 1, 4),
        "second_half": round(curve[-1] / curve[h - 1] - 1, 4),
        "exits": dict(sorted(exits.items(), key=lambda kv: -kv[1])),
    }


def run(tickers=None):
    tickers = tickers or scanner.DEFAULT_UNIVERSE
    t0 = time.time()
    raw = load(tickers)
    tickers = [t for t in tickers if t in raw and raw[t]]
    days = sorted(set(raw["SPY"]))
    trend = daily_trend(tickers)
    results = []
    for name, desc, fn in STRATEGIES:
        net, gross, trades = simulate(fn, raw, tickers, days, trend)
        results.append({"name": name, "desc": desc, "metrics": stats(net, trades, len(days), gross),
                        "curve": [round(v, 2) for v in net]})
    # benchmark: SPY bought and held (including overnight moves), no costs
    spy = raw["SPY"]
    first_open = spy[days[0]][0][0]
    spy_curve = [CAPITAL * spy[d][-1][3] / first_open for d in days]
    results.append({"name": "S&P 500 held (incl. overnight)", "desc": "Benchmark: buy SPY on the first day and hold through the whole period. No costs.",
                    "metrics": stats(spy_curve, [], len(days)), "curve": [round(v, 2) for v in spy_curve], "benchmark": True})
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "start": days[0], "end": days[-1], "days": len(days), "dates": days, "tickers": tickers,
           "capital": CAPITAL, "cost_bps": round(COST * 10_000, 2), "max_open": MAX_OPEN,
           "strategies": results, "runtime_sec": round(time.time() - t0, 1)}
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")))
    tmp.replace(OUT_FILE)
    return out


if __name__ == "__main__":
    r = run(sys.argv[1:])
    print(f"{r['start']} → {r['end']} ({r['days']} days, 5-min bars), {len(r['tickers'])} stocks, "
          f"max {r['max_open']} open, {r['cost_bps']:.0f} bps/side, {r['runtime_sec']}s\n")
    hdr = f"{'Strategy':<42}{'Net':>8}{'Gross':>8}{'MaxDD':>8}{'Sharpe':>7}{'Trades':>7}{'/day':>5}{'Win%':>6}{'Avg bps':>8}{'H1':>7}{'H2':>7}"
    print(hdr)
    print("-" * len(hdr))
    for s in sorted(r["strategies"], key=lambda s: -s["metrics"]["sharpe"]):
        m = s["metrics"]
        g = f"{m['gross_return'] * 100:+.1f}%" if m["gross_return"] is not None else "–"
        w = f"{m['win_rate'] * 100:.0f}" if m["win_rate"] is not None else "–"
        a = f"{m['avg_trade_bps']:+.1f}" if m["avg_trade_bps"] is not None else "–"
        print(f"{s['name'][:41]:<42}{m['total_return'] * 100:>+7.1f}%{g:>8}{m['max_drawdown'] * 100:>7.1f}%{m['sharpe']:>7.2f}"
              f"{m['trades']:>7}{m['trades_per_day']:>5}{w:>6}{a:>8}{m['first_half'] * 100:>+6.1f}%{m['second_half'] * 100:>+6.1f}%")
    print("\nNet = after costs; Gross = before costs. H1/H2 = return in each half of the period. Avg bps = average trade after costs (1 bp = 0.01%).")
    print(f"Wrote {OUT_FILE.name}")
