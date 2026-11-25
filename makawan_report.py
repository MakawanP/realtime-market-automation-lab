#!/usr/bin/env python3
"""
makawan_report.py - the daily report: what each bot did, where money was made and lost, and anything odd
========================================================================================================
    python3 makawan_report.py                 # the last 24 hours, every bot folder found here
    python3 makawan_report.py --hours 72      # the last 3 days
    python3 makawan_report.py --all           # everything on record
    python3 makawan_report.py --open          # also open the report in your browser

It reads only the bots' own log folders (makawan_out_late, makawan_out_any, ...). Nothing is sent anywhere and
nothing is changed. The report is saved in makawan_reports/ as a text file and a web page.
The watchdog runs this every morning and sends the headline to your phone.
"""
from __future__ import annotations

import argparse
import csv
import html
import math
import os
import sys
import time
import webbrowser
from datetime import datetime

import makawan_bot as tb

REQUIRES = "2026.11.13"
if getattr(tb, "__version__", None) != REQUIRES:
    sys.exit("{} and makawan_bot.py come from different downloads. Put ALL the files from the latest download in one "
             "folder.".format(os.path.basename(__file__)))

OUT_DIR = "makawan_reports"


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def when(ts):
    d = datetime.fromtimestamp(ts, tb.DISPLAY_TZ)
    return f"{d.strftime('%a')} {d.strftime('%I:%M %p').lstrip('0')}"


def span(seconds):
    m = int(round(seconds / 60.0))
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60:02d} min"


def money(x):
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def is_terry(b):
    """The lab's 'terry' strategy follows Terry Calls: it trades 1h on purpose and its model column is not its reason."""
    return os.path.basename(os.path.normpath(b.get("folder") or "")) == "makawan_out_lab_terry"


def label_for(folder):
    name = os.path.basename(os.path.normpath(folder))
    rest = name.replace("makawan_out", "").strip("_").replace("_", " ")
    return rest or "single"


def find_folders(root="."):
    out = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if name.startswith("makawan_out") and not name.endswith(("_sim", "_rehearsal")) and os.path.isfile(os.path.join(p, "windows.csv")):
            out.append(p)
    order = {"live": -1, "late": 0, "any": 1, "late maker": 2, "any maker": 3}
    return sorted(out, key=lambda p: (order.get(label_for(p), 9), p))


# --------------------------------------------------------------------------
# loading one bot's record
# --------------------------------------------------------------------------
def load_bot(folder, t0, t1):
    """Everything the report needs about one bot between t0 and t1 (unix seconds)."""
    wins_all = read_csv(os.path.join(folder, "windows.csv"))
    events = read_csv(os.path.join(folder, "events.csv"))
    buys, system, orders = {}, [], 0
    for e in events:
        t = tb.parse_ts(e.get("time_utc"))
        if t is None:
            continue
        ev = e.get("event")
        if ev == "BUY":
            buys.setdefault(e.get("slug"), []).append((t, e))
        elif ev == "ORDER" and t0 <= t <= t1:
            orders += 1
        elif ev in ("HALT", "PAUSE", "RESUME") and t0 <= t <= t1:
            system.append((t, ev, e.get("note", "")))
    trades, windows, alltime = [], [], 0.0
    for r in wins_all:
        we, ws = tb.parse_ts(r.get("window_end")), tb.parse_ts(r.get("window_start"))
        net = fnum(r.get("net_pnl")) if r.get("net_pnl", "") != "" else fnum(r.get("pnl"))
        if r.get("traded_side") and net is not None:
            alltime += net
        if we is None or not (t0 <= we <= t1):
            continue
        windows.append({"ws": ws, "we": we, "horizon": r.get("horizon"), "slug": r.get("slug"),
                        "ptb": fnum(r.get("price_to_beat")), "settle": fnum(r.get("settlement")),
                        "outcome": r.get("outcome"), "provisional": r.get("provisional") in ("1", "True", "true"),
                        "order_status": r.get("order_status", "")})
        if not r.get("traded_side") or net is None:
            continue
        side, shares, px = r["traded_side"], fnum(r.get("shares")) or 0.0, fnum(r.get("avg_px")) or 0.0
        fee = fnum(r.get("fee")) or 0.0
        t = {"we": we, "horizon": r.get("horizon"), "slug": r.get("slug"), "side": side, "shares": shares, "px": px,
             "fee": fee, "net": net, "gross": net + fee, "cost": shares * px + fee, "won": side == r.get("outcome"),
             "liquidity": r.get("liquidity", ""), "left": None, "model": None, "market": None, "n_buys": 0}
        bl = buys.get(r.get("slug")) or []
        t["n_buys"] = len(bl)
        if bl:
            bt, be = bl[0]
            t["at"] = bt
            t["left"] = we - bt
            m, k = fnum(be.get("model")), fnum(be.get("market"))
            t["model"] = None if m is None else tb.side_chance(m, side)
            t["market"] = None if k is None else tb.side_chance(k, side)
        trades.append(t)
    state = tb.read_state(folder)
    return {"folder": folder, "label": label_for(folder), "trades": trades, "windows": windows, "system": system,
            "orders": orders, "alltime": alltime, "bankroll": state.get("bankroll"), "halted": state.get("halted", ""),
            "live": bool(state.get("live")), "stake": state.get("stake")}


