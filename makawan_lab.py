#!/usr/bin/env python3
"""
makawan_lab.py - paper-trade several popular strategies side by side, on the same live data (PAPER ONLY)
==========================================================================================================
    python3 makawan_lab.py --open              # start the lab and open the leaderboard
    python3 makawan_lab.py --only cut,trend    # just some of them
    python3 makawan_lab.py --fresh             # archive the lab's history and start over
    python3 makawan_lab.py --simulate          # offline test with made-up prices

One program, one stream of market data, five strategies (fade, sniper, rising, favs, terry; the five others were
switched off on Nov 21 and still run with --only), each with its own paper account ($1,000, $20 a trade)
and its own record in makawan_out_lab_<name>/. Leaderboard: http://localhost:48217 (also open to your home network; --host 127.0.0.1 keeps it to this computer)
"terry" follows Terry Calls' first call instead of Makawan's maths; it needs TERRY_API_KEY in a .env file next to
this program. Only Terry's key is read, never your Polymarket key.

Run it INSTEAD of run_both.py, not alongside: it already includes today's rules as "baseline", and together they
would ask Polymarket for data too often. It never places a real order and never uses your key.

What makes these paper results more honest than before:
  * An order only fills if the price is still there one second later, as a real order has to. Missed = skipped.
  * The fee is the real one measured on your live trades (0.095 x price x (1 - price) a share).
  * The stake, the 20-point gap rule and one-trade-per-window are the same for every strategy, so the only
    difference between two rows is the idea being tested.

How to judge it: ignore the hit rate. Look at "edge" (how often it won minus what it needed to win at the prices
it paid) and net profit, and wait for a few hundred trades per strategy. A 2-point edge takes about 2,000 trades
to tell apart from luck. Decide the pass mark BEFORE looking.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import makawan_bot as tb
import makawan_report as rp
import makawan_web as tw

REQUIRES = "2026.11.13"
if getattr(tb, "__version__", None) != REQUIRES:
    sys.exit("{} and makawan_bot.py come from different downloads. Put ALL the files from the latest download in one "
             "folder.".format(os.path.basename(__file__)))

# P0 calibration from makawan_tune/params_proposed.json (15m only; 5m vol_scale=1.2 suppresses sniper entries)
CALIBRATED_15M = {"vol_short_weight": 0.0, "vol_scale": 1.1, "tail_w": 0.05}

# name, title, the idea in one line, why it might work / what the evidence says, settings changed from baseline
STRATEGIES = [
    ("baseline", "Fair value (today's rules)",
     "Buy when Makawan's chance is above the price by more than the fee.",
     "Your current 'any' strategy, now with honest fills. The yardstick for the others.",
     {}),
    ("base15m", "Calibrated 15m fair value",
     "15m only, any time: Makawan fair value with params_proposed 15m calibration and arrival checks.",
     "Drops 1h (worst Brier/losses) and 5m (fee drag), applies 15m calibration (vol_short_weight=0, vol_scale=1.1, "
     "tail_w=0.05), keeps trade_last_s empty, and re-checks entry filters on paper arrival.",
     {"no_trade_horizons": ("5m", "1h")}),
    ("cut", "Fair value + cut losers",
     "Baseline, but sell a losing position when buyers pay more than the maths says it is worth.",
     "On your Nov 10 logs this beat holding in every set of trades. Winners still ride to settlement.",
     {"cut_on_value": True}),
    ("cautious", "Trust the market more",
     "Weigh the market's price 65% (not 50%), never pay over 85c, cut losers.",
     "Your live trades showed the model is overconfident and the market is usually closer. Trades less often.",
     {"w_market": 0.65, "max_buy_cap": 0.85, "cut_on_value": True}),
    ("trend", "Trend following (momentum)",
     "Assume 30% of Bitcoin's move over the last 10 minutes carries on for the rest of the window.",
     "The classic strategy. Studies find some intraday momentum in Bitcoin, but it comes and goes.",
     {"drift_lookback_s": 600, "drift_beta": 0.30}),
    ("fade", "Mean reversion (fade the move)",
     "Assume 30% of Bitcoin's move over the last minute reverses.",
     "The other classic. Minute-by-minute Bitcoin moves tend to partly undo themselves; the effect is small.",
     {"drift_lookback_s": 60, "drift_beta": -0.30}),
    ("maker", "Maker first (resting bids)",
     "Same signal as baseline, but bid and wait instead of paying the ask. No fee.",
     "Studies say makers are the consistent winners. The catch: a waiting bid mostly fills when it is wrong.",
     {"exec_mode": "maker", "paper_delay_s": 0.0}),
    ("sniper", "Late favourite (sniping, no 1h)",
     "In the last 60 seconds of 5m and 15m, buy the side already priced 90-97c (arrival-checked).",
     "Enforces 90-97c, edge, gap, and >=50% fill ratio on arrival to eliminate <90c leaks and tiny partial fills.",
     {"trade_last_s": {"5m": 60, "15m": 60}, "min_buy_px": {"5m": 0.90, "15m": 0.90}, "max_buy_cap": 0.97}),
    ("rising", "Fair value + price moving your way",
     "Baseline, but only buys while the side's market price is up 3c or more over the last 30 seconds.",
     "The one lead from the Nov 15 late-entry backtest (1,499 markets): sides bought while rising did better than "
     "sides bought while falling in every favourite band. Not proven; that is what this tests.",
     {"rising_look_s": 30.0, "rising_min": 0.03}),
    ("favs", "Favourites only (80c+, no 1h)",
     "5m and 15m only: buys sides priced 80c+ with >=1c edge after fee, re-checked on arrival (paper research).",
     "Dropping 1h and enforcing 80c+ and >=1c edge at arrival turned Era 2+3 favs positive (+$269, PF 1.24, p=0.10); "
     "paper-research candidate until proven out-of-sample (pre-trade live IOC unproven).",
     {"min_buy_px": {"5m": 0.80, "15m": 0.80}}),
    ("terry", "Follow Terry's first call (no 1h, >=1c edge)",
     "Buy Terry Calls' FIRST call on 5m and 15m within 15 s of posting, at most 2c over his price, only with >=1c edge.",
     "Dropping 1h and enforcing ev_per_share >= 1c after fee turned Era 2+3 terry from -$254 to +$159 (p=0.37). "
     "Needs TERRY_API_KEY.",
     {"call_up": 0.999, "keep_up": 0.998, "keep_down": 0.002, "call_down": 0.001,
      "terry_max_age_s": 15.0, "terry_max_chase": 0.02}),
]
NAMES = [s[0] for s in STRATEGIES]
# Switched off by default after the Nov 24 strategy review (still defined, so '--only fade,rising' runs them again,
# and their records stay in makawan_out_lab_<name>/); a plain start runs only DEFAULT.
RETIRED = ("baseline", "cut", "cautious", "trend", "fade", "rising", "maker")
DEFAULT = [n for n in NAMES if n not in RETIRED]


class CachedTransport:
    """Lets several strategies share one set of requests.

    The lab goes round its strategies one after another, once a second. Each round starts with new_round(): the first
    strategy that asks a question fetches the answer, and every later strategy in the same round gets that answer
    (or that error) without asking again. Outside a round (tests, start-up), an answer is reused for `ttl` seconds.

    Why rounds and not just a timer: with ten strategies, a round can take longer than the timer, and then every
    strategy asked Polymarket itself (the lab slowed to one reading every ~9 s instead of 5). Worse, a slow or failing
    request was not remembered, so ten strategies each waited out the same 8-second timeout, which froze the lab for
    a minute or two (the watchdog's 'stuck' alerts). A 'get past the cache' request (used when a book says 'not
    found') is shared within the round too, instead of being sent once per strategy."""

    def __init__(self, inner, ttl=0.8):
        self.inner, self.ttl = inner, ttl
        self.lock = threading.Lock()
        self.cache = {}
        self.round, self.round_no = None, 0       # answers for the current round: key -> (value, error)
        self.asked = self.fetched = 0

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def new_round(self):
        with self.lock:
            self.round, self.round_no = {}, self.round_no + 1

    def end_round(self):
        with self.lock:
            self.round = None

    def get(self, path, params=None, headers=None):
        key = (path, tuple(sorted((k, str(v)) for k, v in (params or {}).items() if k != "_")))
        fresh = bool(headers) or "_" in (params or {})    # a 'get past the cache' request goes to Polymarket once a round
        rkey = key + (fresh,)
        now = time.monotonic()
        with self.lock:
            self.asked += 1
            got = self.round.get(rkey) if self.round is not None else None
            if got is None and not fresh:
                hit = self.cache.get(key)
                if hit and now - hit[0] < self.ttl:
                    got = (hit[1], hit[2])
        if got is not None:
            val, err = got
            if err is not None:
                raise err
            return val
        val = err = None
        keep = True
        try:
            val = self.inner.get(path, params, headers) if headers else self.inner.get(path, params)
        except tb.NotFound as e:                       # a 'not found' is an answer too
            err = e
        except Exception as e:                         # a failure: shared for this round only, so the others don't
            err = tb.ApiError(f"{type(e).__name__}: {str(e)[:120]}")   # wait out the same timeout; asked again next round
            err.__cause__ = e
            keep = False
        with self.lock:
            self.fetched += 1
            if self.round is not None:
                self.round[rkey] = (val, err)
            if keep:
                self.cache[key] = (time.monotonic(), val, err)
            if len(self.cache) > 400:
                old = time.monotonic() - 5
                for k in [k for k, v in self.cache.items() if v[0] < old]:
                    del self.cache[k]
        if err is not None:
            raise err
        return val


# ---- Terry Calls: follow his FIRST call (no locks) ----------------------------------------------------------------
TERRY_BASE = "https://terrycalls.com/api"


def find_terry_keys(env=None, paths=None):
    """Every Terry Calls key: TERRY_API_KEY, TERRY_API_KEY_2, TERRY_API_KEY_3 ... from the environment, else from the
    first .env file (next to the lab) that has any. Returns ([keys], where)."""
    env = os.environ if env is None else env

    def collect(pairs):
        found = []
        for name, v in sorted(pairs, key=lambda x: (len(x[0]), x[0])):     # TERRY_API_KEY first, then _2, _3 ...
            v = (v or "").strip().strip('"').strip("'")
            if (name == "TERRY_API_KEY" or name.startswith("TERRY_API_KEY_")) and v and v not in found:
                found.append(v)
        return found
    keys = collect(env.items())
    if keys:
        return keys, "environment"
    here = os.path.dirname(os.path.abspath(__file__))
    if paths is None:
        paths = [os.path.join(here, ".env")]
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                pairs = [ln.strip().split("=", 1) for ln in f if "=" in ln and not ln.lstrip().startswith("#")]
            keys = collect((k.strip().removeprefix("export ").strip(), v) for k, v in pairs)
            if keys:
                return keys, path
        except OSError:
            pass
    return [], ""


def find_terry_key(env=None, paths=None):
    """The first Terry Calls key and where it came from (kept for older callers)."""
    keys, where = find_terry_keys(env, paths)
    return (keys[0], where) if keys else ("", "")


# When Terry can make his first call (from get_rules, Nov 14 2026): (window length s, first call from s, never in last s)
CALL_WINDOWS = {"5m": (300, 90, 20), "15m": (900, 300, 60), "1h": (3600, 900, 120)}


class TerryFeed:
    """Polls Terry Calls' live markets (GET /v2/calls) in the background; one feed for the whole lab.
    It asks every `every` seconds (1 by default) while a first call could come on a traded market that has none
    yet, and every `slow` seconds otherwise. `budget` caps requests per key per UTC day (free plan: 20,000) by
    stretching the fast interval just enough to last the day; budget 0 = no cap (Unlimited plan). With several keys
    it takes turns, so the day's allowance is the budget times the number of keys."""

    def __init__(self, key, every=1.0, base=TERRY_BASE, fetch=None, clock=time.time, horizons=("5m", "15m", "1h"),
                 budget=19000, slow=15.0, state_path=None):
        self.keys = [k for k in ([key] if isinstance(key, str) else list(key or [])) if k]
        self.key = self.keys[0] if self.keys else ""
        self.base, self.clock = base.rstrip("/"), clock
        self.per_key = int(budget or 0)
        self.budget = self.per_key * max(1, len(self.keys))          # the whole day's allowance, all keys together
        self.horizons, self.state_path = set(horizons), state_path
        # free plan: 60 a minute per key, so each key stays just under 1 a second; Unlimited: 120 a minute
        n = max(1, len(self.keys))
        self.every = max((1.05 if self.per_key > 0 else 0.5) / n, float(every))
        self.slow = max(self.every, float(slow))
        self.fetch = fetch or self._http
        self.lock = threading.Lock()
        self.by_slug, self.by_end = {}, {}
        self.updated = 0.0
        self.polls = 0
        self.error = "" if key or fetch else "no TERRY_API_KEY found (put it in .env next to makawan_lab.py)"
        self.backoff_until = 0.0
        self.day, self.used, self.used_by, self.key_backoff, self._turn = None, 0, {}, {}, 0
        self.fast, self.interval, self.fast_share, self._last_plan = True, self.every, 0.7, None
        self._load_count()

    @staticmethod
    def _kid(key):
        """A short name for a key that does not give the key away (for the saved count and the status line)."""
        import hashlib
        return hashlib.sha256(key.encode()).hexdigest()[:8]

    def _pick(self, wall):
        """The next key in turn that is not resting after a 429 and has allowance left today; None if none."""
        n = len(self.keys)
        for i in range(n):
            k = self.keys[(self._turn + i) % n]
            if self.key_backoff.get(k, 0.0) > wall:
                continue
            if self.per_key > 0 and self.used_by.get(self._kid(k), 0) >= self.per_key:
                continue
            self._turn = (self._turn + i + 1) % n
            return k
        return None

    # -- daily request count (kept on disk so a restart does not forget what was already used today) --
    @staticmethod
    def _utc_day(ts):
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    def _load_count(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("day") == self._utc_day(time.time()):
                by = d.get("used_by")
                if not isinstance(by, dict):                         # saved by the one-key version
                    by = {self._kid(self.key): int(d.get("used", 0))} if self.key else {}
                self.day, self.used_by = d["day"], {k: int(v) for k, v in by.items()}
                self.used = int(d.get("used", sum(self.used_by.values())))
                self.fast_share = float(d.get("fast_share", self.fast_share))
        except (OSError, TypeError, ValueError, KeyError):
            pass

    def _save_count(self):
        if not self.state_path:
            return
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"day": self.day, "used": self.used, "used_by": self.used_by,
                           "fast_share": round(self.fast_share, 3)}, f)
        except OSError:
            pass

    def _count(self, wall, key=""):
        day = self._utc_day(wall)
        if day != self.day:
            self.day, self.used, self.used_by = day, 0, {}
        self.used += 1
        if key:
            kid = self._kid(key)
            self.used_by[kid] = self.used_by.get(kid, 0) + 1
        if self.used % 25 == 0:
            self._save_count()

    def _http(self, key=None):
        import urllib.error
        import urllib.request
        key = key or self.key
        req = urllib.request.Request(self.base + "/v2/calls",
                                     headers={"X-API-Key": key, "User-Agent": "makawan-lab/terry"})
        try:
            with urllib.request.urlopen(req, timeout=8, context=tb.ssl_context()) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self.key_backoff[key] = time.time() + float(e.headers.get("Retry-After") or 30)
            raise RuntimeError(f"Terry Calls HTTP {e.code}" + (f" (key {self._kid(key)})" if len(self.keys) > 1 else "")) from None

    def ingest(self, data):
        by_slug, by_end = {}, {}
        for m in (data or {}).get("markets", []):
            we = tb.parse_ts(m.get("window_end"))
            by_slug[m.get("market")] = m
            if we is not None:
                by_end[(m.get("horizon"), int(round(we)))] = m
        with self.lock:
            self.by_slug, self.by_end = by_slug, by_end
            self.updated = self.clock()
            self.error = "data marked stale by Terry Calls" if (data or {}).get("stale") else ""

    def poll_once(self, wall=None):
        wall = time.time() if wall is None else wall
        custom = self.fetch != self._http
        key = None if custom else self._pick(wall)
        if not custom and key is None:                               # every key resting or used up for today
            return
        self._count(wall, key or "")
        try:
            self.ingest(self.fetch() if custom else self._http(key))
            self.polls += 1
        except Exception as e:                                       # keep going; the strategy just waits
            with self.lock:
                self.error = f"{type(e).__name__}: {e}"[:160]

    def lookup(self, info):
        """(market dict or None, seconds since Terry's data was fetched)."""
        with self.lock:
            m = self.by_slug.get(info.slug) or self.by_end.get((info.horizon, int(round(info.we))))
            return m, (self.clock() - self.updated) if self.updated else float("inf")

    def call_due(self):
        """True while a traded market has no first call yet and is in the part of its window where one can come."""
        with self.lock:
            if not self.updated:
                return True
            markets, age = list(self.by_slug.values()), self.clock() - self.updated
        for m in markets:
            h = m.get("horizon")
            if h not in self.horizons or h not in CALL_WINDOWS or m.get("call"):
                continue
            length, start, last = CALL_WINDOWS[h]
            elapsed = length - (float(m.get("seconds_left") or 0) - age)
            if start - 5 <= elapsed <= length - last:
                return True
        return False

    def next_interval(self, wall):
        """Seconds to wait before the next request."""
        if self._last_plan is not None:                              # learn how much of the day needs fast checks
            dt = max(0.0, min(wall - self._last_plan, 60.0))
            a = min(1.0, dt / 3600.0)
            self.fast_share += a * ((1.0 if self.fast else 0.0) - self.fast_share)
        self._last_plan = wall
        self.fast = self.call_due()
        if not self.fast:
            self.interval = self.slow
        elif self.budget <= 0:
            self.interval = self.every
        else:
            if self._utc_day(wall) != self.day:
                self.day, self.used, self.used_by = self._utc_day(wall), 0, {}
            secs_left = 86400.0 - (wall % 86400.0)
            remaining = self.budget - self.used
            f = min(0.95, max(0.2, self.fast_share))
            fast_budget = remaining - (1.0 - f) * secs_left / self.slow
            if remaining <= 50 or fast_budget <= 0:
                self.interval = max(self.slow, secs_left / max(1.0, remaining))
            else:
                self.interval = max(self.every, f * secs_left / fast_budget)
        return self.interval

    def status(self):
        if not self.key and self.fetch == self._http:
            return "Terry feed off: " + self.error
        if self.error:
            return f"Terry feed problem: {self.error}"
        if not self.updated:
            return "Terry feed waiting for first answer"
        pace = (f"checking every {self.interval:.1f}s (a first call is due)" if self.fast else
                f"every {self.slow:.0f}s (no first call due)")
        cap = f" of {self.budget:,}" if self.budget > 0 else ""
        keys = f" ({len(self.keys)} keys)" if len(self.keys) > 1 else ""
        return (f"Terry feed live ({self.clock() - self.updated:.0f}s ago), {pace}; "
                f"{self.used:,}{cap} requests today{keys}")

    def start(self):
        if not self.key:
            return self

        def loop():
            while True:
                self.poll_once()
                time.sleep(self.next_interval(time.time()))
        threading.Thread(target=loop, daemon=True, name="terry-feed").start()
        return self


