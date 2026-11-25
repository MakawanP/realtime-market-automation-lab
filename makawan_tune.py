#!/usr/bin/env python3
"""
makawan_tune.py - measure and tune the model instead of trusting the video's example numbers
==========================================================================================
The numbers in the video (57/53/47/43%, a 50/50 blend, a plain bell curve) were examples. This tool replaces
them with numbers measured on data.

  python makawan_tune.py backtest --days 14
      Downloads Polymarket US's public BRTI history, replays thousands of past 5m / 15m / 1h windows and finds the
      volatility setting and tail shape whose chances were best calibrated, judged on the LAST 30% of the data that
      was not used to pick them. No market prices needed. Writes makawan_out/params.json.

  python makawan_tune.py fit-blend
      After the bot has run for a while (it writes makawan_out/ticks.csv), learns how much to trust the model vs the
      market's own price. Refuses to change anything until there are enough settled windows to mean something.

Then run the bot with:   python makawan_bot.py --params makawan_out/params.json

Pure Python, no extra packages. Past calibration does not guarantee future calibration, and a model that is
well calibrated is not the same thing as a profitable one: you still pay the spread and the fee.
"""
from __future__ import annotations

import argparse
import shutil
import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

import makawan_bot as tb

REQUIRES = "2026.11.13"
if getattr(tb, "__version__", None) != REQUIRES:
    sys.exit("{} and makawan_bot.py come from different downloads. Put ALL the files from the latest download in one "
             "folder, replacing the old ones (makawan_bot.py reports version {}, this file needs {}).".format(
                 os.path.basename(__file__), getattr(tb, "__version__", "older than 2026.11.08.4"), REQUIRES))

CHUNK = 3000                      # seconds of 1-second candles per request
EPS = 1e-4
LOOKBACK = 1800                   # must match Config.vol_lookback_s
FLOOR = 5.0 / math.sqrt(60.0)     # Config.vol_floor_min
AVG = int(tb.Config().avg_window_s)   # settlement averaging window in seconds (1 = the closing print)

SW_GRID = (0.0, 0.5, 1.0)
SCALE_GRID = (0.8, 0.9, 1.0, 1.1, 1.2, 1.3)
TAIL_GRID = ((0.0, 2.0), (0.05, 2.0), (0.10, 2.0), (0.10, 2.5), (0.20, 2.0))
CKPT_FRACS = (0.2, 0.5, 0.8)      # share of the window already elapsed
CKPT_TAU = (30,)                  # plus: seconds left (the averaging-aware end game)


# --------------------------------------------------------------------------
# 1. history
# --------------------------------------------------------------------------
TUNE_DIR = "makawan_tune"          # proposals and applied settings shared by all the bots


def fetch_history(api: tb.Api, t_start: int, t_end: int, cache_path: str, refresh=False, say=print, pace=0.12):
    """{unix_second: price} for [t_start, t_end], using a local CSV cache."""
    have = {}
    if os.path.exists(cache_path) and not refresh:
        with open(cache_path, newline="") as f:
            for r in csv.reader(f):
                if r and r[0].isdigit():
                    have[int(r[0])] = float(r[1])
    chunks = list(range(t_start - t_start % CHUNK, t_end, CHUNK))
    todo = [c for c in chunks if sum(1 for t in range(c, min(c + CHUNK, t_end)) if t in have) < 0.98 * min(CHUNK, t_end - c)]
    say(f"{len(chunks)} blocks of history, {len(todo)} to download" + (" (rest from cache)" if len(todo) < len(chunks) else ""))
    for k, c in enumerate(todo, 1):
        for ts, close, ticks in api.brti(c, min(c + CHUNK - 1, t_end)):
            have[ts] = close
        if k % 25 == 0 or k == len(todo):
            say(f"  downloaded {k}/{len(todo)}")
        time.sleep(pace)                                     # ~8 requests/s, the limit is 25
    os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
    with open(cache_path, "w", newline="") as f:
        w = csv.writer(f)
        for t in sorted(have):
            w.writerow([t, have[t]])
    return {t: p for t, p in have.items() if t_start <= t <= t_end}


