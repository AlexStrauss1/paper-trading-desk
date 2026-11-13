#!/usr/bin/env python3
"""
Writes the dashboard as a static site into docs/ (for GitHub Pages):
  docs/index.html          the same dashboard, in read-only mode
  docs/data/*.json         accounts, each account's state, scanner, strategy lab
Run:  python3 export_static.py
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import backtest
import daytrade
import scanner
import server as S

HERE = Path(__file__).resolve().parent


def _write(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    tmp.replace(path)


def export(out=HERE / "docs"):
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)
    bt = S.backtest_stats()
    _write(data / "accounts.json", {"accounts": [S.account_summary(k, bt) for k in S.ACCOUNTS], "daily": S.daily_info,
                                    "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    for k in S.ACCOUNTS:
        st = S.public_state(k)
        st["events"] = st["events"][-100:]
        _write(data / f"state_{k}.json", st)
    scan = json.loads(scanner.OUT_FILE.read_text()) if scanner.OUT_FILE.exists() else None
    _write(data / "scan.json", {"status": {"running": False, "error": None}, "scan": scan})
    lab = {"status": {"running": False, "error": None},
           "swing": json.loads(backtest.OUT_FILE.read_text()) if backtest.OUT_FILE.exists() else None,
           "day": json.loads(daytrade.OUT_FILE.read_text()) if daytrade.OUT_FILE.exists() else None}
    _write(data / "lab.json", lab)
    html = (HERE / "dashboard.html").read_text()
    html = html.replace("<head>", "<head>\n<script>window.STATIC_MODE = true;</script>", 1)
    (out / "index.html").write_text(html)
    (out / ".nojekyll").write_text("")  # serve files as-is
    return out


if __name__ == "__main__":
    print("wrote", export())