class TerryEngine(tb.Engine):
    """Same paper account, fills, fee and settlement as every other strategy; only the reason to buy differs:
    Terry Calls' first call for the window instead of Makawan's maths. Makawan's own calls are switched off
    (unreachable thresholds) so the page shows Terry's first call."""

    terry = None                                                     # TerryFeed, set by build()

    def _eval(self, w, now, book):
        had_fc = w.first_call is not None
        super()._eval(w, now, book)
        if not had_fc and w.first_call is not None:                  # never let Makawan's CALL overwrite Terry's first_call
            w.first_call = w.first_call_p = w.first_call_left = None
        cfg = self.cfg.view(w.info.horizon)
        if cfg.trade and w.book is not None and not (w.position or w.paper_order or w.entered or w.order
                                                     or w.live_order):
            self._maybe_buy(w, now)

    def _paper_arrival_reject(self, w, now, cfg, o, sb, avg, got):
        """Re-validate Terry's call, edge, gap, price bounds, and fill ratio on delayed paper arrival."""
        side = o["side"]
        if w.tau < cfg.min_secs_left:
            return f"only {w.tau:.0f}s left"
        if cfg.paper_min_fill_frac > 0 and got < cfg.paper_min_fill_frac * o["qty"] - 1e-9:
            return f"partial fill {got:.2f}/{o['qty']:.2f} (< {cfg.paper_min_fill_frac:.0%})"
        best_ask = sb.asks[0][0]
        floor_px = cfg.min_buy_px.get(w.info.horizon, 0.0)
        if best_ask < floor_px - 1e-9 or avg < floor_px - 1e-9:
            return f"price {avg:.3f} (ask {best_ask:.3f}) under min {floor_px:.2f}"
        if cfg.max_buy_cap and (best_ask > cfg.max_buy_cap + 1e-9 or avg > cfg.max_buy_cap + 1e-9):
            return f"price {avg:.3f} over cap {cfg.max_buy_cap:.2f}"
        if avg >= 0.99 - 1e-9:
            return f"price {avg:.3f} >= 0.99"
        feed = self.terry
        if feed is None:
            return "no Terry feed"
        m, age = feed.lookup(w.info)
        if m is None or age > 30:
            return "Terry data stale or missing"
        call = m.get("call")
        if not call or str(call.get("side", "")).upper() != side:
            return "Terry call missing or changed"
        chance_now = (m.get("chance_now") or {}).get(side.lower())
        p_side = float(chance_now if chance_now is not None else (call.get("chance") or 0))
        edge = tb.ev_per_share(p_side, avg, cfg.fee_theta)
        if edge < cfg.min_edge - 1e-9:
            return f"Terry edge {edge:+.3f} < {cfg.min_edge:.3f} at {avg:.3f}"
        mkt = tb.side_chance(w.p_mkt, side) if w.p_mkt is not None else best_ask
        if p_side - mkt > cfg.max_model_gap + 1e-9:
            return f"Terry gap {(p_side - mkt) * 100:.0f} pts > {cfg.max_model_gap * 100:.0f}"
        return ""

    def _maybe_buy(self, w, now):
        cfg = self.cfg.view(w.info.horizon)
        if w.position or w.paper_order or w.entered:
            return
        if self.halted or self.day_paused == tb.day_key(now):
            block = self.guard_blocks(now, 0.0)
            if block:
                w.value_note = block
                return
        if w.info.horizon in cfg.no_trade_horizons:
            w.value_note = "tracking only: no trades on this window length"
            return
        feed = self.terry
        if feed is None:
            w.value_note = "no Terry feed"
            return
        m, age = feed.lookup(w.info)
        if m is None or age > 30:
            w.value_note = (f"no Terry data ({feed.error})" if feed.error else
                            "Terry has nothing for this window yet" if m is None else f"Terry data {age:.0f}s old")
            return
        call = m.get("call")
        if not call:
            w.value_note = "waiting for Terry's first call"
            return
        side = str(call.get("side", "")).upper()
        at = tb.parse_ts(call.get("at"))
        if side not in ("UP", "DOWN") or at is None:
            w.value_note = "Terry's call could not be read"
            return
        if w.first_call is None:                                     # windows.csv scores Terry's first call
            w.first_call, w.first_call_p, w.first_call_left = side, float(call.get("chance") or 0), w.info.we - at
        terry_px = float(call.get("price") or 0)
        call_age = now - at
        max_age = float(getattr(self.cfg, "terry_max_age_s", 15.0))
        if call_age > max_age:
            w.entered = True                                         # too late to follow: skip this window
            w.value_note = f"skipped: Terry's first call ({side} at {terry_px:.0%}) was {call_age:.0f}s old"
            self.log(now, w, f"TERRY first call {side} {terry_px:.0%} seen {call_age:.0f}s after it posted; skipped")
            return
        sb = self._side(w, side)
        ask = sb.ask
        if ask is None:
            w.value_note = "no sellers"
            return
        floor_px = cfg.min_buy_px.get(w.info.horizon, 0.0)
        if ask < floor_px:
            w.value_note = f"skip: side under {floor_px:.0%}"
            return
        chase_ceiling = min(terry_px + float(getattr(self.cfg, "terry_max_chase", 0.02)), cfg.max_buy_cap or 0.99, 0.99)
        if ask > chase_ceiling + 1e-9:
            w.value_note = f"price ran: ask {ask:.0%}, Terry called at {terry_px:.0%}"
            return
        chance_now = (m.get("chance_now") or {}).get(side.lower())
        p_side = float(chance_now if chance_now is not None else (call.get("chance") or 0))
        edge_ceiling = tb.max_buy_price(p_side, cfg.fee_theta, cfg.min_edge)
        edge = tb.ev_per_share(p_side, ask, cfg.fee_theta)
        if edge < cfg.min_edge - 1e-9 or ask > edge_ceiling + 1e-9 or ask >= 0.99:
            w.value_note = f"skip: Terry edge {edge * 100:+.1f}c < {cfg.min_edge * 100:g}c at {ask:.0%}"
            return
        ceiling = min(chase_ceiling, edge_ceiling)
        mkt = tb.side_chance(w.p_mkt, side) if w.p_mkt is not None else ask
        if p_side - mkt > cfg.max_model_gap + 1e-9:
            w.value_note = f"skip: Terry {(p_side - mkt) * 100:.0f} pts above the market"
            return
        if w.tau < cfg.min_secs_left:
            return
        stake = self._stake(cfg, p_side, ask, cfg.fee_theta)
        block = self.guard_blocks(now, stake)
        if block:
            w.value_note = block
            return
        self.log(now, w, f"TERRY first call {side} {float(call.get('chance') or 0):.0%} at {terry_px:.0%}, "
                         f"{call_age:.0f}s ago; ask {ask:.0%} (edge {edge * 100:+.1f}c)")
        self._paper_send(w, now, cfg, side, ask, ceiling, stake)


