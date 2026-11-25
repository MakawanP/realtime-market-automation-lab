#!/usr/bin/env python3
"""Late-entry backtest (read-only; never places orders, needs nothing but the bot's own record folders).

    python3 late_backtest.py                 # last 2 minutes, 5m + 15m markets, strict fills
    python3 late_backtest.py --fill seen     # kindest case: every order fills at the price on screen
    python3 late_backtest.py --last 60       # last minute only
    python3 late_backtest.py --horizon 5m    # one market length
    python3 late_backtest.py --band 85-90    # which entry band the detailed sections (F-K) look at

Question it answers: if I ignore the model and only buy late in a market when the price of one side is inside a
band (say 80-85c), do I make money -- holding to the end, or selling early at a target / stop?

How a trade is simulated:
  * readings come from ticks.csv, about one every 5-6 seconds per market;
  * the first reading in the allowed time where a side's ASK is inside the band sends a buy at that ask + 1c;
  * STRICT (default): it fills at the NEXT reading's ask, and only if that ask is still at or under the limit;
    otherwise the window is skipped (one try per window, like the live bot).  KINDEST (--fill seen): it fills at
    the price on screen.  Real life sits between the two;
  * fee = 0.095 x price x (1 - price) per share on every taker buy and sell (the rate measured on real fills);
  * 'sell at X' = once the side's BID reaches X a sell is sent (strict: it fills at the next reading's bid if that
    has not dropped more than 1c; a stop-loss accepts 3c);  'resting sell' = a limit sell waits at X, no fee.
Every market is counted once even when several bots recorded it."""
import argparse
import calendar
import csv
import glob
import math
import os
import time

FEE = 0.095
KEEP = 330           # seconds before the close that are loaded
BANDS = [(0.03, 0.10), (0.10, 0.20), (0.20, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70),
         (0.70, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.99)]
FAV = [(0.70, 0.80), (0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.99)]
TARGETS = [0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.97]
INSTANT = False


def fee(p):
    return FEE * p * (1 - p)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ts(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))


def load(here="."):
    folders = sorted(glob.glob(os.path.join(here, "makawan_out*"))) + \
        sorted(glob.glob(os.path.join(here, "makawan_out*", "archive-*")))
    outcome, best, clash = {}, {}, set()
    for d in folders:
        if d.endswith(("_sim", "_rehearsal")):
            continue
        w, t = os.path.join(d, "windows.csv"), os.path.join(d, "ticks.csv")
        if not (os.path.isfile(w) and os.path.isfile(t)):
            continue
        for r in csv.DictReader(open(w)):
            if r.get("outcome") in ("UP", "DOWN") and r.get("provisional") not in ("1", "True", "true"):
                if outcome.get(r["slug"], r["outcome"]) != r["outcome"]:
                    clash.add(r["slug"])
                outcome[r["slug"]] = r["outcome"]
        rows = {}
        for r in csv.DictReader(open(t)):
            left = num(r.get("secs_left"))
            if left is None or left > KEEP or left < 0:
                continue
            try:
                when = ts(r["time_utc"])
            except ValueError:
                continue
            rows.setdefault(r["slug"], []).append((left, num(r.get("bid")), num(r.get("ask")), when, r["horizon"]))
        for slug, lst in rows.items():
            if slug not in best or len(lst) > len(best[slug]):
                best[slug] = lst
    out = {}
    for slug, lst in best.items():
        if slug in outcome and slug not in clash:
            lst.sort(key=lambda x: -x[0])
            out[slug] = dict(rows=lst, outcome=outcome[slug], h=lst[0][4], end=lst[-1][3] + lst[-1][0])
    return out, len(clash)


def quote(row, side):
    """(bid, ask) for one side; the files hold the UP side's prices."""
    b, a = row[1], row[2]
    if side == "UP":
        return b, a
    return (None if a is None else round(1 - a, 4)), (None if b is None else round(1 - b, 4))


def bid_of(row, side):
    """The side's bid; 0 when nobody bids for it and it is offered for 5c or less (as good as worthless)."""
    b, a = quote(row, side)
    if b is None and a is not None and a <= 0.05:
        return 0.0
    return b


def mid_up(row):
    """The market's chance of UP at this reading, or None when the book says nothing useful."""
    b, a = row[1], row[2]
    if b is not None and a is not None:
        return (b + a) / 2
    if b is None and a is not None and a <= 0.10:
        return a / 2
    if a is None and b is not None and b >= 0.90:
        return (b + 1) / 2
    return None


