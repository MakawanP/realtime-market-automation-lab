#!/usr/bin/env python3
"""Replay the strategy-lab rules over the live session's recorded readings (read-only).
    python3 lab_replay.py makawan_out_live [stake]
Uses ticks.csv (one reading about every 5 s per window) and windows.csv (how each window settled).
An order is placed at one reading and fills at the NEXT reading (~5 s later) only if the ask is still at or under
its limit (harsher than the lab's 1 s). Sells (cut) fill at the bid of the reading after the decision."""
import csv
import math
import os
import sys

import makawan_bot as tb

FEE = 0.095
fee = lambda p: FEE * p * (1 - p)
folder = sys.argv[1] if len(sys.argv) > 1 else "makawan_out_live"
STAKE = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0

STRATS = {
    "baseline": {},
    "cut": {"cut": True},
    "cautious": {"w": 0.65, "cap": 0.85, "cut": True},
    "trend": {"look": 600, "beta": 0.30},
    "fade": {"look": 60, "beta": -0.30},
    "maker": {"maker": True},
    "sniper": {"last": 60, "minpx": 0.90, "cap": 0.97},
}
cfg = tb.Config()

outcome = {}
for r in csv.DictReader(open(os.path.join(folder, "windows.csv"))):
    if r["outcome"] in ("UP", "DOWN") and r.get("provisional") not in ("1", "True"):
        outcome[r["slug"]] = r["outcome"]

ticks, brti = {}, {}
for r in csv.DictReader(open(os.path.join(folder, "ticks.csv"))):
    t = tb.parse_ts(r["time_utc"])
    if r["brti"]:
        brti[int(t)] = float(r["brti"])
    if r["horizon"] in ("5m", "15m") and r["slug"] in outcome:
        ticks.setdefault(r["slug"], []).append((t, r))
secs = sorted(brti)


def price_at(ts):
    import bisect
    i = bisect.bisect_right(secs, ts) - 1
    return brti[secs[i]] if i >= 0 and ts - secs[i] < 30 else None


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def chances(r, t, s):
    """(model UP chance, final UP chance) for strategy settings s at reading r."""
    S, K, sig, left = num(r["brti"]), num(r["price_to_beat"]), num(r["sigma_min"]), num(r["secs_left"])
    pm, mk = num(r["p_model"]), num(r["p_market"])
    if None in (S, K, sig, left, pm):
        return None, None
    if s.get("beta"):
        past = price_at(t - s["look"])
        if past is not None:
            adj = s["beta"] * (S - past) * min(1.0, left / s["look"])
            adj = max(-0.005 * S, min(0.005 * S, adj))
            pm = tb.chance_up(S + adj, K, left, sig / math.sqrt(60), None, 1.0, cfg.tail_w, cfg.tail_s2)
    if mk is None:
        return pm, None
    w = s.get("w", 0.5)
    return pm, (1 - w) * pm + w * mk


def side_quote(r, side):
    b, a = num(r["bid"]), num(r["ask"])
    if side == "UP":
        return b, a
    return (None if a is None else 1 - a), (None if b is None else 1 - b)


