#!/usr/bin/env python3
"""
Market scanner: sorts out how companies differ from each other.

1. Ranking   - momentum, risk and trend metrics per company, combined into one
               composite score (cross-sectional z-scores), sorted best -> worst.
2. Clusters  - groups companies whose daily returns move together
               (agglomerative clustering, average linkage, distance = 1 - correlation).
3. Pairs     - for closely related companies, fits log(A) = a + b*log(B), tests the
               spread for mean reversion (Engle-Granger / Dickey-Fuller) and flags pairs
               whose spread is currently stretched (|z| >= 2): the classic pairs trade.

Data: one year of daily adjusted closes from Yahoo Finance's public chart endpoint.
Standard library only.

Usage:  python3 scanner.py                 # default universe
        python3 scanner.py KO PEP V MA     # your own list
Writes scan.json next to this file (read by server.py / the dashboard).
"""
import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from statistics import mean, pstdev

HERE = Path(__file__).resolve().parent
OUT_FILE = HERE / "scan.json"

# Built so that natural peers sit in the same list (sector pairs make the pairs scan useful).
# ~120 large, liquid US stocks. TradingView's price feed covers 40 per script (price_feed.pine);
# the rest use Yahoo prices unless you add another copy of the script with a different list.
CORE_UNIVERSE = [
    # tech / communication
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "MU", "AMAT", "LRCX",
    "KLAC", "ADI", "ORCL", "CRM", "ADBE", "NOW", "INTU", "IBM", "CSCO", "ACN", "TSLA", "NFLX", "UBER", "SHOP",
    "PLTR", "PANW", "CRWD", "SNOW", "DIS", "CMCSA", "T", "VZ", "TMUS",
    # financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "AXP", "V", "MA", "PYPL", "COF", "USB", "PNC",
    # energy / materials / utilities / real estate
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "LIN", "FCX", "NEM", "NEE", "DUK", "SO", "AMT", "PLD",
    # consumer
    "KO", "PEP", "PG", "CL", "WMT", "COST", "TGT", "KR", "MDLZ", "PM", "MO", "HD", "LOW", "MCD", "YUM",
    "SBUX", "NKE", "BKNG", "ABNB", "CMG", "TJX",
    # health care
    "UNH", "CVS", "JNJ", "PFE", "MRK", "LLY", "ABBV", "TMO", "ABT", "AMGN", "GILD", "BMY", "ISRG", "MDT",
    # industrials
    "UPS", "FDX", "CAT", "DE", "BA", "LMT", "RTX", "GE", "HON", "UNP", "CSX", "MMM",
]
_UNIVERSE_FILE = Path(__file__).resolve().parent / "universe.txt"
# ~600 stocks: S&P 500 + the most-traded S&P 400 mid-caps (edit universe.txt to change)
DEFAULT_UNIVERSE = ([t.strip().upper() for t in _UNIVERSE_FILE.read_text().split() if t.strip()]
                    if _UNIVERSE_FILE.exists() else CORE_UNIVERSE)
PAIRS_UNIVERSE_SIZE = 150  # correlation clusters/pairs are O(n^2): limit them to the core list

PAIR_MIN_CORR = 0.6        # only test pairs that actually move together
DIVERGED_Z = 2.0           # spread stretched this many std devs = "diverged"
COINT_CRIT = -3.37         # ~5% Engle-Granger critical value (2 series, constant)
CLUSTER_MAX_DIST = 0.5     # stop merging clusters once avg correlation drops below 0.5

TV_EXCHANGE = {"NYQ": "NYSE", "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NAS": "NASDAQ",
               "ASE": "AMEX", "PCX": "AMEX", "BTS": "CBOE"}


# ---------------------------------------------------------------- data
def ssl_context():
    """python.org builds on macOS ship without CA certs; fall back to the system bundle."""
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca") and os.path.exists("/etc/ssl/cert.pem"):
        ctx = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ctx


SSL_CTX = ssl_context()


def get_json(url, timeout=15, tries=3):
    """GET a Yahoo chart URL, retrying with backoff when rate-limited (HTTP 429) or on a network blip."""
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code != 429 or k == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError):
            if k == tries - 1:
                raise
        time.sleep(1.5 * (k + 1))


