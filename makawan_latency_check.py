#!/usr/bin/env python3
"""
makawan_latency_check.py - how slow is my connection, and how far do prices move while an order is in flight? (READ-ONLY)
======================================================================================================================
    python3 makawan_latency_check.py                 # about 90 seconds
    python3 makawan_latency_check.py --seconds 300   # watch prices for longer (more reliable)

It measures, using public market data only:
  1. Round trip: how long one request to Polymarket US takes from this computer.
  2. Clock: how far this computer's clock is from Polymarket's (the bot times each window from your clock).
  3. Price feed: how old the newest Bitcoin index print is when it reaches you.
  4. Price drift: how much the price you would pay moves 1, 2 and 5 seconds later. That is the slippage a late
     order would see.
If POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY are set, it also times a signed, read-only balance request, which
is the closest thing to an order's round trip that does not place an order.

It never places, changes or cancels an order. Order-matching time inside the exchange is not included, so real
order latency is somewhat higher than the round trips shown.
"""
from __future__ import annotations

import argparse
import email.utils
import os
import statistics
import sys
import time

import makawan_bot as tb

REQUIRES = "2026.11.13"
if getattr(tb, "__version__", None) != REQUIRES:
    sys.exit("{} and makawan_bot.py come from different downloads. Put ALL the files from the latest download in one "
             "folder.".format(os.path.basename(__file__)))


