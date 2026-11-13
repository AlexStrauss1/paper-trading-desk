#!/usr/bin/env python3
"""
Multi-strategy paper trading desk.

Runs every strategy that passed the Strategy lab, each in its own simulated account
($10k each), side by side:

  long-term : momentum rotation, 50/200 trend, 52-week-high breakout
  swing     : 7-day low/high, 3 down days in an uptrend
  day       : short gap-ups in downtrends (flat by the close)

Daily strategies decide once a day at ~3:50 pm ET using that day's live prices (the same
rule code as backtest.py) and fill at the live price. The day strategy trades from the open.
Stops/targets are checked every minute and on every TradingView price push.

TradingView webhooks still work: order alerts (buy/sell/short/cover) go to the account named
in the alert ("account"), default "momentum"; price pushes from price_feed.pine feed every account.

Simulator only (Alpaca paper is used only for webhook orders if ALPACA_KEY/SECRET are set).
Standard library only. Run:  python3 server.py
"""
import bisect
import contextvars
import hmac
import json
import math
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import backtest
import daytrade
import scanner

HERE = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("STATE_DIR", HERE))
ET = ZoneInfo("America/New_York")
CONFIG = {
    "secret": os.environ.get("WEBHOOK_SECRET", "change-me"),
    "port": int(os.environ.get("PORT", "8080")),
    "starting_cash": float(os.environ.get("STARTING_CASH", "10000")),
    "fractional": os.environ.get("FRACTIONAL_SHARES", "1") != "0",
    "max_daily_loss_pct": float(os.environ.get("MAX_DAILY_LOSS_PCT", "3")),  # only for accounts with halt_on_loss
    "allowed_tickers": [t.strip().upper() for t in os.environ.get("ALLOWED_TICKERS", "").split(",") if t.strip()],
    "alpaca_key": os.environ.get("ALPACA_KEY"),
    "alpaca_secret": os.environ.get("ALPACA_SECRET"),
    "auto": os.environ.get("AUTO_TRADE", "1") != "0",
    "interval_sec": int(os.environ.get("AUTO_INTERVAL_SEC", "60")),
    "daily_run_at": os.environ.get("DAILY_RUN_AT", "15:50"),     # ET, daily strategies decide from here...
    "daily_run_until": os.environ.get("DAILY_RUN_UNTIL", "16:00"),  # ...until here (GitHub Actions: later, runs can start late)
    "gap_scan_until": os.environ.get("GAP_SCAN_UNTIL", "09:45"),    # last time to open gap-fade shorts
    "day_flatten_at": os.environ.get("DAY_FLATTEN_AT", "15:55"),    # cover all day trades from here
}
ALPACA_PAPER_URL = "https://paper-api.alpaca.markets/v2/orders"  # paper only, on purpose

# ---------------------------------------------------------------- accounts
# Each account = one strategy that passed the Strategy lab, its own state file and $10k.
# No cap on the number of positions: each new position is `size` of the account's equity (from
# backtest.LIVE_SIZES), longs are limited only by cash, and total shorts by 100% of equity (no leverage).
B = backtest


def _daily(label, kind, strat):
    key = label
    return {"label": label, "kind": kind, "strategy": strat,
            "size": 0.10 if hasattr(strat, "rebalance_every") else B.LIVE_SIZES[strat.name]}