def to_series(prices: dict):
    """Forward-filled list p with p[i] = price at t0 + i."""
    t0, t1 = min(prices), max(prices)
    p, prev = [], None
    for t in range(t0, t1 + 1):
        prev = prices.get(t, prev)
        p.append(prev)
    first = next(x for x in p if x is not None)
    return t0, [first if x is None else x for x in p]


class Hist:
    """Prefix sums so the volatility estimate at any second costs O(1)."""

    def __init__(self, p, halflife=300.0):
        n = len(p)
        self.p, self.n = p, n
        self.P = [0.0] * (n + 1)
        self.CD1 = [0.0] * n
        self.CD60 = [0.0] * n
        self.EW = [0.0] * n
        lam = 0.5 ** (1.0 / halflife)
        num = den = 0.0
        for j in range(n):
            self.P[j + 1] = self.P[j] + p[j]
            if j >= 1:
                d = p[j] - p[j - 1]
                self.CD1[j] = self.CD1[j - 1] + d * d
                num, den = num * lam + d * d, den * lam + 1.0
                self.EW[j] = num / den
            if j >= 60:
                d = p[j] - p[j - 60]
                self.CD60[j] = self.CD60[j - 1] + d * d
            elif j >= 1:
                self.CD60[j] = self.CD60[j - 1]

    def avg(self, a, b):
        """Mean of p[a..b-1]."""
        return (self.P[b] - self.P[a]) / (b - a)

    def sigma_parts(self, i):
        """(long-window sigma, recent sigma) per sqrt(second) at index i; same formulas as tb.estimate_vol."""
        c60 = LOOKBACK - 59
        s_long = math.sqrt((self.CD60[i] - self.CD60[i - c60]) / c60) / math.sqrt(60.0)
        rms1 = math.sqrt((self.CD1[i] - self.CD1[i - LOOKBACK]) / LOOKBACK)
        ratio = min(2.0, max(0.5, s_long / rms1)) if rms1 > 0 else 1.0
        return s_long, math.sqrt(self.EW[i]) * ratio


def build_samples(t0, hist: Hist, horizon: str):
    """One sample per (window, checkpoint): everything the probability needs except the volatility."""
    dur = tb.HORIZON_SECONDS[horizon]
    out = []
    first = (t0 + LOOKBACK + 61) // dur + 1
    last = (t0 + hist.n - 2) // dur - 1
    taus = sorted({round(dur * (1 - f)) for f in CKPT_FRACS} | set(CKPT_TAU), reverse=True)
    for k in range(first, last + 1):
        ws, we = k * dur, k * dur + dur
        iw, ie = ws - t0, we - t0
        K = hist.avg(iw - AVG, iw)
        y = 1 if hist.avg(ie - AVG, ie) >= K else 0
        for ci, tau in enumerate(taus):
            i = ie - tau
            S = hist.p[i]
            obs = hist.avg(ie - AVG, i + 1) if tau < AVG else None
            mean, sd = tb.settle_stats(S, tau, 1.0, obs, float(AVG))
            sl, ss = hist.sigma_parts(i)
            out.append((mean - K, sd, sl, ss, y, ci, ws))
    return out


# --------------------------------------------------------------------------
# 2. scoring
# --------------------------------------------------------------------------
def predict(sample, sw, scale, tw, ts2):
    dm, fv, sl, ss, y, _, _ = sample
    sig = max(math.sqrt((1 - sw) * sl * sl + sw * ss * ss) * scale, FLOOR)
    sd = sig * fv
    p = (1.0 if dm >= 0 else 0.0) if sd < 1e-9 else tb.tail_cdf(dm / sd, tw, ts2)
    return min(max(p, EPS), 1 - EPS)


def score(samples, sw, scale, tw, ts2):
    ll = br = 0.0
    for s in samples:
        p = predict(s, sw, scale, tw, ts2)
        y = s[4]
        ll -= math.log(p if y else 1 - p)
        br += (p - y) ** 2
    n = max(1, len(samples))
    return ll / n, br / n