def fetch(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1y&interval=1d"
    res = get_json(url)["chart"]["result"][0]
    meta = res["meta"]
    closes = res["indicators"].get("adjclose", [{}])[0].get("adjclose") or res["indicators"]["quote"][0]["close"]
    series = {}
    for ts, c in zip(res["timestamp"], closes):
        if c:
            series[datetime.fromtimestamp(ts, timezone.utc).date().isoformat()] = c
    exch = TV_EXCHANGE.get(meta.get("exchangeName"))
    return {
        "ticker": ticker,
        "name": meta.get("longName") or meta.get("shortName") or ticker,
        "tv_symbol": f"{exch}:{ticker}" if exch else ticker,
        "series": series,
    }


def quote(ticker):
    """Latest trade price and whether the regular session is open right now."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1d&interval=1m"
    meta = get_json(url, timeout=10)["chart"]["result"][0]["meta"]
    reg = meta.get("currentTradingPeriod", {}).get("regular", {})
    now = datetime.now(timezone.utc).timestamp()
    return {"price": meta["regularMarketPrice"], "open": reg.get("start", 0) <= now < reg.get("end", 0)}


def intraday(ticker):
    """Today's 1-minute bars (regular session): price now, session start/end, and (ts, high, low) bars."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1d&interval=1m"
    res = get_json(url, timeout=10)["chart"]["result"][0]
    meta, q = res["meta"], res["indicators"]["quote"][0]
    reg = meta.get("currentTradingPeriod", {}).get("regular", {})
    bars = [(ts, h, l) for ts, h, l in zip(res.get("timestamp", []), q.get("high", []), q.get("low", []))
            if h is not None and l is not None]
    now = datetime.now(timezone.utc).timestamp()
    day_open = next((o for ts, o in zip(res.get("timestamp", []), q.get("open", []))
                     if o is not None and ts >= reg.get("start", 0)), None)
    return {"price": meta["regularMarketPrice"], "open": reg.get("start", 0) <= now < reg.get("end", 0),
            "session_start": reg.get("start"), "day_open": day_open,
            "prev_close": meta.get("chartPreviousClose") or meta.get("previousClose"), "bars": bars}


def many(fn, tickers):
    out = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, fut in [(t, pool.submit(fn, t)) for t in tickers]:
            try:
                out[t] = fut.result()
            except Exception:  # noqa: BLE001
                pass
    return out


def quotes(tickers):
    out = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, fut in [(t, pool.submit(quote, t)) for t in tickers]:
            try:
                out[t] = fut.result()
            except Exception:  # noqa: BLE001
                pass
    return out


def load_universe(tickers):
    data, errors = {}, {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for t, fut in [(t, pool.submit(fetch, t)) for t in tickers]:
            try:
                d = fut.result()
                if len(d["series"]) < 130:
                    raise ValueError(f"only {len(d['series'])} days of history")
                data[t] = d
            except Exception as e:  # noqa: BLE001
                errors[t] = str(e)
    # align everyone on the dates they all share
    if data:
        longest = max(len(d["series"]) for d in data.values())
        for t in [t for t, d in data.items() if len(d["series"]) < longest * 0.95]:
            errors[t] = f"only {len(data[t]['series'])} days of history"
            del data[t]
    dates = sorted(set.intersection(*(set(d["series"]) for d in data.values()))) if data else []
    for d in data.values():
        d["prices"] = [d["series"][day] for day in dates]
        del d["series"]
    return data, dates, errors


# ---------------------------------------------------------------- math helpers
def log_returns(p):
    return [math.log(b / a) for a, b in zip(p, p[1:])]


def corr(x, y):
    mx, my = mean(x), mean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0


def ols(y, x):
    """y = a + b*x. Returns a, b, residuals, std-error of b."""
    n, mx, my = len(x), mean(x), mean(y)
    sxx = sum((v - mx) ** 2 for v in x)
    b = sum((u - mx) * (v - my) for u, v in zip(x, y)) / sxx
    a = my - b * mx
    resid = [v - (a + b * u) for u, v in zip(x, y)]
    s2 = sum(r * r for r in resid) / (n - 2)
    return a, b, resid, math.sqrt(s2 / sxx)


def rsi(p, n=14):
    gains = losses = 0.0
    for a, b in zip(p[-n - 1:], p[-n:]):
        gains += max(b - a, 0)
        losses += max(a - b, 0)
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


def max_drawdown(p):
    peak, dd = p[0], 0.0
    for v in p:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return dd


def zscores(values):
    m, s = mean(values), pstdev(values)
    return [(v - m) / s if s else 0.0 for v in values]


# ---------------------------------------------------------------- 1. ranking
def rank(data):
    rows = []
    for t, d in data.items():
        p = d["prices"]
        r = log_returns(p)
        vol = pstdev(r) * math.sqrt(252)
        ret = lambda days: p[-1] / p[-1 - days] - 1 if len(p) > days else None  # noqa: E731
        rows.append({
            "ticker": t, "name": d["name"], "tv_symbol": d["tv_symbol"],
            "price": round(p[-1], 2),
            "ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126), "ret_1y": p[-1] / p[0] - 1,
            "mom12_1": p[-22] / p[0] - 1,  # 12-month return skipping the latest month (momentum rotation)
            "volatility": vol,
            "sharpe": (mean(r) * 252) / vol if vol else 0.0,
            "max_drawdown": max_drawdown(p),
            "rsi": rsi(p),
            "from_high": p[-1] / max(p) - 1,
            "above_sma50": p[-1] > mean(p[-50:]),
            "above_sma200": p[-1] > mean(p[-200:]) if len(p) >= 200 else None,
        })
    # composite: momentum + risk-adjusted return, penalise deep drawdowns
    z6, z3 = zscores([x["ret_6m"] for x in rows]), zscores([x["ret_3m"] for x in rows])
    zsh, zdd = zscores([x["sharpe"] for x in rows]), zscores([x["max_drawdown"] for x in rows])
    for i, x in enumerate(rows):
        x["score"] = 0.35 * z6[i] + 0.25 * z3[i] + 0.25 * zsh[i] + 0.15 * zdd[i]
    rows.sort(key=lambda x: x["score"], reverse=True)
    for i, x in enumerate(rows, 1):
        x["rank"] = i
        for k, v in list(x.items()):
            if isinstance(v, float):
                x[k] = round(v, 4)
    return rows


# ---------------------------------------------------------------- 2. clustering
def correlation_matrix(data):
    tickers = list(data)
    rets = {t: log_returns(data[t]["prices"]) for t in tickers}
    c = {}
    for a, b in combinations(tickers, 2):
        c[(a, b)] = c[(b, a)] = corr(rets[a], rets[b])
    return c


def cluster(tickers, c):
    groups = [[t] for t in tickers]
    dist = lambda g, h: mean(1 - c[(a, b)] for a in g for b in h)  # noqa: E731
    while len(groups) > 1:
        d, i, j = min((dist(groups[i], groups[j]), i, j) for i, j in combinations(range(len(groups)), 2))
        if d > CLUSTER_MAX_DIST:
            break
        groups[i] += groups.pop(j)
    out = []
    for g in sorted(groups, key=len, reverse=True):
        inner = [c[(a, b)] for a, b in combinations(g, 2)]
        out.append({"members": sorted(g), "avg_corr": round(mean(inner), 3) if inner else None})
    return out


# ---------------------------------------------------------------- 3. pairs
def pair_stats(a, b, data, c):
    la = [math.log(v) for v in data[a]["prices"]]
    lb = [math.log(v) for v in data[b]["prices"]]
    _, beta, spread, _ = ols(la, lb)
    # Dickey-Fuller on the spread: d(s_t) = k + g*s_{t-1}; mean-reverting if g clearly < 0
    ds = [y - x for x, y in zip(spread, spread[1:])]
    _, g, _, se = ols(ds, spread[:-1])
    t_stat = g / se
    half_life = -math.log(2) / g if g < 0 else None
    sd = pstdev(spread)
    z = spread[-1] / sd if sd else 0.0
    z_5d = spread[-6] / sd if sd else 0.0
    ra, rb = data[a]["prices"], data[b]["prices"]
    gap_3m = (ra[-1] / ra[-64] - 1) - (rb[-1] / rb[-64] - 1)
    # If the two prices trended in opposite directions over the year, the fit gives beta <= 0
    # even though daily moves are correlated. That spread isn't a hedge, so don't trade it.
    tradable = beta > 0
    if not tradable:
        idea = "opposite long-term trends (hedge ratio <= 0): not a pairs trade"
    elif z >= DIVERGED_Z:
        idea = f"{a} rich vs {b}: short {a} / long {b} (long-only: favour {b})"
    elif z <= -DIVERGED_Z:
        idea = f"{a} cheap vs {b}: long {a} / short {b} (long-only: favour {a})"
    else:
        idea = ""
    return {
        "a": a, "b": b,
        "tv_a": data[a]["tv_symbol"], "tv_b": data[b]["tv_symbol"],
        "corr": round(c[(a, b)], 3), "hedge_ratio": round(beta, 3),
        "adf_t": round(t_stat, 2), "cointegrated": t_stat < COINT_CRIT,
        "half_life_days": round(half_life, 1) if half_life else None,
        "z": round(z, 2), "z_5d_ago": round(z_5d, 2),
        "perf_gap_3m": round(gap_3m, 4),
        "diverged": tradable and abs(z) >= DIVERGED_Z, "idea": idea,
    }


def find_pairs(data, c):
    pairs = [pair_stats(a, b, data, c) for a, b in combinations(sorted(data), 2) if c[(a, b)] >= PAIR_MIN_CORR]
    # best trades first: cointegrated + stretched, then just stretched, then by how stretched
    pairs.sort(key=lambda p: (not (p["cointegrated"] and p["diverged"]), not p["diverged"], -abs(p["z"])))
    return pairs


# ---------------------------------------------------------------- main
def run(tickers=None):
    if not tickers:
        tickers = os.environ.get("SCAN_TICKERS", "").split(",")
    tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip())) or DEFAULT_UNIVERSE
    data, dates, errors = load_universe(tickers)
    if len(data) < 2:
        raise RuntimeError(f"need at least 2 tickers with data; errors: {errors}")
    core = {t: data[t] for t in CORE_UNIVERSE if t in data}
    if len(core) < 10:  # custom small list: analyse all of it
        core = dict(list(data.items())[:PAIRS_UNIVERSE_SIZE])
    c = correlation_matrix(core)
    result = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": dates[-1], "days": len(dates),
        "universe": sorted(data), "errors": errors,
        "ranking": rank(data),
        "clusters": cluster(list(core), c),
        "pairs": find_pairs(core, c)[:60],
    }
    tmp = OUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(OUT_FILE)
    return result