# --------------------------------------------------------------------------
# sums and buckets
# --------------------------------------------------------------------------
def tally(trades):
    n = len(trades)
    net = sum(t["net"] for t in trades)
    out = {"n": n, "won": sum(t["won"] for t in trades), "staked": sum(t["cost"] for t in trades),
           "gross": sum(t["gross"] for t in trades), "fees": sum(t["fee"] for t in trades), "net": net, "se": None}
    if n >= 2:
        mean = net / n
        out["se"] = math.sqrt(sum((t["net"] - mean) ** 2 for t in trades) / (n - 1) * n)
    return out


def luck(s):
    """Plain-words reading of whether the result could be chance."""
    if s["n"] < 30 or s["se"] is None:
        return "too few trades to judge"
    if abs(s["net"]) < 2 * s["se"]:
        return f"could be luck (swings of ${2 * s['se']:,.0f} are normal for this many trades)"
    return "bigger than luck usually explains" + (" (so far)" if s["n"] < 200 else "")


PRICE_BUCKETS = [("under 25c", 0, 0.25), ("25-50c", 0.25, 0.50), ("50-75c", 0.50, 0.75), ("75-90c", 0.75, 0.90),
                 ("90c and up", 0.90, 1.01)]
LEFT_BUCKETS = [("under 30 s left", 0, 30), ("30-60 s left", 30, 60), ("1-2 min left", 60, 121),
                ("2-5 min left", 121, 301), ("over 5 min left", 301, 1e9)]
GAP_BUCKETS = [("model below market", -9, 0), ("model 0-5 pts above", 0, 0.05), ("model 5-10 pts above", 0.05, 0.10),
               ("model 10-20 pts above", 0.10, 0.20), ("model 20+ pts above", 0.20, 9)]


def bucket_rows(trades):
    """[(group title, [(label, tally)])] for the 'where the money came from' tables."""
    groups = [("by window length", [(h, [t for t in trades if t["horizon"] == h]) for h in ("5m", "15m", "1h")]),
              ("by price paid", [(nm, [t for t in trades if lo <= t["px"] < hi]) for nm, lo, hi in PRICE_BUCKETS]),
              ("by time left when it bought", [(nm, [t for t in trades if t["left"] is not None and lo <= t["left"] < hi])
                                               for nm, lo, hi in LEFT_BUCKETS])]
    gaps = [(nm, [t for t in trades if t["model"] is not None and t["market"] is not None
                  and lo <= t["model"] - t["market"] < hi]) for nm, lo, hi in GAP_BUCKETS]
    gaps.append(("no market price at entry", [t for t in trades if t["model"] is not None and t["market"] is None]))
    groups.append(("by how far the model was above the market", gaps))
    return [(title, [(nm, tally(ts)) for nm, ts in rows if ts]) for title, rows in groups]


def tick_times(path, t0, t1):
    """Sorted unix times of the rows in ticks.csv between t0 and t1. Jumps straight to t0 in a big file."""
    if not os.path.exists(path):
        return []
    lo_key, hi_key = tb.iso(max(0.0, t0)), tb.iso(t1)
    out = []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        lo, hi = 0, f.tell()
        while hi - lo > 65536:                        # times are in order, so halve the file until near t0
            mid = (lo + hi) // 2
            f.seek(mid)
            f.readline()
            key = f.readline()[:20].decode("ascii", "replace")
            if key and key < lo_key:
                lo = mid
            else:
                hi = mid
        f.seek(lo)
        if lo:
            f.readline()
        last = None
        for raw in f:
            key = raw[:20].decode("ascii", "replace")
            if not key[:1].isdigit() or key < lo_key:
                continue
            if key > hi_key:
                break
            if key != last:
                last = key
                t = tb.parse_ts(key.rstrip(","))
                if t is not None:
                    out.append(t)
    return out