def pctl(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def summarize_ms(xs):
    return f"typical {statistics.median(xs):.0f} ms, slow (9 in 10 are faster) {pctl(xs, 0.9):.0f} ms, worst {max(xs):.0f} ms"


def drift_stats(series, lags=(1, 2, 5)):
    """series: [(t, up_bid, up_ask)]. For each lag: how the price to BUY moved `lag` seconds later, pooled over
    buying UP (pay the ask) and buying DOWN (pay 1 - bid). Positive = it got more expensive."""
    out = {}
    for lag in lags:
        moves = []
        for i, (t0, b0, a0) in enumerate(series):
            later = next(((b, a) for t, b, a in series[i + 1:] if t - t0 >= lag - 0.25), None)
            if later is None:
                continue
            b1, a1 = later
            if a0 is not None and a1 is not None:
                moves.append(a1 - a0)                      # buying UP
            if b0 is not None and b1 is not None:
                moves.append((1 - b1) - (1 - b0))          # buying DOWN
        if moves:
            worse = [m for m in moves if m > 1e-9]
            out[lag] = {"n": len(moves), "moved": sum(1 for m in moves if abs(m) > 1e-9) / len(moves),
                        "worse": len(worse) / len(moves),
                        "avg_when_worse": (sum(worse) / len(worse)) if worse else 0.0,
                        "avg_cost": sum(max(m, 0.0) for m in moves) / len(moves), "max": max(moves)}
    return out


def main():
    ap = argparse.ArgumentParser(description="Measure connection latency and short-term price drift (read-only)")
    ap.add_argument("--seconds", type=int, default=60, help="how long to watch prices (default 60)")
    ap.add_argument("--gateway", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args()
    cfg = tb.Config()
    if a.gateway:
        cfg.gateway = a.gateway
    tr = tb.HttpTransport(cfg.gateway)
    api = tb.Api(tr, cfg)

    print(f"Checking {cfg.gateway} (read-only)...\n")
    # --- 1 + 2: round trip and clock
    rt, offsets = [], []
    for _ in range(20):
        t0 = time.time()
        r = tr.s.get(tr.base + "/v1/asset-prices/history",
                     params={"assets": "crypto:btc", "from": int(t0) - 3, "to": 0, "bucket": "ASSET_PRICE_BUCKET_1S"},
                     timeout=8)
        t1 = time.time()
        rt.append((t1 - t0) * 1000)
        d = r.headers.get("Date")
        if d:
            try:
                offsets.append((t0 + t1) / 2 - email.utils.parsedate_to_datetime(d).timestamp())
            except (TypeError, ValueError):
                pass
        time.sleep(0.25)
    print(f"1. Round trip (20 requests): {summarize_ms(rt)}")
    if offsets:
        off = statistics.median(offsets) - 0.5             # the Date header is whole seconds, rounded down
        verdict = "fine" if abs(off) < 1.5 else "TOO FAR OFF: turn on 'Set time automatically' in System Settings"
        print(f"2. Your clock vs Polymarket's: about {off:+.1f} s ({verdict}; measured to the nearest second)")
    else:
        print("2. Your clock vs Polymarket's: the server sent no time, so this could not be measured")

    # --- 3: feed freshness
    ages = []
    for _ in range(10):
        now = time.time()
        c = [x for x in api.brti(int(now) - 10, 0) if x[2] > 0]
        if c:
            ages.append(max(0.0, time.time() - c[-1][0]))  # seconds since the newest print's own timestamp
        time.sleep(0.5)
    if ages:
        print(f"3. Bitcoin index print age when it reaches you: typical {statistics.median(ages):.1f} s, "
              f"worst {max(ages):.1f} s")

    # --- signed round trip (optional)
    kid, sec = os.environ.get("POLYMARKET_KEY_ID", "").strip(), os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    if kid and sec:
        try:
            from polymarket_us import PolymarketUS
            client = PolymarketUS(key_id=kid, secret_key=sec)
            srt = []
            for _ in range(10):
                t0 = time.time()
                client.account.balances()
                srt.append((time.time() - t0) * 1000)
                time.sleep(0.3)
            client.close()
            print(f"   Signed request (read-only balance, 10 tries): {summarize_ms(srt)}")
        except Exception as e:                              # never let the optional part stop the check
            print(f"   Signed request: skipped ({type(e).__name__}: {e})")
    else:
        print("   Signed request: skipped (no API key set in this Terminal window; that is fine)")

    # --- 4: price drift
    found = api.find_active(time.time())
    if not found:
        sys.exit("\nNo active BTC Up/Down windows were found, so price drift could not be measured.")
    print(f"\n4. Watching prices for {a.seconds} s on: {', '.join(sorted(found))} ...")
    series = {h: [] for h in found}
    book_rt, empty, polls = [], {h: 0 for h in found}, 0
    end = time.time() + a.seconds
    while time.time() < end:
        tick = time.time()
        polls += 1
        for h, m in list(found.items()):
            if tick >= m.we:
                continue
            t0 = time.time()
            try:
                b = api.book(m.slug)
            except tb.ApiError:
                continue
            book_rt.append((time.time() - t0) * 1000)
            if b is None or (b.bid is None and b.ask is None):
                empty[h] += 1
            series[h].append((time.time(), b.bid if b else None, b.ask if b else None))
        time.sleep(max(0.0, 1.0 - (time.time() - tick)))
    if book_rt:
        print(f"   Order book request: {summarize_ms(book_rt)}")
    for h in sorted(series):
        s = series[h]
        if not s:
            continue
        quoted = [x for x in s if x[1] is not None or x[2] is not None]
        print(f"\n   {h} window: {len(s)} looks, order book empty in {empty[h]} of them")
        st = drift_stats(quoted)
        if not st:
            print("      not enough quotes to measure drift")
            continue
        for lag, v in st.items():
            print(f"      {lag} s later: price changed {v['moved']:.0%} of the time, got worse {v['worse']:.0%} "
                  f"(by {v['avg_when_worse'] * 100:.1f}c when it did); average cost of being {lag} s late "
                  f"{v['avg_cost'] * 100:.2f}c a share, worst {v['max'] * 100:.0f}c")
    print("\nHow to read this: your order is 'late' by about one round trip plus the bot's 1-second polling. "
          "Compare the average cost of that delay with your edge per share (a few cents). "
          "Nothing was bought, sold or changed.")


if __name__ == "__main__":
    main()