def pct(v):
    return "   n/a" if v is None else f"{v * 100:+6.1f}%"


if __name__ == "__main__":
    r = run(sys.argv[1:])
    print(f"Scanned {len(r['universe'])} companies, {r['days']} trading days to {r['as_of']}")
    if r["errors"]:
        print("Skipped:", r["errors"])
    print("\nRANKING (composite: 6M/3M momentum, Sharpe, drawdown)")
    print(f"{'#':>3} {'Ticker':<6} {'Score':>6} {'3M':>7} {'6M':>7} {'1Y':>7} {'Vol':>6} {'MaxDD':>7} {'RSI':>5}")
    for x in r["ranking"]:
        print(f"{x['rank']:>3} {x['ticker']:<6} {x['score']:>6.2f} {pct(x['ret_3m'])} {pct(x['ret_6m'])} {pct(x['ret_1y'])}"
              f" {x['volatility'] * 100:5.1f}% {pct(x['max_drawdown'])} {x['rsi']:5.1f}")
    print("\nCLUSTERS (move together)")
    for g in r["clusters"]:
        if len(g["members"]) > 1:
            print(f"  corr {g['avg_corr']:.2f}: {', '.join(g['members'])}")
    print("\nDIVERGED PAIRS (|z| >= 2)")
    div = [p for p in r["pairs"] if p["diverged"]]
    for p in div:
        tag = "cointegrated" if p["cointegrated"] else "not cointegrated"
        print(f"  {p['a']}/{p['b']}  z={p['z']:+.2f}  corr={p['corr']:.2f}  half-life={p['half_life_days']}d  [{tag}]  {p['idea']}")
    if not div:
        print("  none right now")
    print(f"\nWrote {OUT_FILE.name}")