def run(name, s):
    res = []
    for slug, rows in ticks.items():
        rows.sort(key=lambda x: x[0])
        h = rows[0][1]["horizon"]
        win = tb.HORIZON_SECONDS[h]
        warm = max(45, 0.10 * win)
        pos, order, call, sent = None, None, None, False
        for i, (t, r) in enumerate(rows):
            left = num(r["secs_left"])
            pm, pf = chances(r, t, s)
            if pf is None:
                continue
            if call is None and win - left >= warm:            # first call, with the usual 57/43 thresholds
                call = "UP" if pf >= 0.57 else "DOWN" if pf <= 0.43 else None
            elif call == "UP" and pf < 0.53:
                call = "DOWN" if pf <= 0.43 else None
            elif call == "DOWN" and pf > 0.47:
                call = "UP" if pf >= 0.57 else None
            elif call is None and win - left >= warm:
                call = "UP" if pf >= 0.57 else "DOWN" if pf <= 0.43 else None
            if order is not None:                              # an order placed at the last reading arrives now
                bid, ask = side_quote(r, order["side"])
                if s.get("maker"):
                    if call != order["side"] or left < 20:
                        order = None
                    elif ask is not None and ask <= order["px"] + 1e-9:
                        pos = dict(side=order["side"], px=order["px"], sh=order["sh"], fee=0.0)
                        order = None
                    continue
                if ask is not None and ask <= order["limit"] + 1e-9:
                    sh = math.floor((STAKE - 0.03) / (ask + fee(ask)) * 100) / 100
                    pos = dict(side=order["side"], px=ask, sh=sh, fee=sh * fee(ask))
                order = None
                continue
            if pos is not None:
                if s.get("cut") and left >= 10 and not pos.get("sold"):
                    bid, _ = side_quote(r, pos["side"])
                    mine = pm if pos["side"] == "UP" else 1 - pm
                    paid = pos["px"] + pos["fee"] / pos["sh"]
                    if bid is not None and bid - fee(bid) > mine + 0.01 and bid - fee(bid) < paid:
                        nxt = rows[i + 1][1] if i + 1 < len(rows) else None
                        nb = side_quote(nxt, pos["side"])[0] if nxt else None
                        if nb is not None and nb >= bid - 0.01:
                            pos["sold"] = nb * pos["sh"] - fee(nb) * pos["sh"]
                continue
            if call is None or left < 5 or sent:
                continue
            side = call
            p_side = pf if side == "UP" else 1 - pf
            m_side = pm if side == "UP" else 1 - pm
            mk = num(r["p_market"])
            mk_side = mk if side == "UP" else 1 - mk
            bid, ask = side_quote(r, side)
            if ask is None or ask >= 0.99:
                continue
            if s.get("last") and left > s["last"]:
                continue
            if s.get("minpx") and ask < s["minpx"]:
                continue
            if h == "5m" and ask < 0.25:
                continue
            if s.get("cap") and ask > s["cap"]:
                continue
            ceiling = tb.max_buy_price(p_side, FEE, 0.01)
            if m_side - mk_side > 0.20:
                continue
            if s.get("maker"):
                if bid is None:
                    continue
                px = round(bid + 0.01, 2)
                if px >= ask or p_side - px < 0.01:
                    continue
                order = dict(side=side, px=px, sh=math.floor(STAKE / px * 100) / 100)
                sent = True
                continue
            if ask > ceiling:
                continue
            order = dict(side=side, limit=min(ceiling, ask + 0.01))
            sent = True
        if pos:
            won = outcome[slug] == pos["side"]
            cost = pos["sh"] * pos["px"] + pos["fee"]
            back = pos["sold"] if "sold" in pos else (pos["sh"] if won else 0.0)
            res.append(dict(slug=slug, h=h, won=won, net=back - cost, cost=cost, px=pos["px"], cut="sold" in pos))
    return res


print(f"Replay of the live session in {folder}: {len(ticks)} windows (5m + 15m), ${STAKE:g} a trade\n")
print(f"{'strategy':10} {'trades':>6} {'won':>5} {'cut':>4} {'avg px':>6} {'net':>9} {'per $1':>7}  5m / 15m")
for name, s in STRATS.items():
    res = run(name, s)
    n = len(res)
    net = sum(x["net"] for x in res)
    staked = sum(x["cost"] for x in res)
    f = lambda hz: sum(x["net"] for x in res if x["h"] == hz)
    print(f"{name:10} {n:6d} {(sum(x['won'] for x in res) / n if n else 0):5.0%} {sum(x['cut'] for x in res):4d} "
          f"{(sum(x['px'] for x in res) / n if n else 0):6.2f} {net:+9.2f} {(net / staked * 100 if staked else 0):+6.1f}c"
          f"  {f('5m'):+.2f} / {f('15m'):+.2f}")