def fill(rows, i, side, a):
    if INSTANT:
        return ("fill", i, i, side, a)
    if i + 1 >= len(rows):
        return ("miss",)
    nxt = rows[i + 1]
    a2 = quote(nxt, side)[1]
    if rows[i][0] - nxt[0] > 15 or a2 is None or a2 > a + 0.01 + 1e-9 or a2 < 0.01:
        return ("miss",)
    return ("fill", i, i + 1, side, a2)


def enter(rows, lo, hi, tmax, tmin=5):
    """First reading with tmin <= seconds left <= tmax where a side's ask is in [lo, hi)."""
    for i, r in enumerate(rows):
        if r[0] > tmax:
            continue
        if r[0] < max(tmin, 5):
            break
        c = []
        for side in ("UP", "DOWN"):
            a = quote(r, side)[1]
            if a is not None and lo - 1e-9 <= a < hi - 1e-9:
                c.append((a, side))
        if c:
            a, side = min(c)
            return fill(rows, i, side, a)
    return None


def enter_mark(rows, mark):
    """Buy the favourite at the first reading at or after the mark, whatever it costs (50-99c)."""
    for i, r in enumerate(rows):
        if r[0] > mark:
            continue
        if r[0] < mark - 8 or r[0] < 5:
            break
        m = mid_up(r)
        if m is None:
            return None
        side = "UP" if m >= 0.5 else "DOWN"
        a = quote(r, side)[1]
        if a is None or a < 0.5 or a > 0.99:
            return None
        return fill(rows, i, side, a)
    return None


def leave(rows, j, side, won, tp=None, sl=None, rest=False):
    """What one share is worth at the exit -> (value, 'tp' / 'sl' / None when held to the end)."""
    hold = 1.0 if won else 0.0
    if tp is None and sl is None:
        return hold, None
    tries = 0
    for k in range(j, len(rows)):
        b = bid_of(rows[k], side)
        if b is None:
            continue
        if rest and tp is not None:
            if k > j and b >= tp - 1e-9:
                return tp, "tp"
            why = None
        else:
            why = "tp" if tp is not None and b >= tp - 1e-9 else None
        if why is None and sl is not None and k > j and b <= sl + 1e-9:
            why = "sl"
        if why is None or rows[k][0] < 5:
            continue
        if INSTANT:
            if b < 0.01:
                continue
            return b - fee(b), why
        if k + 1 >= len(rows):
            continue
        b2 = bid_of(rows[k + 1], side)
        if b2 is not None and b2 >= 0.01 and b2 >= b - (0.01 if why == "tp" else 0.03) - 1e-9:
            return b2 - fee(b2), why
        tries += 1
        if tries >= 3:
            break
    return hold, None


def collect(data, lo, hi, tmax, tmin=5, mark=None):
    got, tries = [], 0
    for v in data.values():
        e = enter_mark(v["rows"], mark) if mark else enter(v["rows"], lo, hi, tmax, tmin)
        if e is None:
            continue
        tries += 1
        if e[0] == "fill":
            got.append(dict(v=v, d=e[1], j=e[2], side=e[3], px=e[4], won=v["outcome"] == e[3]))
    return tries, got