ACCOUNTS = {
    "momentum": _daily("Momentum rotation", "long-term", B.MomentumRotation()),
    "momfilter": _daily("Momentum + market filter", "long-term", B.DualMomentum()),
    "trend": _daily("Trend 50/200", "long-term", B.GoldenCross()),
    "highs": _daily("52-week highs", "long-term", B.NewHighs()),
    "autowide": _daily("Auto-trader, wide exits", "swing", B.AutoTrader("Auto-trader, wider exits", 3.0, 1.5)),
    "autotrader": _daily("Auto-trader", "swing", B.AutoTrader("Auto-trader (live rules)")),
    "pullback": _daily("Pullbacks in leaders", "swing", B.MomentumPullback()),
    "double7": _daily("7-day low/high", "swing", B.DoubleSevens()),
    "rsi2lead": _daily("RSI(2) dips in leaders", "swing", B.Rsi2Leaders()),
    "threedown": _daily("3 down days", "swing", B.ThreeDownDays()),
    "turtle": _daily("20-day breakout", "swing", B.DonchianBreakout()),
    "squeeze": _daily("Bollinger squeeze", "swing", B.SqueezeBreakout()),
    "pairs": _daily("Pairs: buy the laggard", "swing", B.PairsLaggard()),
    "bbdip": _daily("Bollinger dip-buy", "swing", B.BollingerReversion()),
    "gapfade": {"label": "Gap-up fade (short)", "kind": "day", "size": 0.20, "halt_on_loss": True, "strategy": None,
                "experimental": True,
                "desc": "EXPERIMENTAL, did not pass the bar on the 250-stock test (+4.9% in 60 days but lost in the 2nd half). "
                        "Day trade, short side: at the open, shorts stocks below their 200-day average that gapped up 2%+, "
                        "betting the gap fills. Stop 3% above the open; every position is covered by 3:55 pm ET."},
}
_OLD_FILES = {"momentum": "state.json"}
for _k, _a in ACCOUNTS.items():
    _a["file"] = STATE_DIR / _OLD_FILES.get(_k, f"state_{_k}.json")
    _a.setdefault("desc", _a["strategy"].desc if _a["strategy"] else "")
    _a["name"] = _a["strategy"].name if _a["strategy"] else "Short: gap-up fade in downtrends"
DEFAULT_ACCOUNT = "momentum"
DAILY_ACCOUNTS = [k for k, a in ACCOUNTS.items() if a["strategy"] is not None]

ACCT = contextvars.ContextVar("account", default=DEFAULT_ACCOUNT)


class use_account:
    def __init__(self, key):
        self.key = key

    def __enter__(self):
        self.tok = ACCT.set(self.key)

    def __exit__(self, *exc):
        ACCT.reset(self.tok)


def A():
    return ACCOUNTS[ACCT.get()]


STATUS = {k: {"last_run": None, "market_open": None, "note": "starting", "decisions": []} for k in ACCOUNTS}


def status():
    return STATUS[ACCT.get()]


lock = threading.RLock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_et():
    return datetime.now(ET)


def today():
    return now_et().date().isoformat()


def load_state():
    f = A()["file"]
    if f.exists():
        s = json.loads(f.read_text())
        if "auto_last_rebalance" in s and "last_rebalance_date" not in s:  # migrate from the single-account version
            s["last_rebalance_date"] = s["auto_last_rebalance"][:10]
        return s
    return {"cash": CONFIG["starting_cash"], "positions": {}, "last_prices": {}, "trades": [], "events": [],
            "day": {"date": today(), "start_equity": CONFIG["starting_cash"], "trades": 0}, "halted": False}


def save_state(state):
    f = A()["file"]
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(f)


def sign(p):
    return -1 if p.get("side") == "short" else 1


def equity(state):
    held = sum(sign(p) * p["qty"] * state["last_prices"].get(t, p["avg_price"]) for t, p in state["positions"].items())
    return state["cash"] + held


def roll_day(state):
    if state["day"]["date"] != today():
        state["day"] = {"date": today(), "start_equity": equity(state), "trades": 0}
        state["halted"] = False