def losses(samples, cfgv):
    out = []
    for s in samples:
        p = predict(s, *cfgv)
        out.append(-math.log(p if s[4] else 1 - p))
    return out


def split(samples, frac=0.7):
    times = sorted({s[6] for s in samples})
    cut = times[int(len(times) * frac)]
    return [s for s in samples if s[6] < cut], [s for s in samples if s[6] >= cut]


VIDEO = (0.0, 1.0, 0.0, 2.0)
PRO_DEFAULT = (0.5, 1.0, 0.10, 2.0)


def calibration_table(samples, cfgv, bins=10):
    rows = [[0, 0.0, 0] for _ in range(bins)]
    for s in samples:
        p = predict(s, *cfgv)
        b = min(bins - 1, int(p * bins))
        rows[b][0] += 1
        rows[b][1] += p
        rows[b][2] += s[4]
    return [(i / bins, (i + 1) / bins, n, sp / n, up / n) for i, (n, sp, up) in enumerate(rows) if n]


TIMING_EDGES = (0.0, 0.05, 0.10, 0.20, 0.35, 0.50, 1.01)      # share of the window already elapsed


def call_timing(t0, hist: Hist, horizon: str, cv):
    """When should the first call be allowed? Replays every window second by second (model alone, no market
    price) with the normal 57/53/47/43% rules and no waiting, and records for each first call: how far into the
    window it came, whether it was right, and whether the call later flipped to the opposite side."""
    dur = tb.HORIZON_SECONDS[horizon]
    step = max(5, dur // 60)
    sw, sc, tw, ts2 = cv
    rules = tb.Config().view(horizon)
    first = (t0 + LOOKBACK + 61) // dur + 1
    last = (t0 + hist.n - 2) // dur - 1
    n_windows, rows = 0, []
    for k in range(first, last + 1):
        iw, ie = k * dur - t0, k * dur + dur - t0
        K = hist.avg(iw - AVG, iw)
        up = hist.avg(ie - AVG, ie) >= K
        state, call, flipped = None, None, False
        for off in range(step, dur - step + 1, step):
            i, tau = iw + off, dur - off
            obs = hist.avg(ie - AVG, i + 1) if tau < AVG else None
            mean, sd = tb.settle_stats(hist.p[i], tau, 1.0, obs, float(AVG))
            sl, ss = hist.sigma_parts(i)
            sig = max(math.sqrt((1 - sw) * sl * sl + sw * ss * ss) * sc, FLOOR)
            p = (1.0 if mean >= K else 0.0) if sd * sig < 1e-9 else tb.tail_cdf((mean - K) / (sd * sig), tw, ts2)
            state = tb.next_call(state, p, rules)
            if state and call is None:
                call = (off / dur, state)
            elif state and call and state != call[1]:
                flipped = True
        n_windows += 1
        if call:
            rows.append((call[0], int(call[1] == ("UP" if up else "DOWN")), flipped))
    return n_windows, rows


def timing_lines(h, n_windows, rows):
    dur = tb.HORIZON_SECONDS[h]
    cur = max(45.0, 0.10 * dur)
    out = [f"  when the first call comes (model alone, no waiting; the bot now waits {cur:.0f}s = {cur / dur:.0%} of this window):",
           f"    {'first call at':16s} {'calls':>6s} {'right':>7s} {'later flipped':>14s}"]
    for lo, hi in zip(TIMING_EDGES, TIMING_EDGES[1:]):
        r = [x for x in rows if lo <= x[0] < hi]
        if len(r) >= 5:
            out.append(f"    {lo:4.0%} - {min(hi, 1):4.0%}      {len(r):6d} {sum(x[1] for x in r) / len(r):7.0%} "
                       f"{sum(x[2] for x in r) / len(r):14.0%}")
    out.append(f"    {len(rows)} of {n_windows} windows got a call at some point")
    return out


def run_backtest(args, api, say=print):
    cfg_h = [h for h in args.horizons.split(",") if h]
    end = int(args.end_time or time.time()) - 5
    start = end - int(args.days * 86400)
    cache = os.path.join(args.out, "brti_cache.csv")
    prices = fetch_history(api, start, end, cache, args.refresh, say,
                           pace=0.0 if args.simulate else max(0.12, float(getattr(args, "pace", 0.12) or 0.12)))
    if len(prices) < 4 * 3600:
        sys.exit("Not enough history came back. Check your connection or try --refresh.")
    t0, p = to_series(prices)
    say(f"{len(p):,} seconds of index prices ({len(p) / 86400:.1f} days). Building windows...")
    hist = Hist(p)
    report, result, per_h = [], {}, {}
    for h in cfg_h:
        smp = build_samples(t0, hist, h)
        if len(smp) < 200:
            say(f"{h}: only {len(smp)} samples, skipping")
            continue
        train, test = split(smp)
        per_h[h] = (smp, train, test)
    if not per_h:
        sys.exit("Nothing to evaluate.")

    lines = [f"Backtest on {len(p) / 86400:.1f} days of BRTI, ending {tb.local_iso(end)} {tb.tz_abbr(end)}", ""]
    updates = {}
    for h, (smp, train, test) in per_h.items():
        cands = [(score(train, sw, sc, tw, ts2)[0], sw, sc, tw, ts2)
                 for sw in SW_GRID for sc in SCALE_GRID for tw, ts2 in TAIL_GRID]
        _, sw, sc, tw, ts2 = min(cands)
        chosen = (sw, sc, tw, ts2)
        ll = {name: score(test, *cv) for name, cv in (("video model", VIDEO), ("pro default", PRO_DEFAULT),
                                                      ("tuned", chosen))}
        ll["coin flip"] = (math.log(2), 0.25)
        diff = [a - b for a, b in zip(losses(test, VIDEO), losses(test, chosen))]
        md = sum(diff) / len(diff)
        se = math.sqrt(sum((d - md) ** 2 for d in diff) / max(1, len(diff) - 1) / len(diff))
        nwin = len({s[6] for s in smp})
        lines += [f"=== {h}: {nwin} windows, {len(smp)} checks ({len(train)} to choose, {len(test)} held out)",
                  f"  chosen: {sw:.0%} weight on recent volatility, vol x{sc:g}, tails {tw:.0%} x{ts2:g}",
                  f"  {'held-out data':14s} {'log loss':>9s} {'Brier':>7s}"]
        for name in ("coin flip", "video model", "pro default", "tuned"):
            lines.append(f"  {name:14s} {ll[name][0]:9.4f} {ll[name][1]:7.4f}")
        lines.append(f"  tuned beats video by {md:+.4f} log loss per check (+/- {se:.4f}; the checks overlap, so the true "
                     f"uncertainty is larger)")
        better = md > max(0.001, 2 * se)
        if nwin < 200:
            lines.append(f"  note: only {nwin} windows; treat this as indicative and run again with more --days")
        lines.append("  -> " + (("PROPOSED (nothing is changed until you apply it)" if getattr(args, "propose", False)
                                 else "saved to params.json") if better else
                                "NOT saved: no clear gain on held-out data (needs to beat the video by 2 standard errors), "
                                "so this window length keeps its defaults"))
        lines.append("  calibration of the tuned model (held-out): predicted vs what actually happened")
        for lo, hi, n, mp, ur in calibration_table(test, chosen):
            lines.append(f"    {lo:4.0%}-{hi:4.0%}  n={n:5d}  predicted {mp:5.1%}  actual UP {ur:5.1%}")
        nw, trows = call_timing(t0, hist, h, chosen if better else PRO_DEFAULT)
        lines += timing_lines(h, nw, trows)
        lines.append("")
        if better:
            updates[h] = {"vol_short_weight": sw, "vol_scale": sc, "tail_w": tw, "tail_s2": ts2}
    text = "\n".join(lines)
    say(text)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "backtest_report.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    if getattr(args, "propose", False):
        write_proposal(args.out, updates, f"backtest {args.days:g}d ending {tb.iso(end)}", say)
        return updates
    if updates and not args.no_write:
        path = os.path.join(args.out, "params.json")
        merge_params(path, updates, f"backtest {args.days:g}d ending {tb.iso(end)}")
        say(f"Wrote {path} for: {', '.join(k for k in updates)}.  Use it:  python makawan_bot.py --params {path}")
    elif not updates:
        say("No window length showed a clear improvement on held-out data, so params.json was not changed.")
    return updates


# --------------------------------------------------------------------------
# 3. learning how much to trust the market (needs the bot's own logs)
# --------------------------------------------------------------------------
def solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit_logit(X, y, l2=1e-3, iters=60):
    """Logistic regression with an intercept (last coefficient), ridge on the slopes. Returns the coefficients."""
    k = len(X[0]) + 1
    w = [0.0] * k
    for _ in range(iters):
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(X, y):
            z = xi + [1.0]
            p = tb._sigmoid(sum(a * b for a, b in zip(w, z)))
            for i in range(k):
                g[i] += (p - yi) * z[i]
                for j in range(k):
                    H[i][j] += p * (1 - p) * z[i] * z[j]
        for i in range(k - 1):
            g[i] += l2 * w[i]
            H[i][i] += l2
        step = solve(H, g)
        w = [a - b for a, b in zip(w, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return w


def load_blend_samples(out_dir, horizons, fracs=(0.25, 0.5, 0.75)):
    outcomes = {}
    with open(os.path.join(out_dir, "windows.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["outcome"] and not int(r.get("provisional") or 0):
                outcomes[r["slug"]] = (1 if r["outcome"] == "UP" else 0, r["window_start"])
    by_slug = {}
    with open(os.path.join(out_dir, "ticks.csv"), newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["slug"] in outcomes and r["horizon"] in horizons and r["p_market"] and r["p_model"]:
                by_slug.setdefault(r["slug"], []).append(r)
    samples = {h: [] for h in horizons}
    for slug, rows in by_slug.items():
        h = rows[0]["horizon"]
        dur = tb.HORIZON_SECONDS.get(h)
        if not dur:
            continue
        y, ws = outcomes[slug]
        for fr in fracs:
            tgt = fr * dur
            best = min(rows, key=lambda r: abs((dur - float(r["secs_left"])) - tgt))
            if abs((dur - float(best["secs_left"])) - tgt) <= 15:
                pm = min(max(float(best["p_model"]), EPS), 1 - EPS)
                pk = min(max(float(best["p_market"]), EPS), 1 - EPS)
                samples[h].append((tb._logit(pm), tb._logit(pk), pm, pk, y, ws, slug))
    return samples


def run_fit_blend(args, say=print):
    horizons = [h for h in args.horizons.split(",") if h]
    try:
        samples = load_blend_samples(args.out, horizons)
    except FileNotFoundError as e:
        sys.exit(f"{e.filename} not found. Run the bot for a while first (it writes ticks.csv and windows.csv).")
    lines, updates = [], {}
    for h in horizons:
        smp = samples[h]
        slugs = sorted({s[6] for s in smp}, key=lambda g: next(s[5] for s in smp if s[6] == g))
        n = len(slugs)
        lines.append(f"=== {h}: {n} settled windows with market prices, {len(smp)} checks")
        if n < 40:
            lines.append(f"  too little data to say anything (need hundreds; have {n}). Keep the average.\n")
            continue
        cut = set(slugs[:int(n * 0.7)])
        train = [s for s in smp if s[6] in cut]
        test = [s for s in smp if s[6] not in cut]
        w = fit_logit([[s[0], s[1]] for s in train], [s[4] for s in train])

        def ll(fn, rows):
            t = 0.0
            for s in rows:
                p = min(max(fn(s), EPS), 1 - EPS)
                t -= math.log(p if s[4] else 1 - p)
            return t / len(rows)
        res = {"model only": ll(lambda s: s[2], test), "market only": ll(lambda s: s[3], test),
               "average 50/50": ll(lambda s: 0.5 * s[2] + 0.5 * s[3], test),
               "fitted": ll(lambda s: tb._sigmoid(w[0] * s[0] + w[1] * s[1] + w[2]), test)}
        lines.append(f"  fitted on {len(cut)} windows: logit(chance) = {w[0]:.2f} x logit(model) + {w[1]:.2f} x logit(market) {w[2]:+.2f}")
        lines.append(f"  (model weight {w[0]:.2f}, market weight {w[1]:.2f}; they sum to {w[0] + w[1]:.2f}: above 1 = sharper than "
                     f"either input, below 1 = more cautious)")
        lines.append(f"  held-out log loss ({len(test)} checks, {n - len(cut)} windows):")
        for k, v in res.items():
            lines.append(f"    {k:14s} {v:.4f}")
        gain = res["average 50/50"] - res["fitted"]
        ok = len(cut) >= 300 and gain > 0.002
        if ok:
            wa = fit_logit([[s[0], s[1]] for s in smp], [s[4] for s in smp])
            updates[h] = {"blend_mode": "logit", "blend_a": round(wa[0], 3), "blend_b": round(wa[1], 3), "blend_c": round(wa[2], 3)}
            lines.append(f"  -> fitted blend beats the average by {gain:.4f}: saved (refit on all {n} windows)")
        else:
            why = f"only {len(cut)} training windows (need 300)" if len(cut) < 300 else f"gain {gain:+.4f} is too small (need > 0.002)"
            lines.append(f"  -> keeping the simple average: {why}")
        lines.append("")
    text = "\n".join(lines)
    say(text)
    if updates and not args.no_write:
        path = os.path.join(args.out, "params.json")
        merge_params(path, updates, "fit-blend")
        say(f"Wrote {path}.  Use it:  python makawan_bot.py --params {path}")


# --------------------------------------------------------------------------
def merge_params(path, updates, note):
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    for k, v in updates.items():
        data.setdefault(k, {}).update(v)
    data["_note"] = f"{note}; written {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')} by makawan_tune.py"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


PROPOSED, APPLIED = "params_proposed.json", "params.json"
SETTING_WORDS = {"vol_short_weight": "weight on the last few minutes' volatility", "vol_scale": "volatility multiplier",
                 "tail_w": "share of time treated as a big-move regime", "tail_s2": "size of big moves (x normal)"}


def write_proposal(out_dir, updates, note, say=print):
    """Save what the backtest would change WITHOUT changing anything. The user applies it with `apply`."""
    os.makedirs(out_dir, exist_ok=True)
    cur_path, prop_path = os.path.join(out_dir, APPLIED), os.path.join(out_dir, PROPOSED)
    current = {}
    if os.path.exists(cur_path):
        with open(cur_path, encoding="utf-8") as f:
            current = json.load(f)
    base = tb.Config()
    lines, changed = [f"Makawan tuner proposal ({note})", ""], {}
    for h, vals in updates.items():
        for k, v in vals.items():
            now_v = current.get(h, {}).get(k, getattr(base.view(h), k))
            if abs(float(now_v) - float(v)) > 1e-9:
                changed.setdefault(h, {})[k] = v
                lines.append(f"  {h}: {SETTING_WORDS.get(k, k)} ({k}): {now_v:g} -> {v:g}")
    if not changed:
        if os.path.exists(prop_path):
            os.replace(prop_path, prop_path + ".old")
        lines.append("No change proposed: nothing beat the current settings clearly on held-out data.")
        summary = "Tuner: no change proposed this week."
    else:
        with open(prop_path, "w", encoding="utf-8") as f:
            json.dump(dict(changed, _note=f"PROPOSAL, not in use. {note}; written "
                                          f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}"), f, indent=2)
        lines += ["", "Nothing has been changed. The bots keep their current settings until you decide.",
                  "This backtest scores how well the model predicts UP/DOWN on past index prices. It does not",
                  "use market prices, so a better score here is not proof of more profit.",
                  "Changing settings in the middle of a paper test makes before/after hard to compare.",
                  "", "To accept:   python3 makawan_tune.py apply      (then restart the bots)",
                  "To ignore:   do nothing. Next week's run replaces this proposal."]
        summary = (f"Tuner: proposes changes for {', '.join(changed)}. Nothing applied. "
                   f"See {os.path.join(out_dir, 'tune_proposal.txt')}")
    with open(os.path.join(out_dir, "tune_proposal.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    say("\n".join(lines))
    say("SUMMARY: " + summary)
    return changed


def run_apply(args, say=print):
    prop_path, cur_path = os.path.join(args.out, PROPOSED), os.path.join(args.out, APPLIED)
    if not os.path.exists(prop_path):
        sys.exit(f"There is no proposal waiting in {args.out}/. Run:  python3 makawan_tune.py backtest --propose")
    with open(prop_path, encoding="utf-8") as f:
        prop = json.load(f)
    updates = {k: v for k, v in prop.items() if not k.startswith("_")}
    tb.load_params(tb.Config(), prop_path)                     # refuses anything the bot would not accept
    if os.path.exists(cur_path):
        bak = os.path.join(args.out, "params-before-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
        shutil.copy(cur_path, bak)
        say(f"Kept the previous settings as {bak}")
    merge_params(cur_path, updates, "applied by you from " + (prop.get("_note") or "a proposal"))
    os.replace(prop_path, prop_path + ".applied")
    say(f"Applied to {cur_path}. Restart the bots to use it:  python3 run_both.py   "
        f"(it picks this file up by itself; `python3 makawan_tune.py revert` undoes it).")


def run_revert(args, say=print):
    cur_path = os.path.join(args.out, APPLIED)
    if not os.path.exists(cur_path):
        sys.exit(f"No tuned settings are in use in {args.out}/. The bots are on their built-in settings.")
    aside = os.path.join(args.out, "params-removed-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".json")
    os.replace(cur_path, aside)
    say(f"Tuned settings set aside as {aside}. Restart the bots and they use their built-in settings again.")


def main():
    ap = argparse.ArgumentParser(description="Measure and tune the Makawan model")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backtest", help="replay past windows on real BRTI history and tune volatility + tails")
    b.add_argument("--days", type=float, default=14)
    b.add_argument("--horizons", default="5m,15m,1h")
    b.add_argument("--out", default="makawan_out")
    b.add_argument("--refresh", action="store_true", help="ignore the local cache")
    b.add_argument("--no-write", action="store_true")
    b.add_argument("--propose", action="store_true",
                   help="only write a proposal (params_proposed.json + tune_proposal.txt); change nothing")
    b.add_argument("--pace", type=float, default=0.12, help="seconds between history downloads (raise it to go gently)")
    b.add_argument("--simulate", action="store_true", help="use the offline simulator instead of the API")
    b.add_argument("--end-time", type=int, default=0, help=argparse.SUPPRESS)
    f = sub.add_parser("fit-blend", help="learn how much to trust the market from the bot's own logs")
    f.add_argument("--horizons", default="5m,15m,1h")
    f.add_argument("--out", default="makawan_out")
    f.add_argument("--no-write", action="store_true")
    for name, text in (("apply", "start using the waiting proposal (your decision; restart the bots afterwards)"),
                       ("revert", "stop using tuned settings and go back to the built-in ones")):
        x = sub.add_parser(name, help=text)
        x.add_argument("--out", default=TUNE_DIR)
    a = ap.parse_args()
    if a.cmd == "fit-blend":
        return run_fit_blend(a)
    if a.cmd == "apply":
        return run_apply(a)
    if a.cmd == "revert":
        return run_revert(a)
    if a.propose and a.out == "makawan_out":
        a.out = TUNE_DIR                                        # proposals live apart from any one bot's logs
    cfg = tb.Config()
    if a.simulate:
        clock = tb.SimClock(time.time())
        api = tb.Api(tb.SimTransport(clock, history_s=int(a.days * 86400) + 7200), cfg)
        a.end_time = int(clock.time())
    else:
        api = tb.Api(tb.HttpTransport(cfg.gateway), cfg)
    run_backtest(a, api)


if __name__ == "__main__":
    main()