def rets(got, **how):
    out, why = [], []
    for g in got:
        val, w = leave(g["v"]["rows"], g["j"], g["side"], g["won"], **how)
        out.append((val / (g["px"] + fee(g["px"])) - 1, int(g["v"]["end"] // 900), g["v"]["end"]))
        why.append(w)
    return out, why


def stats(tr):
    """tr: (return per $1, 15-minute block, end time) -> n, mean, standard error (grouped by block)."""
    n = len(tr)
    if not n:
        return 0, 0.0, float("nan")
    m = sum(x[0] for x in tr) / n
    g = {}
    for x in tr:
        g[x[1]] = g.get(x[1], 0.0) + (x[0] - m)
    G = len(g)
    se = math.sqrt(G / (G - 1) * sum(v * v for v in g.values())) / n if G > 1 else float("nan")
    return n, m, se


def cents(x):
    return f"{x * 100:+.1f}c"


def verdict(n, m, se):
    if n < 30:
        return "too few trades"
    if se != se or se == 0:
        return ""
    t = m / se
    return "ahead, beyond luck" if t >= 2 else "behind, beyond luck" if t <= -2 else "could be luck"


def band(lo, hi):
    return f"{int(round(lo * 100)):2d}-{int(round(hi * 100)):2d}c"


def row(label, got, tries=None, cut=None):
    """One line: trades, price paid, win rate, win rate needed, profit per $1, +/-, reading."""
    tr, _ = rets(got)
    n, m, se = stats(tr)
    head = f"   {label:16}" + (f" {tries:5d}" if tries is not None else "")
    if not n:
        return head + f" {0:6d}"
    won = sum(g["won"] for g in got) / n
    paid = sum(g["px"] for g in got) / n
    need = sum(g["px"] + fee(g["px"]) for g in got) / n
    s = head + f" {n:6d} {paid * 100:4.0f}c {won:6.1%} {need:6.1%} {cents(m):>7} {se * 100:5.1f}c"
    if cut is not None:
        s += f" {cents(stats([x for x in tr if x[2] < cut])[1]):>8} {cents(stats([x for x in tr if x[2] >= cut])[1]):>8}"
    return s + "  " + verdict(n, m, se)


HEAD = f"{'trades':>6} {'paid':>5} {'won':>6} {'need':>6} {'per $1':>7} {'+/-':>6}"


def main():
    global INSTANT
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=120, help="only trade in the last N seconds (default 120)")
    ap.add_argument("--horizon", default="5m,15m", help="market lengths to include (default 5m,15m)")
    ap.add_argument("--stake", type=float, default=20.0)
    ap.add_argument("--band", default="80-85", help="entry band for the detailed sections, in cents (default 80-85)")
    ap.add_argument("--fill", choices=("next", "seen"), default="next")
    a = ap.parse_args()
    INSTANT = a.fill == "seen"
    L = a.last
    flo, fhi = (int(x) / 100 for x in a.band.split("-"))
    hz = set(a.horizon.split(","))
    data, clash = load()
    data = {k: v for k, v in data.items() if v["h"] in hz}
    if not data:
        print("No recorded markets found here.")
        return
    ends = sorted(v["end"] for v in data.values())
    cut = ends[len(ends) // 2]
    day = lambda t: time.strftime("%b %d %H:%M", time.gmtime(t))
    per_h = {h: sum(1 for v in data.values() if v["h"] == h) for h in sorted(hz)}
    print(f"LAST {L} SECONDS.  {len(data)} settled markets {per_h}, {day(ends[0])} to {day(ends[-1])} UTC.  "
          f"Fee 0.095 x p x (1-p).  ${a.stake:g} a trade.")
    print("Fills: " + ("KINDEST CASE - every order fills at the price on screen." if INSTANT else
                       "STRICT - an order fills at the next reading (~5s later) only if the price is still there."))
    print(f"First half / second half split at {day(cut)} UTC.  'need' = win rate that just covers price + fee.\n")

    # ---- A. is the market's own price right? ----
    print(f"A. HOW OFTEN THE FAVOURITE WON, by its market price at the {L}s mark (no trading, no fee)")
    print(f"   {'market price':13} {'markets':>7} {'avg price':>9} {'actually won':>12} {'lost':>6} {'difference':>10}")
    cal = {}
    for v in data.values():
        r = next((x for x in v["rows"] if x[0] <= L), None)
        if r is None or L - r[0] > 20 or r[1] is None or r[2] is None:
            continue
        mid = (r[1] + r[2]) / 2
        fav, p = ("UP", mid) if mid >= 0.5 else ("DOWN", 1 - mid)
        for lo, hi in ((0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.85), (0.85, 0.9), (0.9, 0.95), (0.95, 1.0)):
            if lo <= p < hi:
                cal.setdefault((lo, hi), []).append((p, v["outcome"] == fav))
    for (lo, hi), lst in sorted(cal.items()):
        n = len(lst)
        p_, w_ = sum(x[0] for x in lst) / n, sum(x[1] for x in lst) / n
        print(f"   {int(lo * 100):3d}-{int(hi * 100):3d}c     {n:7d} {p_:9.1%} {w_:12.1%} {1 - w_:6.1%} {(w_ - p_) * 100:+9.1f}pt")

    # ---- B. buy in a band, hold ----
    fills = {}
    print(f"\nB. BUY THE FIRST TIME A SIDE'S PRICE IS IN THE BAND (last {L}s), HOLD TO THE END")
    print(f"   {'buy band':16} {'tries':>5} {HEAD} {'1st half':>8} {'2nd half':>8}  reading")
    for lo, hi in BANDS:
        tries, got = collect(data, lo, hi, L)
        fills[(lo, hi)] = got
        print(row(band(lo, hi), got, tries, cut))

    # ---- C/D. sell early at a target ----
    for rest, title in ((False, "C. SAME BUYS, SELL EARLY ONCE THE PRICE REACHES A TARGET (taker sell, fee paid)"),
                        (True, "D. SAME BUYS, WITH A RESTING LIMIT SELL WAITING AT THE TARGET (no fee if filled)")):
        print(f"\n{title}")
        print("   profit per $1 staked, and (share of trades sold early). 'hold' repeats table B.")
        print(f"   {'buy band':8} {'n':>4} {'hold':>7} " + " ".join(f"{'sell ' + str(int(t * 100)) + 'c':>13}" for t in TARGETS))
        for lo, hi in BANDS:
            got = fills[(lo, hi)]
            if len(got) < 10:
                continue
            cells = []
            for t in TARGETS:
                if t < hi - 1e-9:
                    cells.append(f"{'':>13}")
                    continue
                tr, why = rets(got, tp=t, rest=rest)
                cells.append(f"{cents(stats(tr)[1])} ({sum(1 for w in why if w) / len(got):4.0%})".rjust(13))
            print(f"   {band(lo, hi):8} {len(got):4d} {cents(stats(rets(got)[0])[1]):>7} " + " ".join(cells))

    # ---- E. stop-loss ----
    stops = [0.05, 0.10, 0.20, 0.30]
    print("\nE. SAME BUYS, SELL IF THE PRICE DROPS BY THIS MUCH (stop-loss; taker sell, fee paid)")
    print(f"   {'buy band':8} {'n':>4} {'hold':>7} " + " ".join(f"{'drop ' + str(int(s * 100)) + 'c':>13}" for s in stops))
    for lo, hi in BANDS:
        got = fills[(lo, hi)]
        if len(got) < 10 or lo < 0.30:
            continue
        cells = []
        for d in stops:
            out, sold = [], 0
            for g in got:
                val, w = leave(g["v"]["rows"], g["j"], g["side"], g["won"], sl=g["px"] - d)
                sold += w is not None
                out.append((val / (g["px"] + fee(g["px"])) - 1, 0, 0))
            cells.append(f"{cents(stats(out)[1])} ({sold / len(got):4.0%})".rjust(13))
        print(f"   {band(lo, hi):8} {len(got):4d} {cents(stats(rets(got)[0])[1]):>7} " + " ".join(cells))

    # ---- F. when to enter ----
    slices = [(300, 120), (120, 90), (90, 60), (60, 30), (30, 5)]
    print("\nF. DOES IT MATTER HOW LATE YOU BUY?  (buy the first time the price is in the band inside each time slice; hold)")
    print(f"   {'band, secs left':16} {HEAD}  reading")
    for lo, hi in FAV:
        for hi_t, lo_t in slices:
            tries, got = collect(data, lo, hi, hi_t, lo_t)
            print(row(f"{band(lo, hi)} {hi_t:3d}-{lo_t:<3d}", got))
        print()

    # ---- G. rising or falling into the buy ----
    print("G. RISING OR FALLING?  The side's price when you buy, against its price ~30 seconds earlier (3c or more = a move)")
    print(f"   {'band, direction':16} {HEAD}  reading")
    for lo, hi in FAV[:4]:
        groups = {"rising": [], "flat": [], "falling": []}
        for g in fills[(lo, hi)]:
            rows = g["v"]["rows"]
            now = rows[g["d"]]
            a_now = quote(now, g["side"])[1]
            past = [quote(r, g["side"])[1] for r in rows[:g["d"]] if 25 <= r[0] - now[0] <= 40]
            past = [x for x in past if x is not None]
            if a_now is None or not past:
                continue
            ch = a_now - past[0]
            groups["rising" if ch >= 0.03 else "falling" if ch <= -0.03 else "flat"].append(g)
        for name, got in groups.items():
            print(row(f"{band(lo, hi)} {name}", got))
        print()

    # ---- H. how far trades dip ----
    print("H. HOW FAR DOES A TRADE GO AGAINST YOU BEFORE THE END?  (worst bid seen after buying, against the price paid)")
    print(f"   {'buy band':8} {'n':>4} {'avg dip':>8} {'typical':>8} {'dipped 10c+':>11} {'...and still won':>16} "
          f"{'dipped 20c+':>11} {'...and still won':>16} {'worst run $':>11} {'losses in a row':>15}")
    for lo, hi in BANDS:
        got = fills[(lo, hi)]
        if len(got) < 10 or lo < 0.50:
            continue
        dips = []
        for g in got:
            lows = [bid_of(r, g["side"]) for r in g["v"]["rows"][g["j"] + 1:]]
            lows = [x for x in lows if x is not None]
            dips.append((max(0.0, g["px"] - min(lows)) if lows else 0.0, g["won"]))
        n = len(dips)
        d10 = [w for d, w in dips if d >= 0.10]
        d20 = [w for d, w in dips if d >= 0.20]
        srt = sorted(d for d, _ in dips)
        eq = peak = worst = 0.0
        streak = longest = 0
        tr, _ = rets(got)
        for (r, _, _), g in sorted(zip(tr, got), key=lambda x: x[0][2]):
            eq += r * a.stake
            peak = max(peak, eq)
            worst = max(worst, peak - eq)
            streak = 0 if g["won"] else streak + 1
            longest = max(longest, streak)
        pct = lambda lst: f"{sum(lst) / len(lst):.0%}" if lst else "-"
        print(f"   {band(lo, hi):8} {n:4d} {sum(srt) / n * 100:7.1f}c {srt[n // 2] * 100:7.1f}c {len(d10) / n:11.0%} {pct(d10):>16} "
              f"{len(d20) / n:11.0%} {pct(d20):>16} {-worst:11.0f} {longest:15d}")

    # ---- I/J/K. the chosen band in detail ----
    tries, got = collect(data, flo, fhi, L)
    name = band(flo, fhi).strip()
    if len(got) >= 10:
        n = len(got)
        base = stats(rets(got)[0])[1]
        print(f"\nI. BUY AT {name}, SELL AT A TARGET: what happens to the trades   ({n} trades; hold to the end = {cents(base)} per $1)")
        print(f"   {'target':7} {'reached':>8} {'per $1 on those':>15} | {'never reached':>13} {'of those, won':>13} "
              f"{'per $1 on those':>15} | {'all trades per $1':>17} {'resting sell':>12}")
        for t in [x for x in (0.85, 0.88, 0.90, 0.93, 0.95, 0.97, 0.98) if x >= fhi - 1e-9]:
            tr, why = rets(got, tp=t)
            hit = [x for x, w in zip(tr, why) if w]
            miss = [(x, g) for x, w, g in zip(tr, why, got) if not w]
            mw = sum(g["won"] for _, g in miss) / len(miss) if miss else 0
            print(f"   {int(t * 100):4d}c   {len(hit) / n:8.0%} {cents(stats(hit)[1]) if hit else '-':>15} | {len(miss) / n:13.0%} {mw:13.0%} "
                  f"{cents(stats([x for x, _ in miss])[1]) if miss else '-':>15} | {cents(stats(tr)[1]):>17} "
                  f"{cents(stats(rets(got, tp=t, rest=True)[0])[1]):>12}")

        print(f"\nK. BUY AT {name}: TARGET x STOP-LOSS TOGETHER  (profit per $1 staked; risk/reward = stop distance : target distance)")
        tps = [None] + [x for x in (0.90, 0.95, 0.97) if x >= fhi + 0.02]
        sls = [None, 0.05, 0.10, 0.20, 0.30]
        print(f"   {'':14}" + " ".join(f"{('no target' if t is None else 'sell ' + str(int(t * 100)) + 'c'):>11}" for t in tps))
        for d in sls:
            cells = []
            for t in tps:
                out = []
                for g in got:
                    val, w = leave(g["v"]["rows"], g["j"], g["side"], g["won"], tp=t, sl=None if d is None else g["px"] - d)
                    out.append((val / (g["px"] + fee(g["px"])) - 1, 0, 0))
                cells.append(f"{cents(stats(out)[1]):>11}")
            print(f"   {('no stop' if d is None else 'stop -' + str(int(d * 100)) + 'c'):14}" + " ".join(cells))

    # ---- L. accuracy of the market price by time left ----
    print("\nL. WHEN IS THE MARKET PRICE MOST ACCURATE?  (the favourite at each moment, all markets)")
    print(f"   {'secs left':9} {'markets':>7} {'avg favourite price':>19} {'favourite won':>13} {'favourite LOST':>14} "
          f"{'difference':>10} {'error score':>11}")
    for mark in (300, 240, 180, 120, 90, 60, 45, 30, 20, 10, 5):
        lst = []
        for v in data.values():
            r = next((x for x in v["rows"] if x[0] <= mark), None)
            if r is None or mark - r[0] > 8:
                continue
            m = mid_up(r)
            if m is None:
                continue
            fav, p = ("UP", m) if m >= 0.5 else ("DOWN", 1 - m)
            y = v["outcome"] == fav
            lst.append((p, y, (p - y) ** 2))
        if len(lst) < 30:
            continue
        n = len(lst)
        p_, w_ = sum(x[0] for x in lst) / n, sum(x[1] for x in lst) / n
        print(f"   {mark:6d}s   {n:7d} {p_:19.1%} {w_:13.1%} {1 - w_:14.1%} {(w_ - p_) * 100:+9.1f}pt {sum(x[2] for x in lst) / n:11.3f}")
    print("   (error score: 0 = perfect, 0.25 = coin flip.)")

    # ---- M. reversals in the final seconds ----
    print("\nM. HOW OFTEN DOES THE LEADER LOSE AT THE VERY END?  (leader ~10 seconds before the close)")
    print(f"   {'leader price at 10s':20} {'markets':>7} {'leader lost':>11}")
    rev = {}
    for v in data.values():
        r = next((x for x in v["rows"] if x[0] <= 12), None)
        if r is None or r[0] < 4:
            continue
        m = mid_up(r)
        if m is None:
            continue
        fav, p = ("UP", m) if m >= 0.5 else ("DOWN", 1 - m)
        for lo, hi in ((0.5, 0.7), (0.7, 0.9), (0.9, 0.97), (0.97, 1.01)):
            if lo <= p < hi:
                rev.setdefault((lo, hi), []).append(v["outcome"] != fav)
        rev.setdefault((0.5, 1.01), []).append(v["outcome"] != fav)
    for (lo, hi), lst in sorted(rev.items(), key=lambda x: (x[0][1] - x[0][0] > 0.4, x[0])):
        lab = "all markets" if hi - lo > 0.4 else f"{int(lo * 100)}-{min(100, int(hi * 100))}c"
        print(f"   {lab:20} {len(lst):7d} {sum(lst) / len(lst):11.1%}")

    # ---- N. liquidity by time left ----
    print("\nN. IS THERE SOMEONE TO TRADE WITH?  (top of the order book by time left; the records do not hold order sizes)")
    print(f"   {'secs left':10} {'readings':>8} {'both sides quoted':>17} {'avg gap bid-ask':>15} {'gap 1c or less':>14} {'leader can be bought':>20}")
    for hi_t, lo_t in ((300, 180), (180, 120), (120, 90), (90, 60), (60, 30), (30, 15), (15, 5), (5, 0)):
        n = both = tight = can = 0
        gap = 0.0
        for v in data.values():
            for r in v["rows"]:
                if not (lo_t <= r[0] < hi_t):
                    continue
                n += 1
                if r[1] is not None and r[2] is not None:
                    both += 1
                    gap += r[2] - r[1]
                    tight += r[2] - r[1] <= 0.0101
                m = mid_up(r)
                if m is not None:
                    x = quote(r, "UP" if m >= 0.5 else "DOWN")[1]
                    can += x is not None and x <= 0.99
        if n:
            print(f"   {hi_t:3d}-{lo_t:<3d}s   {n:8d} {both / n:17.0%} {(gap / both * 100 if both else 0):14.1f}c "
                  f"{(tight / both if both else 0):14.0%} {can / n:20.0%}")

    # ---- P. fixed time or wait for a price? ----
    print("\nP. BUY THE LEADER AT A FIXED MOMENT, WHATEVER IT COSTS (50-99c), HOLD  -- compare with waiting for a price (table B)")
    print(f"   {'buy at':16} {'tries':>5} {HEAD} {'1st half':>8} {'2nd half':>8}  reading")
    for mark in (120, 90, 60, 30, 15):
        tries, got = collect(data, 0, 0, 0, mark=mark)
        print(row(f"{mark}s before end", got, tries, cut))

    if clash:
        print(f"\n({clash} markets left out because two record folders disagree on how they settled.)")
    print("\nReading the tables: '+/-' is one standard error. Dozens of combinations are tested on the same few days, so some "
          "will look good\nby luck alone -- trust a line only if both halves agree and the reading says 'beyond luck'.")


if __name__ == "__main__":
    main()
