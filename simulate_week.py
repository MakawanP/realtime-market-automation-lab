#!/usr/bin/env python3
"""
simulate_week.py - run the bot through N days of a SIMULATED market and report paper profit
===========================================================================================
    python simulate_week.py                      # 7 days, the default strategy
    python simulate_week.py --efficient          # control: a market that is priced perfectly
    python simulate_week.py --days 3 --seed 2 --params makawan_out/params.json --out sim_runs/a

WHAT THIS IS NOT: a test of whether the strategy makes money on Polymarket. The market here is made up. Its prices
are the true chance plus a little noise (--efficient removes the noise), so profit in this simulation only shows how
the strategy behaves under those assumptions. Only real prices can answer the real question.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

import makawan_bot as tb

REQUIRES = "2026.11.13"
if getattr(tb, "__version__", None) != REQUIRES:
    sys.exit("{} and makawan_bot.py come from different downloads. Put ALL the files from the latest download in one "
             "folder, replacing the old ones (makawan_bot.py reports version {}, this file needs {}).".format(
                 os.path.basename(__file__), getattr(tb, "__version__", "older than 2026.11.08.4"), REQUIRES))


def run(days, seed, efficient, preset, params, out_dir, bankroll, say=print, trade_last=None):
    tb.archive_outputs(out_dir)                      # a simulation always starts from scratch
    cfg = tb.Config(**tb.PRESETS[preset])
    cfg.out_dir, cfg.bankroll, cfg.log_ticks = out_dir, bankroll, False
    if params:
        tb.load_params(cfg, params)
    if trade_last is not None:                       # e.g. "none" = trade at any time in every window
        cfg.trade_last_s = tb.parse_trade_last(trade_last)
    cfg.exec_mode = "taker"                          # maker mode is never simulated: the fake market has no trades
    clock = tb.SimClock((1_800_000_000 // 3600 + 1) * 3600 - 20)
    tr = tb.SimTransport(clock, seed=seed, book_noise=0.0 if efficient else 0.02,
                         vol_view=(1.0, 1.0) if efficient else (0.9, 1.2))
    eng = tb.Engine(cfg, tb.Api(tr, cfg), clock, tb.Recorder(out_dir), parallel=False)
    eng.bootstrap()
    total = int(days * 86400)
    curve, t0 = [(0, eng.equity())], time.time()
    for k in range(1, total + 1):
        eng.tick()
        clock.advance(1.0)
        if k % 3600 == 0:
            curve.append((k, eng.equity()))
        if k % 86400 == 0:
            say(f"  day {k // 86400}/{days:g}  equity ${eng.equity():,.2f}  ({(time.time() - t0) / 60:.1f} min)")
    curve.append((total, eng.equity()))
    return eng, curve


def slug_start(slug):
    from datetime import datetime, timezone
    d, hm = slug.split("-updown-")[1].split("-", 1)[1].rsplit("-", 1)
    return datetime.strptime(d + hm.rstrip("z"), "%Y-%m-%d%H%M").replace(tzinfo=timezone.utc).timestamp()


def by_entry_timing(events, rows, limits):
    """Split trades into 'late' (inside the restricted strategy's trade window) and 'early' (only an unrestricted bot takes them)."""
    pnl = {r["slug"]: float(r["pnl"]) for r in rows if r["traded_side"] and r["pnl"] != ""}
    out = {}
    for e in events:
        if e["event"] != "BUY" or e["slug"] not in pnl or e["horizon"] not in limits:
            continue
        left = slug_start(e["slug"]) + tb.HORIZON_SECONDS[e["horizon"]] - tb.parse_ts(e["time_utc"])
        key = (e["horizon"], "late" if left <= limits[e["horizon"]] + 1.0 else "early")
        out.setdefault(key, []).append((pnl[e["slug"]], float(e["price"])))
    res = {}
    for (h, when), v in sorted(out.items()):
        wins = sum(1 for x, _ in v if x > 0)
        res.setdefault(h, {})[when] = {"trades": len(v), "win_rate": round(wins / len(v), 3),
                                       "pnl": round(sum(x for x, _ in v), 2), "pnl_per_trade": round(sum(x for x, _ in v) / len(v), 2),
                                       "avg_entry": round(sum(p for _, p in v) / len(v), 3)}
    return res


def summarize(eng, curve, out_dir):
    rows = list(csv.DictReader(open(os.path.join(out_dir, "windows.csv"))))
    events = list(csv.DictReader(open(os.path.join(out_dir, "events.csv"))))
    fees = sum(float(r["fee"]) for r in events if r["event"] == "BUY" and r["fee"])
    start = eng.cfg.bankroll
    peak, dd = start, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    out = {"start": start, "end": round(eng.equity(), 2), "pnl": round(eng.equity() - start, 2),
           "return_pct": round(100 * (eng.equity() - start) / start, 2), "max_drawdown": round(dd, 2),
           "fees_paid": round(fees, 2), "windows": len(rows), "by_horizon": {},
           "by_entry_timing": by_entry_timing(events, rows, {"5m": 120.0, "15m": 300.0})}
    for h in eng.cfg.horizons:
        tr = [r for r in rows if r["horizon"] == h and r["traded_side"]]
        pnl = [float(r["pnl"]) for r in tr if r["pnl"] != ""]
        wins = [x for x in pnl if x > 0]
        st = eng.stats[h]
        out["by_horizon"][h] = {
            "windows": st.windows, "trades": len(pnl), "win_rate": round(len(wins) / len(pnl), 3) if pnl else None,
            "staked": round(st.staked, 2), "pnl": round(sum(pnl), 2),
            "pnl_per_trade": round(sum(pnl) / len(pnl), 2) if pnl else None,
            "avg_entry": round(sum(float(r["avg_px"]) for r in tr if r["avg_px"] != "") / len(tr), 3) if tr else None,
            "first_call_hit": round(st.correct / st.called, 3) if st.called else None,
            "fees": round(st.fees, 2)}
    return out


def main():
    ap = argparse.ArgumentParser(description="Run the bot through a simulated market and report paper profit")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--efficient", action="store_true", help="control run: the fake market is priced perfectly")
    ap.add_argument("--preset", choices=sorted(tb.PRESETS), default="pro")
    ap.add_argument("--trade-last", default=None,
                    help="trade windows, e.g. 5m=120,15m=300 (default) or 'none' to trade at any time in every window")
    ap.add_argument("--params", default=None)
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--out", default="sim_week")
    a = ap.parse_args()
    print(f"Simulating {a.days:g} days, seed {a.seed}, {'PERFECTLY PRICED' if a.efficient else 'noisy'} market...")
    eng, curve = run(a.days, a.seed, a.efficient, a.preset, a.params, a.out, a.bankroll, trade_last=a.trade_last)
    res = summarize(eng, curve, a.out)
    res.update(seed=a.seed, efficient=a.efficient, days=a.days, trade_last=a.trade_last or "default",
               exec_mode="taker")
    with open(os.path.join(a.out, "summary.json"), "w") as f:
        json.dump(res, f, indent=2)
    with open(os.path.join(a.out, "equity.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["hours", "equity"])
        w.writerows((k // 3600, round(eq, 2)) for k, eq in curve)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
