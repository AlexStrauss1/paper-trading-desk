#!/usr/bin/env python3
"""
One pass of the trading desk, for scheduled runs (GitHub Actions, cron, Task Scheduler...).

Does what server.py's background loop does once, then exits:
  1. marks every account to market and checks stops/targets
  2. gap-fade (day) account: scans for gap-ups after 9:31 ET, covers everything late in the day
  3. daily strategies: once per trading day, from 3:30 pm ET (later if the scheduler starts late;
     after the close they use the closing prices)
  4. refreshes the scanner once a day
  5. writes the static dashboard into docs/ (served by GitHub Pages)

Timing windows are wider than the live server's because scheduled runs can start late.
Run:  python3 run_once.py
"""
import os

# wider windows for schedulers that may start a few to 30 minutes late (set before importing server)
os.environ.setdefault("DAILY_RUN_AT", "15:30")
os.environ.setdefault("DAILY_RUN_UNTIL", "23:59")
os.environ.setdefault("GAP_SCAN_UNTIL", "10:30")
os.environ.setdefault("DAY_FLATTEN_AT", "15:30")

import json  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import export_static  # noqa: E402
import scanner  # noqa: E402
import server as S  # noqa: E402


def traded_today():
    """True if the market had a session today (so after-close runs on holidays don't trade stale prices)."""
    meta = scanner.get_json("https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=1d&interval=1d")["chart"]["result"][0]["meta"]
    return datetime.fromtimestamp(meta["regularMarketTime"], S.ET).date().isoformat() == S.today()


def main():
    t0 = time.time()
    # restore each account's last status (notes/decisions) from its state file
    for k in S.ACCOUNTS:
        with S.use_account(k):
            S.STATUS[k].update(S.load_state().get("status", {}))
    S.daily_info.update(json.loads(S.DAILY_INFO_FILE.read_text()) if S.DAILY_INFO_FILE.exists() else {})
    S.daily_info["running"] = False

    is_open = S.market_is_open()
    session = is_open or traded_today()
    tickers = set()
    for k in S.ACCOUNTS:
        with S.use_account(k):
            tickers |= set(S.load_state()["positions"])
    q = S.get_prices(tickers) if tickers else {}
    for k in S.ACCOUNTS:
        with S.use_account(k):
            if k == "gapfade":
                S.gapfade_tick(is_open)
            elif is_open:
                S.check_stops(q)
            else:
                S.mark_to_market(q)
            S.status().update(last_run=S.now_iso(), market_open=is_open)
            if k != "gapfade" and S.load_state().get("last_daily_run") != S.today():
                S.status()["note"] = f"next decision after {S.CONFIG['daily_run_at']} ET on trading days"

    if session and S.daily_due():
        S.daily_job()
    S.DAILY_INFO_FILE.write_text(json.dumps(S.daily_info))

    scan_day = None
    if scanner.OUT_FILE.exists():
        scan_day = json.loads(scanner.OUT_FILE.read_text())["generated"][:10]
    if session and scan_day != datetime.now(timezone.utc).date().isoformat():
        scanner.run()

    for k in S.ACCOUNTS:  # persist status for the next run / the dashboard
        with S.use_account(k):
            with S.lock:
                s = S.load_state()
                s["status"] = {x: S.STATUS[k].get(x) for x in ("note", "decisions", "last_run", "market_open", "last_daily")}
                S.save_state(s)
    export_static.export()
    print(f"[{S.now_iso()}] run_once done in {time.time() - t0:.0f}s (market {'open' if is_open else 'closed'})", flush=True)


if __name__ == "__main__":
    main()