class RisingEngine(tb.Engine):
    """Baseline's rules plus one more check before buying: the market price of the side it wants must have gone UP
    by cfg.rising_min over the last cfg.rising_look_s seconds. Everything else (maths, fills, fee, one order per
    window) is the shared engine, so the only difference from baseline is this check."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.trail = {}                                              # slug -> [(time, market chance of UP), ...]

    def _eval(self, w, now, book):
        mid = book.mid() if book is not None else None
        if mid is not None:
            t = self.trail.setdefault(w.info.slug, [])
            if not t or now - t[-1][0] >= 0.9:
                t.append((now, mid))
            keep = now - float(getattr(self.cfg, "rising_look_s", 30.0)) - 20
            while len(t) > 2 and t[0][0] < keep:
                t.pop(0)
            if len(self.trail) > 40:                                 # finished windows: forget them
                for slug in [k for k, v in self.trail.items() if v[-1][0] < now - 7200]:
                    del self.trail[slug]
        super()._eval(w, now, book)

    def side_move(self, w, now, side):
        """How far the side's market price moved over the look-back (None = not enough history to say)."""
        t = self.trail.get(w.info.slug) or []
        look = float(getattr(self.cfg, "rising_look_s", 30.0))
        if not t or now - t[-1][0] > 3:
            return None
        then = [x for x in t if look - 1e-9 <= now - x[0] <= look + 12]
        if not then:
            return None
        ch = t[-1][1] - then[-1][1]                                  # against the reading closest to `look` s ago
        return ch if side == "UP" else -ch

    def _maybe_buy(self, w, now):
        if not (w.position or w.paper_order or w.entered):
            need = float(getattr(self.cfg, "rising_min", 0.03))
            ch = self.side_move(w, now, w.call)
            if ch is None:
                w.value_note = "waiting: not enough price history yet to see which way it is moving"
                return
            if ch < need - 1e-9:
                w.value_note = (f"waiting: {w.call} price is {ch * 100:+.0f}c over the last "
                                f"{getattr(self.cfg, 'rising_look_s', 30):g}s; buys only at +{need * 100:g}c or more")
                return
        super()._maybe_buy(w, now)


