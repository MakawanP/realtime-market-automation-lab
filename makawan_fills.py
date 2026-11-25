#!/usr/bin/env python3
"""
makawan_fills.py - every real fill: the limit you set, what you actually paid, and how much you saved (READ-ONLY)
    python3 makawan_fills.py                      # the live bot's folder
    python3 makawan_fills.py --out makawan_out_live
"""
import argparse
import csv
import os
import re
import sys


def main():
    ap = argparse.ArgumentParser(description="Real fills versus their limits (reads the bot's own record)")
    ap.add_argument("--out", default="makawan_out_live")
    a = ap.parse_args()
    path = os.path.join(a.out, "events.csv")
    if not os.path.exists(path):
        sys.exit(f"No record found at {path}")
    n = saved = 0.0
    print(f"{'time':5} {'win':>3} {'side':4} {'order':7} {'limit':>6} {'paid':>7}  result")
    for e in csv.DictReader(open(path, newline="", encoding="utf-8")):
        if e.get("event") != "BUY" or "LIVE" not in (e.get("note") or ""):
            continue
        m = re.search(r"limit ([\d.]+)", e["note"])
        lim, px, sh = (float(m.group(1)) if m else None), float(e["price"]), float(e["shares"])
        kind = "resting" if "maker" in e["note"] else "instant"
        diff = (lim - px) if lim is not None else 0.0
        n += 1
        saved += diff * sh
        res = f"saved {diff * 100:.0f}c a share (${diff * sh:.2f})" if diff > 0.0005 else "at the limit"
        print(f"{e['time_et'][11:16]} {e['horizon']:>3} {e['side']:4} {kind:7} {lim * 100 if lim else 0:5.0f}c "
              f"{px * 100:6.1f}c  {res}")
    print(f"\n{int(n)} real fills; ${saved:.2f} saved in all compared with the limits.")
    print("A resting order always fills at its own limit. An instant order can fill cheaper when sellers below the "
          "limit are still there.")


if __name__ == "__main__":
    main()