def tick_rows(path, t0, t1):
    """The rows of ticks.csv between t0 and t1, as dicts (jumps straight to t0 in a big file)."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        header = next(csv.reader([f.readline()]), [])
    lo_key, hi_key = tb.iso(max(0.0, t0)), tb.iso(t1)
    out = []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        lo, hi = 0, f.tell()
        while hi - lo > 65536:
            mid = (lo + hi) // 2
            f.seek(mid)
            f.readline()
            key = f.readline()[:20].decode("ascii", "replace")
            if key and key < lo_key:
                lo = mid
            else:
                hi = mid
        f.seek(lo)
        f.readline()
        for raw in f:
            key = raw[:20].decode("ascii", "replace")
            if not key[:1].isdigit() or key < lo_key:
                continue
            if key > hi_key:
                break
            vals = next(csv.reader([raw.decode("utf-8", "replace")]), [])
            out.append(dict(zip(header, vals)))
    return out


def spot_vs_index(folders, t0, t1):
    """Did bringing the index up to date with Coinbase predict outcomes better? A section, or None if no data."""
    for folder in folders:
        outcome = {}
        for r in read_csv(os.path.join(folder, "windows.csv")):
            if r.get("outcome") in ("UP", "DOWN") and r.get("provisional") not in ("1", "True", "true"):
                outcome[r["slug"]] = 1.0 if r["outcome"] == "UP" else 0.0
        pts = []
        for r in tick_rows(os.path.join(folder, "ticks.csv"), t0, t1):
            a, b, m = fnum(r.get("p_model_index")), fnum(r.get("p_model_spot")), fnum(r.get("p_market"))
            y, left = outcome.get(r.get("slug")), fnum(r.get("secs_left"))
            if a is not None and b is not None and y is not None and left is not None:
                pts.append((left, a, b, m, y))
        if len(pts) < 50:
            continue
        rows = []
        buckets = [("under 30 s left", 0, 30), ("30 s - 2 min left", 30, 120), ("2 - 5 min left", 120, 300),
                   ("over 5 min left", 300, 1e9), ("all", 0, 1e9)]
        for name, lo, hi in buckets:
            xs = [p for p in pts if lo <= p[0] < hi]
            if not xs:
                continue
            bi = sum((a - y) ** 2 for _, a, _, _, y in xs) / len(xs)
            bs = sum((b - y) ** 2 for _, _, b, _, y in xs) / len(xs)
            mk = [p for p in xs if p[3] is not None]
            bm = sum((m - y) ** 2 for _, _, _, m, y in mk) / len(mk) if mk else None
            flips = sum(1 for _, a, b, _, _ in xs if (a >= 0.5) != (b >= 0.5))
            rows.append([name, len(xs), f"{bi:.4f}", f"{bs:.4f}", "-" if bm is None else f"{bm:.4f}",
                         ("Coinbase" if bs < bi - 1e-4 else "index only" if bi < bs - 1e-4 else "same"),
                         f"{flips / len(xs):.1%}"])
        return ("Coinbase vs index alone (model accuracy)",
                ["", "readings", "index only", "with Coinbase", "market", "better", "sides differ"], rows,
                [f"From {label_for(folder)}'s 5-second readings. Score = average squared miss against what really "
                 f"happened (lower is better; 0.25 = a coin flip). Each window gives many readings, so small gaps "
                 f"mean little until several days are in. 'Sides differ' = how often the two disagreed on UP/DOWN."])
    return None


def running(folder, windows, t0, t1):
    """(share of the period the bot was running, gaps of 10+ minutes, exact?) - from ticks.csv when it exists."""
    ts = tick_times(os.path.join(folder, "ticks.csv"), t0, t1)
    if len(ts) < 2:
        share, gaps = coverage(windows, t0, t1)
        return share, gaps, False
    up = sum(b - a for a, b in zip(ts, ts[1:]) if b - a <= 30)
    gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a >= 600]
    if t1 - ts[-1] >= 600:
        gaps.append((ts[-1], t1))
    start = ts[0] if (t0 <= 0 or ts[0] - t0 > 600) else t0     # a bot started part-way through is not a gap
    return min(1.0, up / max(1.0, t1 - start)), gaps, True


def coverage(windows, t0, t1):
    """How much of the period the bot was actually running, judged from its shortest windows. (share, gaps)"""
    for h in ("5m", "15m", "1h"):
        ws = sorted((w for w in windows if w["horizon"] == h and w["ws"] is not None), key=lambda w: w["ws"])
        if ws:
            break
    else:
        return None, []
    step = tb.HORIZON_SECONDS[h]
    first, last = ws[0]["ws"], ws[-1]["we"]
    gaps = [(a["we"], b["ws"]) for a, b in zip(ws, ws[1:]) if b["ws"] - a["we"] > step * 1.5]
    if t1 - last > 3 * step:
        gaps.append((last, t1))
    start = first if (t0 <= 0 or first - t0 > 2 * step) else t0   # a bot started part-way through is not a gap
    share = min(1.0, len(ws) * step / max(1.0, t1 - start))
    return share, [g for g in gaps if g[1] - g[0] >= 600]


def oddities(bot, cap, max_gap, no_trade, wakes=()):
    """[(kind, size, text)]: one entry per odd thing. `size` ranks entries of the same kind (bigger = worse)."""
    out, L = [], bot["label"]
    maker_far = [t for t in bot["trades"] if t["liquidity"] == "maker" and t["model"] is not None
                 and t["market"] is not None and max_gap < 1 and t["model"] - t["market"] > max_gap + 0.005]
    for t in bot["trades"]:
        what = f"{L}: {when(t['we'])} {t['horizon']} {t['side']} {t['shares']:.0f} @ {t['px'] * 100:.0f}c ({money(t['net'])})"
        if cap and t["cost"] > cap * 1.05 + 0.05:
            out.append(("cost more than the $%g stake limit" % cap, t["cost"], f"{what}: cost ${t['cost']:.2f}, "
                        f"over the ${cap:g} stake limit"))
        if t["model"] is not None and t["market"] is None:
            out.append(("were bought with no market price to compare against", -t["net"],
                        f"{what}: bought with no market price to compare against"))
        elif (t["liquidity"] != "maker" and max_gap < 1 and t["model"] is not None and t["market"] is not None
              and t["model"] - t["market"] > max_gap + 0.005):
            out.append((f"were bought with the model more than {max_gap * 100:.0f} points above the market",
                        t["model"] - t["market"], f"{what}: model was {(t['model'] - t['market']) * 100:.0f} points "
                        f"above the market (the rule skips above {max_gap * 100:.0f})"))
        if t["px"] < 0.05:
            out.append(("were long shots bought under 5c", -t["net"], f"{what}: bought a long shot under 5c"))
        if t["left"] is not None and t["left"] < 10:
            out.append(("were bought with under 10 s left", -t["net"],
                        f"{what}: bought with only {max(0, t['left']):.0f} s left"))
        if t["horizon"] in no_trade:
            out.append((f"traded a switched-off window length", -t["net"],
                        f"{what}: traded a {t['horizon']} window, which is switched off"))
        if t["n_buys"] > 1 and t["liquidity"] != "maker":
            out.append(("bought more than once in a window", t["n_buys"],
                        f"{what}: bought {t['n_buys']} times in one window (the rule is once)"))
        woke = [z for _, z in wakes if t.get("at") is not None and 0 <= t["at"] - z <= 90]
        if woke:
            out.append(("were bought within 90 s of the Mac waking up", -t["net"],
                        f"{what}: bought {t['at'] - woke[0]:.0f} s after the bot came back from a gap"))
    if maker_far:
        out.append(("", 0, f"{L}: {len(maker_far)} of its fills came after the market had moved more than "
                           f"{max_gap * 100:.0f} points against the model (those made {money(sum(t['net'] for t in maker_far))}). "
                           f"That is the usual way a resting bid gets filled: when it is wrong."))
    for w in bot["windows"]:
        if w["provisional"]:
            out.append(("were scored on a provisional result", 0,
                        f"{L}: {when(w['we'])} {w['horizon']} was scored on a provisional result, not the official one"))
        if w["settle"] is not None and w["ptb"] is not None and w["outcome"] in ("UP", "DOWN") and not w["provisional"]:
            if ("UP" if w["settle"] >= w["ptb"] else "DOWN") != w["outcome"]:
                out.append(("", 0, f"{L}: {when(w['we'])} {w['horizon']} was recorded {w['outcome']} but its settlement "
                                   f"price says otherwise"))
    for t, ev, note in bot["system"]:
        word = {"HALT": "TRADING HALTED", "PAUSE": "paused for the day", "RESUME": "halt cleared"}[ev]
        out.append(("", 0, f"{L}: {when(t)} {word}. {note}"))
    run, worst = 0, 0
    for t in sorted(bot["trades"], key=lambda x: x["we"]):
        run = 0 if t["won"] else run + 1
        worst = max(worst, run)
    if worst >= 5:
        out.append(("", 0, f"{L}: lost {worst} trades in a row at one point"))
    return out


def roll_up(label, items, most=3):
    """Lines for one bot. More than `most` of the same kind become one line that names the worst of them."""
    lines, kinds = [], {}
    for kind, size, text in items:
        if kind:
            kinds.setdefault(kind, []).append((size, text))
        else:
            lines.append(text)
    for kind, xs in kinds.items():
        if len(xs) <= most:
            lines += [t for _, t in xs]
        else:
            worst = max(xs, key=lambda x: x[0])[1].split(": ", 1)[1]
            lines.append(f"{label}: {len(xs)} trades {kind}. The worst: {worst}")
    return lines


# --------------------------------------------------------------------------
# building the report
# --------------------------------------------------------------------------
def build(folders, t0, t1, cap=None, max_gap=None, no_trade=None):
    """Returns (sections, summary_lines). A section is (title, header or None, rows, notes)."""
    cfg = tb.Config()
    cap = cfg.flat_stake if cap is None else cap
    max_gap = cfg.max_model_gap if max_gap is None else max_gap
    no_trade = cfg.no_trade_horizons if no_trade is None else no_trade
    bots = [load_bot(f, t0, t1) for f in folders]
    sections, summary, odd = [], [], []

    head = ["bot", "trades", "won", "staked", "before fees", "fees", "net", "is it luck?"]
    rows, notes = [], []
    for b in bots:
        s = tally(b["trades"])
        b["sum"] = s
        rows.append([b["label"] + (" (REAL)" if b["live"] else ""), s["n"], f"{s['won']}/{s['n']} ({s['won'] / s['n']:.0%})" if s["n"] else "-",
                     f"${s['staked']:,.2f}", money(s["gross"]), f"${s['fees']:,.2f}", money(s["net"]), luck(s)])
        if b["bankroll"] is not None:
            notes.append(f"{b['label']}: account now ${b['bankroll'] + b['alltime']:,.2f} "
                         f"(started ${b['bankroll']:,.2f}, all-time {money(b['alltime'])})"
                         + (f". TRADING HALTED: {b['halted']}" if b["halted"] else ""))
        known = [t for t in b["trades"] if t["model"] is not None and t["market"] is not None]
        if len(known) >= 10 and not is_terry(b):              # Terry's trades are not on Makawan's model
            notes.append(f"{b['label']}: when it bought, the model said {sum(t['model'] for t in known) / len(known):.0%} "
                         f"on average and the market said {sum(t['market'] for t in known) / len(known):.0%}; "
                         f"{sum(t['won'] for t in known) / len(known):.0%} actually won")
        summary.append(f"{b['label']}{' (REAL)' if b['live'] else ''} {money(s['net'])} ({s['n']} trades"
                       + (f", {s['won'] / s['n']:.0%} won" if s["n"] else "") + ")")
    sections.append(("Profit and loss by bot", head, rows, notes))

    for b in bots:
        if not b["trades"]:
            continue
        rows = []
        for title, items in bucket_rows(b["trades"]):
            rows.append([title.upper(), "", "", "", ""])
            for nm, s in items:
                rows.append(["  " + nm, s["n"], f"{s['won'] / s['n']:.0%}", money(s["net"]), money(s["net"] / s["n"])])
        best = max((x for _, items in bucket_rows(b["trades"]) for x in items), key=lambda x: x[1]["net"])
        worst = min((x for _, items in bucket_rows(b["trades"]) for x in items), key=lambda x: x[1]["net"])
        sections.append((f"Where {b['label']} made and lost money", ["", "trades", "won", "net", "per trade"], rows,
                         [f"best group: {best[0]} ({money(best[1]['net'])}); worst group: {worst[0]} "
                          f"({money(worst[1]['net'])}). Small groups swing a lot; do not read much into under 30 trades."]))

    every = sorted(((t, b["label"]) for b in bots for t in b["trades"]), key=lambda x: x[0]["net"])
    if every:
        def line(t, lab):
            left = "" if t["left"] is None else f", {max(0, t['left']):.0f} s left"
            return [when(t["we"]), lab, t["horizon"], f"{t['side']} {t['shares']:.0f} @ {t['px'] * 100:.0f}c{left}",
                    money(t["net"])]
        rows = [line(t, lab) for t, lab in every[:3] if t["net"] < 0]
        rows += [line(t, lab) for t, lab in reversed(every[-3:]) if t["net"] > 0]
        sections.append(("Biggest losses and wins", ["window closed", "bot", "length", "trade", "net"], rows, []))

    run_rows, gap_sets = [], []
    for b in bots:
        share, gaps, exact = running(b["folder"], b["windows"], t0, t1)
        odd += roll_up(b["label"], oddities(b, b.get("stake") or cap, 1.0 if is_terry(b) else max_gap,
                                            () if is_terry(b) else no_trade, wakes=gaps if exact else ()))
        if share is None:
            run_rows.append([b["label"], "nothing recorded in this period", ""])
            odd.append(f"{b['label']}: recorded nothing in this period. Was it running?")
            continue
        gap_sets.append(gaps)
        lost = sum(z - a for a, z in gaps)
        run_rows.append([b["label"], f"{'' if exact else 'roughly '}{share:.0%} of the time",
                         (f"{len(gaps)} gap(s), {span(lost)} in all: " + "; ".join(
                             f"{when(a)} to {when(z)} ({span(z - a)})" for a, z in gaps[:3])
                          + (" ..." if len(gaps) > 3 else "")) if gaps else "no gaps"])
        if b["orders"]:
            fills = sum(1 for t in b["trades"] if t["liquidity"] == "maker")
            run_rows.append(["", f"posted {b['orders']} limit bids, {fills} windows filled", ""])
    sections.append(("Was it running?", ["bot", "running", "gaps of 10 minutes or more"], run_rows,
                     ["A gap means the bot recorded nothing: the Mac was asleep, the bot was closed, or the internet was down."]))
    worst_gaps = max(gap_sets, key=lambda g: sum(z - a for a, z in g), default=[])
    if worst_gaps:
        a, z = max(worst_gaps, key=lambda g: g[1] - g[0])
        odd.insert(0, f"Not running for {span(sum(z2 - a2 for a2, z2 in worst_gaps))} in {len(worst_gaps)} gap(s). "
                      f"The longest: {when(a)} to {when(z)} ({span(z - a)}).")
    uniq = list(dict.fromkeys(odd))
    sv = spot_vs_index([b["folder"] for b in bots], t0, t1)
    if sv:
        sections.append(sv)
    sections.append(("Anything odd", None, [[o] for o in uniq] or [["Nothing odd found."]], []))
    total = sum(b["sum"]["net"] for b in bots)
    summary = [f"{when(t0)} to {when(t1)}: " + " | ".join(summary),
               f"{len(uniq)} thing(s) to look at, first: {uniq[0][:140]}" if uniq else "Nothing odd found."]
    return sections, summary, {"bots": bots, "odd": uniq, "total": total}


# --------------------------------------------------------------------------
# writing it out
# --------------------------------------------------------------------------
def as_text(title, sections):
    out = [title, "=" * len(title), ""]
    for name, head, rows, notes in sections:
        out += [name, "-" * len(name)]
        table = ([head] if head else []) + [[str(c) for c in r] for r in rows]
        if table:
            n = max(len(r) for r in table)
            widths = [max(len(str(r[i])) for r in table if i < len(r)) for i in range(n)]
            for r in table:
                out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip())
        out += ["  note: " + x for x in notes] + [""]
    return "\n".join(out)


def as_html(title, sections, real=False):
    e = html.escape
    parts = [f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title><style>
:root{{--bg:#fbfaf7;--ink:#1c1b19;--mute:#6b675f;--line:#e2ded5;--up:#17663a;--down:#a3261d;--card:#fff}}
@media(prefers-color-scheme:dark){{:root{{--bg:#16161a;--ink:#ecebe6;--mute:#9a978f;--line:#2d2d33;--up:#5fc98b;--down:#ef8279;--card:#1d1d22}}}}
body{{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:24px 16px}}
main{{max-width:980px;margin:0 auto}}h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:16px;margin:28px 0 8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 12px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:6px 10px 6px 0;border-bottom:1px solid var(--line);white-space:nowrap}}
td.wrap{{white-space:normal}}tr:last-child td{{border-bottom:0}}th{{color:var(--mute);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
.up{{color:var(--up)}}.down{{color:var(--down)}}.grp td{{color:var(--mute);font-size:12px;letter-spacing:.04em;padding-top:12px}}
p.note{{color:var(--mute);margin:8px 0 0}}</style></head><body><main><h1>{e(title)}</h1>
<p class="note">{"The bot marked (REAL) trades real money; its figures are the bot's own record, so check them with makawan_reconcile.py. The others are paper." if real else "Paper results from the bots' own logs. Nothing here is a real trade."}</p>"""]
    for name, head, rows, notes in sections:
        parts.append(f"<h2>{e(name)}</h2><div class='card'><table>")
        if head:
            parts.append("<tr>" + "".join(f"<th>{e(str(h))}</th>" for h in head) + "</tr>")
        for r in rows:
            cells = []
            grp = len(r) > 1 and all(c == "" for c in r[1:])
            for c in r:
                c = str(c)
                cls = "up" if c.startswith("+$") else "down" if c.startswith("-$") else ""
                if len(r) == 1 or len(c) > 60:
                    cls = (cls + " wrap").strip()
                cells.append(f"<td class='{cls}'>{e(c)}</td>")
            parts.append(f"<tr{' class=grp' if grp else ''}>" + "".join(cells) + "</tr>")
        parts.append("</table></div>" + "".join(f"<p class='note'>{e(x)}</p>" for x in notes))
    parts.append("</main></body></html>")
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Daily report for the Makawan bots (reads the log folders; changes nothing)")
    ap.add_argument("--hours", type=float, default=24.0, help="how far back to look (default 24)")
    ap.add_argument("--all", action="store_true", help="everything on record")
    ap.add_argument("--folders", default=None, help="comma-separated log folders (default: every makawan_out* folder here)")
    ap.add_argument("--stake", type=float, default=None, help="the stake limit to check trades against (default 20)")
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--open", action="store_true", help="open the report in your browser")
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--end-time", type=float, default=0, help=argparse.SUPPRESS)
    a = ap.parse_args()
    tb.set_display_tz(a.tz)
    folders = [f for f in a.folders.split(",") if f] if a.folders else find_folders(".")
    if not folders:
        sys.exit("No log folders (makawan_out...) with a windows.csv were found here. Run this from the bot's folder.")
    t1 = a.end_time or time.time()
    t0 = 0.0 if a.all else t1 - a.hours * 3600
    if a.all:
        firsts = [tb.parse_ts(r[0].get("window_start")) for r in
                  (read_csv(os.path.join(f, "windows.csv"))[:1] for f in folders) if r]
        t0 = min([x for x in firsts if x] or [t1 - 86400])
    if not a.folders:                               # leave out old folders with nothing in this period
        busy = [f for f in folders if load_bot(f, t0, t1)["windows"]]
        folders = busy or folders
    sections, summary, info = build(folders, t0, t1, cap=a.stake)
    stamp = datetime.fromtimestamp(t1, tb.DISPLAY_TZ)
    title = (f"Makawan report: {when(t0)} to {when(t1)} {tb.tz_abbr(t1)} "
             f"({stamp.strftime('%b')} {stamp.day}, {stamp.year})")
    text = as_text(title, sections)
    os.makedirs(a.out, exist_ok=True)
    base = os.path.join(a.out, "report-" + stamp.strftime("%Y-%m-%d") + ("-all" if a.all else ""))
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(as_html(title, sections, real=any(b["live"] for b in info["bots"])))
    print(text)
    print(f"Saved: {base}.txt and {base}.html")
    for s in summary:
        print("SUMMARY: " + s)
    if a.open:
        webbrowser.open("file://" + os.path.abspath(base + ".html"))


if __name__ == "__main__":
    main()