def rising_settings(cfg):
    return (tb.describe_model(cfg) + f" | only buys while the side's price is up {getattr(cfg, 'rising_min', 0.03) * 100:g}c+ "
            f"over the last {getattr(cfg, 'rising_look_s', 30):g}s")


ENGINES = {"terry": TerryEngine, "rising": RisingEngine}           # every other strategy runs the shared tb.Engine


def terry_settings(cfg):
    hs = ", ".join(h for h in cfg.horizons if h not in cfg.no_trade_horizons)
    return (f"Terry Calls first call only (no locks) | trades: {hs}, any time | buy within "
            f"{getattr(cfg, 'terry_max_age_s', 15):g}s of the call, at most {getattr(cfg, 'terry_max_chase', 0.02) * 100:g}c "
            f"over his price | min edge {cfg.min_edge * 100:g}c after fee | skip if Terry {cfg.max_model_gap * 100:.0f}+ pts "
            f"above market | stake ${cfg.flat_stake:g} flat | stop at -{cfg.stop_loss_frac:.0%} | honest paper fills "
            f"({cfg.paper_delay_s:g}s delay, min fill {cfg.paper_min_fill_frac:.0%}) | held to settlement")


class Bot:
    def __init__(self, name, title, idea, why, cfg, eng):
        self.name, self.title, self.idea, self.why, self.cfg, self.eng = name, title, idea, why, cfg, eng
        self.latest = b"{}"
        self.row = {}