def alpaca_order(ticker, qty, side):
    body = json.dumps({"symbol": ticker, "qty": str(qty), "side": side, "type": "market", "time_in_force": "day"}).encode()
    req = urllib.request.Request(ALPACA_PAPER_URL, data=body, method="POST", headers={
        "APCA-API-KEY-ID": CONFIG["alpaca_key"], "APCA-API-SECRET-KEY": CONFIG["alpaca_secret"],
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10, context=scanner.SSL_CTX) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- orders
OPENING = {"buy": "long", "short": "short"}
CLOSING = {"sell": "long", "cover": "short"}


def place_order(ticker, action, price, source="webhook", reason="", extra=None, use_alpaca=True, amount=None):
    """Fill an order in the current account. buy/short open (or top up), sell/cover close. Returns (ok, result)."""
    with lock:
        state = load_state()
        roll_day(state)
        state["last_prices"][ticker] = price
        event = {"time": now_iso(), "ticker": ticker, "action": action, "price": price, "source": source, "result": None}

        def reject(why):
            event["result"] = "rejected: " + why
            state["events"] = (state["events"] + [event])[-500:]
            save_state(state)
            return False, {"ok": False, "reason": why}

        eq = equity(state)
        day_pnl_pct = (eq / state["day"]["start_equity"] - 1) * 100
        if A().get("halt_on_loss") and day_pnl_pct <= -CONFIG["max_daily_loss_pct"]:
            state["halted"] = True
        pos = state["positions"].get(ticker)

        if action in OPENING:
            side = OPENING[action]
            if state["halted"]:
                return reject(f"halted: daily loss {day_pnl_pct:.2f}% hit limit")
            if CONFIG["allowed_tickers"] and ticker not in CONFIG["allowed_tickers"]:
                return reject("ticker not in ALLOWED_TICKERS")
            if pos and (amount is None or pos.get("side", "long") != side):
                return reject(f"already {pos.get('side', 'long')}")
            dollars = amount if amount is not None else eq * A()["size"]
            if side == "long":
                if state["cash"] < dollars * 0.5:
                    return reject("not enough cash")
                dollars = min(dollars, state["cash"])
            else:
                shorts = sum(p["qty"] * state["last_prices"].get(t, p["avg_price"])
                             for t, p in state["positions"].items() if p.get("side") == "short")
                if shorts + dollars > eq:
                    return reject("short exposure would exceed 100% of equity")
            qty = math.floor(dollars / price * 10_000) / 10_000 if CONFIG["fractional"] else int(dollars // price)
            if qty <= 0 or qty * price < 1:
                return reject("not enough cash")
        elif action in CLOSING:
            if not pos or pos.get("side", "long") != CLOSING[action]:
                return reject(f"no {CLOSING[action]} position to {action}")
            qty = pos["qty"]
        else:
            return reject(f"unknown action {action}")

        broker = "sim"
        if use_alpaca and CONFIG["alpaca_key"] and CONFIG["alpaca_secret"]:
            try:
                alpaca_order(ticker, qty, "buy" if action in ("buy", "cover") else "sell")
                broker = "alpaca-paper"
            except Exception as e:  # noqa: BLE001
                return reject(f"alpaca error: {e}")

        trade = {"time": now_iso(), "ticker": ticker, "side": action, "qty": qty, "price": price,
                 "broker": broker, "source": source, "reason": reason}
        if action in OPENING:
            state["cash"] += qty * price if action == "short" else -qty * price
            if pos:  # top-up
                new_qty = round(pos["qty"] + qty, 4)
                pos["avg_price"] = round((pos["avg_price"] * pos["qty"] + price * qty) / new_qty, 4)
                pos["qty"] = new_qty
            else:
                state["positions"][ticker] = {"qty": qty, "avg_price": price, "opened": now_iso(), "side": OPENING[action],
                                              "source": source, "reason": reason, **(extra or {})}
        else:
            state["cash"] += qty * price if action == "sell" else -qty * price
            pnl = (price - pos["avg_price"]) * qty * sign(pos)
            trade["pnl"] = round(pnl, 2)
            trade["pnl_pct"] = round(pnl / (pos["avg_price"] * qty) * 100, 2)
            del state["positions"][ticker]
        state["trades"].append(trade)
        state["day"]["trades"] += 1
        event["result"] = f"filled {action} {qty} @ {price}" + (f" ({reason})" if reason else "")
        state["events"] = (state["events"] + [event])[-500:]
        save_state(state)
        print(f"[{trade['time']}] {ACCT.get():<9} {action.upper():<5} {qty} {ticker} @ {price:.2f} {reason}", flush=True)
        return True, {"ok": True, "trade": trade}


def close_position(t, pos, price, reason):
    return place_order(t, "cover" if pos.get("side") == "short" else "sell", price, "auto", reason, use_alpaca=False)


def handle_alert(alert):
    """TradingView order alert: {"secret","ticker","action": buy|sell|short|cover,"price","account"?}."""
    if not hmac.compare_digest(str(alert.get("secret", "")), CONFIG["secret"]):
        return 401, {"ok": False, "reason": "bad secret"}
    ticker = str(alert.get("ticker", "")).upper().split(":")[-1]
    action = str(alert.get("action", "")).lower()
    account = str(alert.get("account", DEFAULT_ACCOUNT))
    try:
        price = float(alert["price"])
    except (KeyError, TypeError, ValueError):
        return 400, {"ok": False, "reason": "missing/invalid price"}
    if account not in ACCOUNTS:
        return 400, {"ok": False, "reason": f"unknown account; use one of {list(ACCOUNTS)}"}
    if not ticker or action not in (*OPENING, *CLOSING) or price <= 0:
        return 400, {"ok": False, "reason": "need ticker, action buy|sell|short|cover, price > 0"}
    with use_account(account):
        _, result = place_order(ticker, action, price, "webhook", "TradingView alert")
    return 200, result


# ---------------------------------------------------------------- prices
PRICE_SOURCE = os.environ.get("PRICE_SOURCE", "auto")  # auto | tradingview | yahoo
TV_STALE_SEC = float(os.environ.get("TV_STALE_SEC", "180"))
tv_feed = {}
tv_meta = {"last_push": None, "symbols": 0, "bar_seconds": None}


def get_prices(tickers):
    out, need, now = {}, [], time.time()
    for t in tickers:
        f = tv_feed.get(t)
        if PRICE_SOURCE != "yahoo" and f and now - f["at"] <= TV_STALE_SEC:
            out[t] = {"price": f["price"], "open": True, "source": "tradingview"}
        else:
            need.append(t)
    if need and PRICE_SOURCE != "tradingview":
        for t, v in scanner.quotes(need).items():
            out[t] = {**v, "source": "yahoo"}
    return out


def mark_to_market(q):
    with lock:
        s = load_state()
        for t in s["positions"]:
            if t in q:
                s["last_prices"][t] = q[t]["price"]
                s.setdefault("price_sources", {})[t] = q[t]["source"]
        save_state(s)
        return s


def check_stops(q):
    """Stops and targets stored on positions (long: stop below / target above; short: the reverse)."""
    s = mark_to_market(q)
    for t, p in list(s["positions"].items()):
        if p.get("source") != "auto" or t not in q:
            continue
        px, short = q[t]["price"], p.get("side") == "short"
        sl, tp = p.get("sl"), p.get("tp")
        if p.get("trail") and not p.get("trailed") and not short and px >= p["avg_price"] * (1 + p["trail"]):
            with lock:
                s2 = load_state()
                if t in s2["positions"]:
                    s2["positions"][t].update(sl=max(sl or 0, s2["positions"][t]["avg_price"]), trailed=True)
                    save_state(s2)
            continue
        if sl and (px >= sl if short else px <= sl):
            close_position(t, p, px, f"stop {sl:.2f} [{q[t]['source']} price]")
        elif tp and (px <= tp if short else px >= tp):
            close_position(t, p, px, f"target {tp:.2f} [{q[t]['source']} price]")


def handle_prices(alert):
    """Price push from price_feed.pine: {"secret","type":"prices","prices":{"NASDAQ:AAPL": 358.1, ...}}."""
    if not hmac.compare_digest(str(alert.get("secret", "")), CONFIG["secret"]):
        return 401, {"ok": False, "reason": "bad secret"}
    prices = alert.get("prices")
    if not isinstance(prices, dict) or not prices:
        return 400, {"ok": False, "reason": "prices must be a non-empty object"}
    now, got = time.time(), {}
    for sym, px in prices.items():
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        if px > 0:
            t = str(sym).upper().split(":")[-1]
            tv_feed[t] = {"price": px, "at": now, "symbol": sym}
            got[t] = {"price": px, "open": True, "source": "tradingview"}
    tv_meta.update(last_push=now_iso(), symbols=len(got), bar_seconds=alert.get("bar_seconds"))
    for k in ACCOUNTS:  # react immediately
        with use_account(k):
            check_stops(got)
    return 200, {"ok": True, "prices": len(got)}


def market_is_open():
    t = now_et()
    if t.weekday() >= 5:
        return False
    q = get_prices(["SPY"])
    return q.get("SPY", {}).get("open", False)


# ---------------------------------------------------------------- daily strategies
daily_info = {"last_run": None, "running": False, "error": None, "seconds": None}
DAILY_INFO_FILE = STATE_DIR / "daily_info.json"


def build_daily_context(live):
    """5 years of daily bars (cached) + today's live price as today's bar, with indicators and scores."""
    tickers, dates, D = backtest.load_data(scanner.DEFAULT_UNIVERSE)
    d = today()
    if dates[-1] != d:
        dates = dates + [d]
        for t in [*tickers, "SPY"]:
            last = D[t]["c"][-1]
            px = live.get(t, {}).get("price", last)
            for k in ("o", "h", "l", "c"):
                D[t][k].append(px)
    else:
        for t in [*tickers, "SPY"]:
            px = live.get(t, {}).get("price")
            if px:
                D[t]["c"][-1] = px
                D[t]["h"][-1] = max(D[t]["h"][-1], px)
                D[t]["l"][-1] = min(D[t]["l"][-1], px)
    backtest.indicators(D, tickers)
    D["SPY"]["sma200"] = backtest.sma(D["SPY"]["c"], 200)
    return backtest.Ctx(D, tickers, dates, backtest.scores(D, tickers, len(dates)))


def run_daily_account(ctx, live):
    acc, strat = A(), A()["strategy"]
    dates, i, d = ctx.dates, len(ctx.dates) - 1, today()
    with lock:
        st = load_state()
    rot = hasattr(strat, "rebalance_every")
    if rot:
        last = st.get("last_rebalance_date")
        ctx.force_rebalance = last is None or (len(dates) - bisect.bisect_right(dates, last)) >= strat.rebalance_every
    else:
        ctx.force_rebalance = None
    pos = {}
    for t, p in st["positions"].items():
        if p.get("source") != "auto":
            continue
        ed = p.get("entry_date", d)
        pos[t] = {"i": min(bisect.bisect_left(dates, ed), i), "meta": tuple(p["meta"]) if p.get("meta") else None,
                  "side": sign(p)}
    ctx.last_exit = {}
    for tr in st["trades"]:
        if tr["side"] in ("sell", "cover") and tr.get("source") == "auto":
            ctx.last_exit[tr["ticker"]] = min(bisect.bisect_left(dates, tr["time"][:10]), i)
    if hasattr(strat, "z"):
        strat.z = None
    exits, entries = strat.decide(i, ctx, pos)
    for t, p in st["positions"].items():
        if t in pos and p.get("max_hold") and i - pos[t]["i"] >= p["max_hold"]:
            exits.setdefault(t, f"held {p['max_hold']} trading days")
    log = []
    for t, why in exits.items():
        p = st["positions"].get(t)
        if p and t in live:
            ok, _ = close_position(t, p, live[t]["price"], why)
            log.append({"ticker": t, "info": f"{live[t]['price']:.2f}", "decision": f"{'CLOSED' if ok else 'close failed'}: {why}"})
    with lock:
        held = set(load_state()["positions"])
    skipped = []
    for t, why, lv in entries:
        if t in held or t not in live:
            continue
        px, side = live[t]["price"], lv.get("side", 1)
        extra = {"entry_date": d, "max_hold": lv.get("max_hold"), "meta": list(lv["meta"]) if lv.get("meta") else None,
                 "trail": lv.get("trail"),
                 "tp": round(px * (1 + side * lv["tp"]), 2) if lv.get("tp") else None,
                 "sl": round(px * (1 - side * lv["sl"]), 2) if lv.get("sl") else None}
        ok, res = place_order(t, "buy" if side > 0 else "short", px, "auto", why, extra=extra, use_alpaca=False)
        if ok:
            held.add(t)
            log.append({"ticker": t, "info": f"{px:.2f}", "decision": f"{'BOUGHT' if side > 0 else 'SHORTED'}: {why}"})
        elif res["reason"] in ("not enough cash", "short exposure would exceed 100% of equity"):
            skipped.append(t)
    if skipped:
        log.append({"ticker": f"+{len(skipped)}", "info": "", "decision": "more signals, out of cash: " + ", ".join(skipped[:8]) + ("…" if len(skipped) > 8 else "")})
    if rot:  # rotation strategies stay fully invested: top up underweight holdings if >5% sits in cash
        with lock:
            s = load_state()
        eq = equity(s)
        slot = eq * acc["size"]
        if s["cash"] > 0.05 * eq:
            for gap, t in sorted(((slot - p["qty"] * live[t]["price"], t) for t, p in s["positions"].items() if t in live), reverse=True):
                if gap < 0.01 * eq:
                    break
                if place_order(t, "buy", live[t]["price"], "auto", "top-up to full slot", use_alpaca=False, amount=gap)[0]:
                    log.append({"ticker": t, "info": f"+${gap:,.0f}", "decision": "topped up"})
    with lock:
        s = load_state()
        s["last_daily_run"] = d
        if rot and ctx.force_rebalance:
            s["last_rebalance_date"] = d
        save_state(s)
    extra_note = ""
    if rot:
        last = s.get("last_rebalance_date")
        left = strat.rebalance_every - (len(dates) - bisect.bisect_right(dates, last)) if last else 0
        extra_note = f" · next rebalance in {max(left, 0)} trading days"
    status().update(decisions=log, last_daily=now_iso(),
                    note=(f"{len(log)} action(s) at today's run" if log else "no signals today") + extra_note)


def daily_job(force=False):
    if daily_info["running"]:
        return
    daily_info["running"] = True
    t0 = time.time()
    try:
        live = get_prices([*scanner.DEFAULT_UNIVERSE, "SPY"])
        ctx = build_daily_context(live)
        for k in DAILY_ACCOUNTS:
            with use_account(k):
                if not force and load_state().get("last_daily_run") == today():
                    continue  # already decided today
                try:
                    run_daily_account(ctx, live)
                except Exception as e:  # noqa: BLE001
                    status()["note"] = f"error: {e}"
                    print(f"[{now_iso()}] {k} daily run failed: {e}", flush=True)
        daily_info.update(last_run=now_iso(), error=None, seconds=round(time.time() - t0, 1))
        print(f"[{now_iso()}] daily strategies ran in {time.time() - t0:.0f}s", flush=True)
    except Exception as e:  # noqa: BLE001
        daily_info["error"] = str(e)
        print(f"[{now_iso()}] daily job failed: {e}", flush=True)
    finally:
        daily_info["running"] = False


def hhmm(x):
    h, m = x.split(":")
    return int(h), int(m)


def daily_due():
    t = now_et()
    if t.weekday() >= 5 or not (hhmm(CONFIG["daily_run_at"]) <= (t.hour, t.minute) < hhmm(CONFIG["daily_run_until"])):
        return False
    for k in DAILY_ACCOUNTS:
        with use_account(k):
            if load_state().get("last_daily_run") != today():
                return True
    return False


# ---------------------------------------------------------------- day strategy: gap-up fade (short)
def gapfade_tick(is_open):
    t, d = now_et(), today()
    hm = (t.hour, t.minute)
    with lock:
        st = load_state()
    held = {k: p for k, p in st["positions"].items() if p.get("source") == "auto"}
    q = get_prices(list(held)) if held else {}
    if held:
        check_stops(q)
    # never hold overnight: cover anything left from a previous day, and everything at 3:55 pm
    for k, p in held.items():
        if k in q and (p.get("entry_date") != d or hm >= hhmm(CONFIG["day_flatten_at"])):
            with lock:
                if k in load_state()["positions"]:
                    close_position(k, p, q[k]["price"], "end of day" if p.get("entry_date") == d else "stale overnight position")
    if not is_open:
        status()["note"] = "market closed"
        return
    if hm < (9, 31) or hm >= hhmm(CONFIG["gap_scan_until"]) or st.get("gap_scan_date") == d:
        if not status().get("decisions") and st.get("gap_scan_date") != d:
            status()["note"] = "waiting for 9:31 am ET to scan for gap-ups"
        return
    # 9:31-9:45: scan downtrending stocks for 2%+ gap-ups and short them
    tickers, dates, D = backtest.load_data(scanner.DEFAULT_UNIVERSE)
    j = len(dates) - 1 if dates[-1] < d else len(dates) - 2  # last completed session
    downs = []
    for k in tickers:
        c = D[k]["c"]
        if j >= 200 and c[j] < sum(c[j - 199:j + 1]) / 200:
            downs.append(k)
    info = scanner.many(scanner.intraday, downs)
    gaps = []
    for k, v in info.items():
        if v.get("day_open") and v.get("prev_close"):
            g = v["day_open"] / v["prev_close"] - 1
            if g >= 0.02:
                gaps.append((g, k, v))
    gaps.sort(reverse=True)
    log = []
    for g, k, v in gaps:
        stop = round(v["day_open"] * 1.03, 2)
        if v["price"] >= stop:
            log.append({"ticker": k, "info": f"gap {g * 100:+.1f}%", "decision": "skipped: already above the stop"})
            continue
        ok, res = place_order(k, "short", v["price"], "auto", f"gap-up {g * 100:+.1f}% below 200-day avg",
                              extra={"entry_date": d, "sl": stop}, use_alpaca=False)
        log.append({"ticker": k, "info": f"gap {g * 100:+.1f}%", "decision": f"SHORTED @ {v['price']:.2f}, stop {stop}" if ok else res["reason"]})
    with lock:
        s = load_state()
        s["gap_scan_date"] = d
        save_state(s)
    status().update(decisions=log, note=f"scanned {len(info)} downtrending stocks: {len(gaps)} gap-ups ≥ 2%")


# ---------------------------------------------------------------- main loop
def auto_loop():
    time.sleep(5)
    while True:
        try:
            is_open = market_is_open()
            # 1) mark every account to market + stops (one price request for all of them)
            tickers = set()
            for k in ACCOUNTS:
                with use_account(k):
                    tickers |= set(load_state()["positions"])
            q = get_prices(tickers) if tickers else {}
            for k in ACCOUNTS:
                with use_account(k):
                    if k == "gapfade":
                        gapfade_tick(is_open)
                    elif is_open:
                        check_stops(q)
                    else:
                        mark_to_market(q)
                    status().update(last_run=now_iso(), market_open=is_open)
                    if k != "gapfade" and not status().get("last_daily"):
                        status()["note"] = f"next decision at {CONFIG['daily_run_at']} ET on trading days"
            # 2) once a day near the close: run the daily strategies
            if is_open and daily_due():
                daily_job()
        except Exception as e:  # noqa: BLE001
            print(f"[{now_iso()}] auto loop error: {e}", flush=True)
        time.sleep(max(CONFIG["interval_sec"], 15))


# ---------------------------------------------------------------- scanner + lab (unchanged)
scan_status = {"running": False, "error": None}
bt_status = {"running": False, "error": None}


def run_scan():
    if scan_status["running"]:
        return
    scan_status["running"] = True
    try:
        scanner.run()
        scan_status["error"] = None
    except Exception as e:  # noqa: BLE001
        scan_status["error"] = str(e)
    finally:
        scan_status["running"] = False


def scan_loop():
    minutes = float(os.environ.get("SCAN_INTERVAL_MIN", "30"))
    while True:
        run_scan()
        time.sleep(max(minutes, 1) * 60)


def run_backtest():
    if bt_status["running"]:
        return
    bt_status["running"] = True
    try:
        backtest.run()
        daytrade.run()
        bt_status["error"] = None
    except Exception as e:  # noqa: BLE001
        bt_status["error"] = str(e)
    finally:
        bt_status["running"] = False


# ---------------------------------------------------------------- API
def backtest_stats():
    out = {}
    for f in (backtest.OUT_FILE, daytrade.OUT_FILE):
        if f.exists():
            for s in json.loads(f.read_text())["strategies"]:
                out[s["name"]] = s["metrics"]
    return out


def account_summary(k, bt=None):
    with use_account(k):
        with lock:
            s = load_state()
            roll_day(s)
        eq = equity(s)
        a = A()
        m = (bt or {}).get(a["name"], {})
        return {"key": k, "label": a["label"], "kind": a["kind"], "name": a["name"], "size": a["size"], "experimental": a.get("experimental", False),
                "equity": round(eq, 2), "cash": round(s["cash"], 2),
                "total_return_pct": round((eq / CONFIG["starting_cash"] - 1) * 100, 2),
                "day_pnl_pct": round((eq / s["day"]["start_equity"] - 1) * 100, 2),
                "positions": len(s["positions"]), "trades": len(s["trades"]),
                "backtest": {k2: m.get(k2) for k2 in ("cagr", "total_return", "sharpe", "max_drawdown")} if m else None}


def public_state(k):
    with use_account(k):
        with lock:
            state = load_state()
            roll_day(state)
        eq = equity(state)
        a = A()
        return {**state, "account": k,
                "equity": round(eq, 2),
                "total_return_pct": round((eq / CONFIG["starting_cash"] - 1) * 100, 2),
                "realized_pnl": round(sum(t.get("pnl", 0) for t in state["trades"]), 2),
                "day_pnl_pct": round((eq / state["day"]["start_equity"] - 1) * 100, 2),
                "broker": "alpaca-paper" if CONFIG["alpaca_key"] else "sim",
                "secret_is_default": CONFIG["secret"] == "change-me",
                "auto": {"enabled": CONFIG["auto"], "label": a["label"], "kind": a["kind"], "name": a["name"],
                         "desc": a["desc"], "size": a["size"], "experimental": a.get("experimental", False), "daily_run_at": CONFIG["daily_run_at"], **status()},
                "daily": daily_info,
                "feed": {"price_source": PRICE_SOURCE, "stale_after_sec": TV_STALE_SEC, **tv_meta,
                         "age_sec": round(time.time() - max((f["at"] for f in tv_feed.values()), default=0)) if tv_feed else None}}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status_code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status_code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            self._send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            k = qs.get("account", [DEFAULT_ACCOUNT])[0]
            self._send(200, public_state(k if k in ACCOUNTS else DEFAULT_ACCOUNT))
        elif u.path == "/api/accounts":
            bt = backtest_stats()
            self._send(200, {"accounts": [account_summary(k, bt) for k in ACCOUNTS], "daily": daily_info})
        elif u.path == "/api/scan":
            data = json.loads(scanner.OUT_FILE.read_text()) if scanner.OUT_FILE.exists() else None
            self._send(200, {"status": scan_status, "scan": data})
        elif u.path == "/api/lab":
            swing = backtest.OUT_FILE.read_bytes() if backtest.OUT_FILE.exists() else b"null"
            day = daytrade.OUT_FILE.read_bytes() if daytrade.OUT_FILE.exists() else b"null"
            self._send(200, b'{"status":' + json.dumps(bt_status).encode() + b',"swing":' + swing + b',"day":' + day + b"}")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/lab":
            threading.Thread(target=run_backtest, daemon=True).start()
            return self._send(202, {"started": True})
        if path == "/api/scan":
            threading.Thread(target=run_scan, daemon=True).start()
            return self._send(202, {"started": True})
        if path == "/api/run-daily":  # run the daily strategies now instead of waiting for DAILY_RUN_AT
            if not market_is_open():
                return self._send(409, {"started": False, "reason": "market is closed"})
            threading.Thread(target=daily_job, daemon=True).start()
            return self._send(202, {"started": True})
        if path != "/webhook":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        if length > 10_000:
            return self._send(413, {"error": "too large"})
        try:
            alert = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"ok": False, "reason": "body must be JSON"})
        code, result = handle_prices(alert) if alert.get("type") == "prices" else handle_alert(alert)
        self._send(code, result)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if CONFIG["secret"] == "change-me":
        print("WARNING: set WEBHOOK_SECRET to something private before exposing this server.")
    print(f"Dashboard: http://localhost:{CONFIG['port']}/   Webhook: POST /webhook")
    print("Accounts: " + ", ".join(f"{k} ({a['label']})" for k, a in ACCOUNTS.items()))
    if os.environ.get("SCAN_INTERVAL_MIN", "30") != "0":
        threading.Thread(target=scan_loop, daemon=True).start()
    if CONFIG["auto"]:
        threading.Thread(target=auto_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", CONFIG["port"]), Handler).serve_forever()