def make_config(name, overrides, a):
    cfg = tb.Config()
    cfg.bankroll, cfg.flat_stake = a.bankroll, a.stake
    cfg.trade_last_s = {}                              # every strategy may trade at any time unless it says otherwise
    cfg.no_trade_horizons = ("1h",)                    # 1h disabled by default (bad Brier and heavy Era 2+3 losses)
    cfg.paper_delay_s = a.delay
    cfg.live_slip = 0.0                                # no +1c chase slip
    cfg.paper_min_fill_frac = 0.50                     # reject tiny partial paper fills (<50% of requested qty)
    cfg.cut_on_value = False                           # guardrail: delayed cuts underperform holding
    cfg.overrides["15m"] = dict(CALIBRATED_15M)        # params_proposed.json 15m vol/tail calibration
    cfg.strategy = name
    cfg.out_dir = f"makawan_out_lab_{name}"
    cfg.tick_log_s = max(getattr(cfg, "tick_log_s", 5) or 5, 5)
    for k, v in overrides.items():
        if k == "overrides":
            for hk, hv in v.items():
                cfg.overrides.setdefault(hk, {}).update(copy.deepcopy(hv))
        else:
            setattr(cfg, k, copy.deepcopy(v))
    tb.validate_config(cfg)
    return cfg


def score(bot, now):
    """One leaderboard row, from the strategy's own record."""
    b = rp.load_bot(bot.cfg.out_dir, 0.0, now + 60)
    tr = b["trades"]
    s = rp.tally(tr)
    n = s["n"]
    eng = bot.eng
    row = {"name": bot.name, "title": bot.title, "idea": bot.idea, "why": bot.why, "trades": n, "won": s["won"],
           "net": round(s["net"], 2), "gross": round(s["gross"], 2), "fees": round(s["fees"], 2),
           "staked": round(s["staked"], 2), "equity": round(eng.equity(), 2), "halted": eng.halted,
           "luck": rp.luck(s),
           "settings": (terry_settings(bot.cfg) if isinstance(eng, TerryEngine) else
                        rising_settings(bot.cfg) if isinstance(eng, RisingEngine) else tb.describe_model(bot.cfg)),
           "open": sum(1 for w in list(eng.active.values()) + list(eng.pending) if w.position)}
    if n:
        need = sum((t["cost"] / t["shares"]) for t in tr if t["shares"]) / n      # price + fee per share
        wins = sum(t["net"] for t in tr if t["net"] > 0)
        losses = -sum(t["net"] for t in tr if t["net"] < 0)
        row.update(win_rate=s["won"] / n, avg_px=sum(t["px"] for t in tr) / n, need=need,
                   edge=s["won"] / n - need, pf=(wins / losses) if losses > 0 else None,
                   per_trade=s["net"] / n, ret=s["net"] / s["staked"] if s["staked"] else 0.0)
    if bot.cfg.exec_mode == "maker":
        st = eng.stats
        row["tries"] = sum(x.orders for x in st.values())
        row["fills"] = sum(x.fills for x in st.values())
    else:
        row["tries"], row["fills"] = eng.paper_tries, eng.paper_fills
    return row


class Lab:
    bots = []
    by_name = {}
    board = b"{}"
    started = time.time()
    mode = "live"
    feed = None
    terry = None
    round_s = 0.0          # how long the last pass over every strategy took (should be well under 1 s)
    slow_rounds = 0        # passes that took over 2 s since the lab started


LAB_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Makawan strategy lab</title><style>
:root{--bg:#f6f3ea;--card:#fffdf7;--ink:#1d2a22;--mute:#6a7068;--line:#e3ddcc;--up:#1f7a4d;--down:#b4473a;--chip:#efeadb;--warnbg:#fbf0cf}
@media (prefers-color-scheme:dark){:root{--bg:#111512;--card:#192019;--ink:#e7ece4;--mute:#97a297;--line:#2a332b;--up:#4cc38a;--down:#e6806f;--chip:#232c24;--warnbg:#3a3014}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1380px;margin:0 auto;padding:18px 16px 40px}h1{font:600 22px Georgia,serif;margin:0 0 2px}
.sub{color:var(--mute);font-size:13px}.mono{font-family:ui-monospace,Menlo,Consolas,monospace}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:6px 14px;margin-top:14px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:8px 10px 8px 0;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left;white-space:normal;min-width:230px}
th{color:var(--mute);font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
tr:last-child td{border-bottom:0}.up{color:var(--up)}.down{color:var(--down)}.mute{color:var(--mute)}
a{color:inherit}.chip{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--chip);font-size:11.5px}
.note{background:var(--warnbg);border-radius:10px;padding:10px 14px;margin-top:14px;font-size:13.5px}
.bar{height:6px;border-radius:3px;background:var(--line);min-width:80px}.bar i{display:block;height:6px;border-radius:3px;background:var(--mute)}
.idea{font-size:12.5px;color:var(--mute);margin-top:2px}
</style></head><body><div class="wrap">
<h1>Makawan strategy lab <span class="chip">PAPER ONLY</span></h1>
<div class="sub" id="top">loading...</div>
<div class="card"><table id="t"></table></div>
<div class="note"><b>How to read this.</b> Ignore how often a strategy wins on its own. <b>Per $1 staked</b> is the
profit or loss for every dollar put into trades, fees included: above zero makes money, below zero loses it.
<b>Needed</b> is how often it had to win at the prices it paid just to break even; compare it with <b>Won</b>
(strategies that cut losers can make money while winning less often than "needed").
An order here fills only if the price is still there a second later, so <b>Orders filled</b> shows how many would really have traded.
Small numbers of trades mean little: a 2-cent edge takes about 2,000 trades to tell apart from luck. Decide what counts as a pass before you look.</div>
<div class="card"><table id="why"></table></div>
<div class="sub" style="margin-top:14px">Each strategy keeps its own record in <span class="mono">makawan_out_lab_&lt;name&gt;/</span>.
<span class="mono">python3 makawan_report.py --open</span> breaks every one down by price paid, time left and more. Nothing here places a real order.</div>
</div><script>
const $=q=>document.querySelector(q);
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const money=x=>(x<0?'-':'+')+'$'+Math.abs(x).toFixed(2);
const cls=x=>x>0?'up':x<0?'down':'mute';
const pct=(x,d=0)=>x==null?'-':(x*100).toFixed(d)+'%';
async function tick(){
  let S; try{S=await (await fetch('/api/lab',{cache:'no-store'})).json();}catch(e){return;}
  $('#top').textContent=S.status;
  const rows=S.rows||[];
  $('#t').innerHTML='<tr><th>Strategy</th><th>Net P&amp;L</th><th>Per $1 staked</th><th>Trades</th><th>Orders filled</th><th>Won</th><th>Needed</th><th>Avg price</th><th>Fees</th><th>Profit factor</th><th>Is it luck?</th><th>Progress to 300</th></tr>'+
  rows.map(r=>{
    const fill=r.tries?(r.fills+'/'+r.tries+' ('+pct(r.fills/r.tries)+')'):'-';
    const prog=Math.min(100,Math.round(r.trades/300*100));
    return '<tr><td><a href="/bot/'+r.name+'/"><b>'+esc(r.title)+'</b></a>'+(r.halted?' <span class="chip down">halted</span>':'')+
      '<div class="idea">'+esc(r.idea)+'</div></td>'+
      '<td class="'+cls(r.net)+'"><b>'+money(r.net)+'</b></td>'+
      '<td class="'+(r.trades?cls(r.ret):'mute')+'">'+(r.trades?((r.ret>=0?'+':'')+(r.ret*100).toFixed(1)+'c'):'-')+'</td>'+
      '<td>'+r.trades+(r.open?' <span class="mute">+'+r.open+' open</span>':'')+'</td><td>'+fill+'</td>'+
      '<td>'+(r.trades?pct(r.win_rate):'-')+'</td><td>'+(r.trades?pct(r.need):'-')+'</td>'+
      '<td>'+(r.trades?(r.avg_px*100).toFixed(0)+'c':'-')+'</td><td>$'+r.fees.toFixed(2)+'</td>'+
      '<td>'+(r.pf==null?'-':r.pf.toFixed(2))+'</td><td class="mute" style="white-space:normal;min-width:150px;text-align:left">'+esc(r.luck)+'</td>'+
      '<td><div class="bar"><i style="width:'+prog+'%"></i></div></td></tr>';}).join('');
  $('#why').innerHTML='<tr><th>Strategy</th><th style="text-align:left">Why it might work, and what the evidence says</th><th style="text-align:left">Settings</th></tr>'+
  rows.map(r=>'<tr><td><b>'+esc(r.title)+'</b></td><td style="text-align:left;white-space:normal">'+esc(r.why)+'</td><td class="mono mute" style="text-align:left;white-space:normal;font-size:12px">'+esc(r.settings)+'</td></tr>').join('');
}
tick(); setInterval(tick,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(LAB_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/lab":
            return self._send(Lab.board, "application/json")
        if path == "/api/state" and Lab.bots:              # so the watchdog can watch the lab as one bot
            return self._send(Lab.bots[0].latest, "application/json")
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "bot" and parts[1] in Lab.by_name:
            bot, rest = Lab.by_name[parts[1]], "/".join(parts[2:])
            if rest in ("", "index.html"):
                if not path.endswith("/") and rest == "":
                    self.send_response(301)
                    self.send_header("Location", path + "/")
                    self.end_headers()
                    return
                page = (tw.PAGE.replace("fetch('/api/state'", f"fetch('/bot/{bot.name}/api/state'")
                        .replace('href="/events.csv"', f'href="/bot/{bot.name}/events.csv"')
                        .replace('href="/windows.csv"', f'href="/bot/{bot.name}/windows.csv"')
                        .replace('<div class="wrap">', '<div class="wrap"><div class="sub" style="margin-bottom:8px">'
                                 f'<a href="/" style="color:inherit">&larr; strategy lab</a> &middot; <b>{bot.title}</b> '
                                 f'&middot; paper</div>', 1))
                return self._send(page.encode("utf-8"), "text/html; charset=utf-8")
            if rest == "api/state":
                return self._send(bot.latest, "application/json")
            if rest in ("events.csv", "windows.csv"):
                f = os.path.join(bot.cfg.out_dir, rest)
                if os.path.exists(f):
                    with open(f, "rb") as fh:
                        return self._send(fh.read(), "text/csv; charset=utf-8")
        self._send(b"not found", "text/plain", 404)


def refresh_board(clock):
    rows = []
    for b in Lab.bots:
        try:
            b.row = score(b, clock.time())
        except Exception as e:                              # one unreadable record must not blank the page
            b.row = dict(b.row or {"name": b.name, "title": b.title, "idea": b.idea, "why": b.why, "trades": 0,
                                   "net": 0.0, "fees": 0.0}, luck=f"could not read its record ({type(e).__name__})")
        rows.append(b.row)
    rows.sort(key=lambda r: (-(r.get("net") or 0.0), r["name"]))
    up = time.time() - Lab.started
    feed = Lab.feed
    saved = f"; {feed.asked - feed.fetched:,} of {feed.asked:,} data requests shared" if feed is not None else ""
    first = Lab.bots[0].eng if Lab.bots else None
    err = f" - DATA PROBLEM: {first.last_error}" if first is not None and first.last_error else ""
    terry = f"; {Lab.terry.status()}" if Lab.terry is not None else ""
    status = (f"{len(rows)} strategies on the same {'simulated' if Lab.mode != 'live' else 'live'} data, "
              f"running {int(up // 3600)} h {int(up % 3600 // 60):02d} min; one pass over all strategies takes "
              f"{Lab.round_s:.1f}s ({Lab.slow_rounds} slow passes){saved}{terry}{err}")
    Lab.board = json.dumps({"rows": rows, "status": status}, default=str).encode()


def lan_ip():
    """This computer's address on the home network (e.g. 192.168.1.25), or '' if it cannot tell. Sends nothing:
    'connecting' a UDP socket only picks the network card that would be used."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.168.0.1", 9))
            ip = sk.getsockname()[0]
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""


def build(a, say=print):
    """Create the shared data feed and one engine per strategy. Returns (bots, clock)."""
    chosen = [n.strip() for n in a.only.split(",") if n.strip()] if a.only else DEFAULT
    bad = [n for n in chosen if n not in NAMES]
    if bad:
        sys.exit(f"Unknown strategy: {', '.join(bad)}. Choose from: {', '.join(NAMES)}")
    base = tb.Config()
    if a.simulate:
        start, hist = tb.sim_start(f"makawan_out_lab_{chosen[0]}")   # carry the fake clock on from the last run
        clock = tb.SimClock(start)
        feed = tb.SimTransport(clock, seed=a.seed, history_s=hist)
        Lab.mode, Lab.feed = "simulation", None
    else:
        clock = tb.RealClock()
        feed = CachedTransport(tb.HttpTransport(base.gateway))
        Lab.mode, Lab.feed = "live", feed
    api = tb.Api(feed, base)                               # ONE source of market data for every strategy
    spot = None if a.simulate else tb.make_spot(a.spot, say)
    terry = getattr(a, "terry_feed", None)
    if terry is None and "terry" in chosen and not a.simulate:
        keys, where = find_terry_keys()
        include_1h = getattr(a, "terry_include_1h", False) and not getattr(a, "terry_skip_1h", False)
        hs = ("5m", "15m", "1h") if include_1h else ("5m", "15m")
        terry = TerryFeed(keys, getattr(a, "terry_every", 1.0), horizons=hs, budget=getattr(a, "terry_budget", 19000),
                          state_path=os.path.join("makawan_out_lab_terry", "terry_feed.json")).start()
        say(f"terry: {len(keys)} Terry Calls key{'s' if len(keys) > 1 else ''} from {where}"
            + (f", {terry.budget:,} requests a day between them" if terry.per_key > 0 else "")
            if keys else f"terry: {terry.error}; that strategy will not trade")
    Lab.terry = terry
    bots = []
    for name, title, idea, why, over in STRATEGIES:
        if name not in chosen:
            continue
        cfg = make_config(name, over, a)
        if name == "terry":
            if getattr(a, "terry_include_1h", False) and not getattr(a, "terry_skip_1h", False):
                cfg.no_trade_horizons = ()
            else:
                cfg.no_trade_horizons = ("1h",)
        if a.fresh:
            moved = tb.archive_outputs(cfg.out_dir)
            if moved:
                say(f"{name}: earlier history moved to {moved}")
        rec = tb.Recorder(cfg.out_dir)
        tb.write_state(cfg.out_dir, cfg.bankroll, strategy=name, lab=True)
        Eng = ENGINES.get(name, tb.Engine)
        eng = Eng(cfg, api, clock, rec, parallel=not a.simulate, spot=spot)
        if name == "terry":
            eng.terry = terry
        try:
            eng.restore()
        except Exception as e:
            eng.warn(f"Could not read the earlier history ({type(e).__name__}: {e}); continuing without it.")
        bots.append(Bot(name, title, idea, why, cfg, eng))
    return bots, clock


def run_loop(bots, clock, a, stop=None, max_ticks=None):
    for b in bots:
        try:
            b.eng.bootstrap()
        except Exception as e:
            b.eng.last_error = f"{type(e).__name__}: {e}"
    n = 0
    feed = bots[0].eng.api.t if bots else None
    rounds = callable(getattr(feed, "new_round", None))
    while not (stop and stop.is_set()):
        t0 = time.monotonic()
        if rounds:
            feed.new_round()                                # every strategy this pass shares one set of answers
        for b in bots:
            try:
                b.eng.tick()
                b.latest = json.dumps(tw.snapshot(b.eng, clock, Lab.mode), default=str).encode()
            except Exception as e:                          # one strategy failing must not stop the rest
                b.eng.last_error = f"{type(e).__name__}: {e}"
        if rounds:
            feed.end_round()
        Lab.round_s = time.monotonic() - t0
        Lab.slow_rounds = getattr(Lab, "slow_rounds", 0) + (Lab.round_s > 2.0)
        if n % (10 if not a.simulate else 60) == 0:
            refresh_board(clock)
        n += 1
        if max_ticks and n >= max_ticks:
            refresh_board(clock)
            return
        if a.simulate:
            clock.advance(1.0)
            if a.speed > 0:
                time.sleep(1.0 / a.speed)
        else:
            time.sleep(max(0.05, math.floor(time.time()) + 1.15 - time.time()))


def main():
    ap = argparse.ArgumentParser(description="Paper-trade several strategies side by side on the same live data")
    ap.add_argument("--only", default="", help="comma-separated strategy names (default: " + ", ".join(DEFAULT)
                    + "). Switched off but still available: " + ", ".join(RETIRED))
    ap.add_argument("--port", type=int, default=48217, help="web page port (default 48217)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="0.0.0.0 (default) = other computers on your home network can open the page; "
                         "127.0.0.1 = only this computer")
    ap.add_argument("--bankroll", type=float, default=1000.0, help="paper money per strategy (default 1000)")
    ap.add_argument("--stake", type=float, default=20.0, help="paper dollars per trade (default 20)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds before a paper order 'arrives' and must still find its price (default 1)")
    ap.add_argument("--spot", choices=("off", "watch", "use"), default="use",
                    help="Coinbase live price: use = the maths runs on the live Coinbase estimate (default since Nov 21; "
                         "falls back to the index print when Coinbase is late or jumps), watch = index print, Coinbase logged")
    ap.add_argument("--terry-every", type=float, default=1.0,
                    help="'terry': seconds between Terry Calls requests while a first call is due (default 1)")
    ap.add_argument("--terry-budget", type=int, default=19000,
                    help="'terry': most Terry requests per key per UTC day (default 19000, under the free plan's 20,000); "
                         "the 1 s pace stretches just enough to last the day. 0 = no cap (Unlimited plan)")
    ap.add_argument("--terry-skip-1h", action="store_true",
                    help="'terry': do not trade or watch for 1h calls (now the default)")
    ap.add_argument("--terry-include-1h", action="store_true",
                    help="'terry': opt back into trading and watching 1h calls")
    ap.add_argument("--fresh", action="store_true", help="archive the lab's history and start over")
    ap.add_argument("--simulate", action="store_true", help="made-up prices, offline (for testing)")
    ap.add_argument("--speed", type=float, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--seed", type=int, default=11, help=argparse.SUPPRESS)
    ap.add_argument("--allow-sleep", action="store_true")
    ap.add_argument("--no-watchdog", action="store_true",
                    help="do not start the watchdog (phone alerts, the 7:00 daily report) alongside the lab")
    ap.add_argument("--open", action="store_true", help="open the leaderboard in your browser")
    ap.add_argument("--tz", default="America/New_York")
    a = ap.parse_args()
    if not tb.set_display_tz(a.tz):
        print(f"Unknown time zone {a.tz}; using UTC.")
    if a.stake <= 0 or a.bankroll <= 0:
        sys.exit("--stake and --bankroll must be more than 0.")
    bots, clock = build(a)
    for b in bots:
        if a.spot == "use":
            b.cfg.spot_use = True
        for note in b.eng.notes:
            print(f"{b.name}: {note}")
        if b.eng.halted:
            print(f"{b.name}: trading halted from an earlier run ({b.eng.halted}). --fresh starts it again.")
    Lab.bots, Lab.by_name, Lab.started = bots, {b.name: b for b in bots}, time.time()
    if not a.simulate and not a.allow_sleep:
        tb.keep_awake()
    threading.Thread(target=run_loop, args=(bots, clock, a), daemon=True).start()
    dog = None
    if not a.simulate and not a.no_watchdog:                # separate program: if it stops, the lab keeps going
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            dog = subprocess.Popen([sys.executable, os.path.join(here, "makawan_watchdog.py"), "--bots",
                                    f"lab={a.port},LIVE?=8770", "--tz", a.tz],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            threading.Thread(target=lambda: [print("watchdog | " + ln.rstrip(), flush=True) for ln in dog.stdout],
                             daemon=True).start()
        except OSError as e:
            print(f"Could not start the watchdog ({e}); the lab runs without it.")
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    url = f"http://{'localhost' if a.host in ('127.0.0.1', '0.0.0.0', '') else a.host}:{a.port}"
    print(f"Strategy lab: {url}   ({len(bots)} strategies, PAPER ONLY, times in {tb.DISPLAY_TZ_NAME})   Ctrl+C to stop")
    if a.host in ("0.0.0.0", ""):
        ip = lan_ip()
        print(f"  From another computer on your network: http://{ip or 'THIS-COMPUTER-IP'}:{a.port}"
              + ("" if ip else "   (ipconfig shows the IP)"))
    print("  " + ", ".join(b.name for b in bots))
    print("  Run this instead of run_both.py, not alongside it.")
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nLab stopped. Each strategy's record is kept and picked up next time.")
    finally:
        if dog is not None and dog.poll() is None:
            dog.terminate()


if __name__ == "__main__":
    main()

