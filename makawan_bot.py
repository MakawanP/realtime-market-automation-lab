#!/usr/bin/env python3
"""
makawan_bot.py  -  Bitcoin 5m / 15m / 1h tracker for Polymarket US
================================================================
Makawan: re-implements the thinking shown in the "How Terry thinks" video:

  1. Watch  : official BTC index (BRTI), the "price to beat", the clock,
              how jumpy BTC is (dollars per typical minute), the order book.
  2. Chance : turn those into a probability that BTC finishes above the line.
  3. Blend  : average that with the market's own price.
  4. Call   : post UP / DOWN only when the chance is strong enough, and don't
              flip-flop (57% in, 53% keep / 47% keep, 43% in).
  5. Value  : being right isn't enough - the price (plus fee) must be good.
  6. Size   : a quarter of full Kelly, never anywhere near all-in.
  7. Exit   : none. Every position is held to settlement (no early selling), and trades
              are only opened late in a window (default: last 2 min of a 5m, last 5 min of a 15m).
  8. Record : every call and paper trade is logged and scored against the
              exchange's own settlement.

It is a TRACKER + PAPER-TRADING bot. It never places a real order and needs no
API keys: it only reads Polymarket US's public market-data endpoints.

    python makawan_bot.py --preset video  # the video's example numbers exactly (default preset is "pro")
    python makawan_bot.py --params makawan_out/params.json   # settings measured by makawan_tune.py
    python makawan_bot.py --selftest      # reproduce the video's numbers
    python makawan_bot.py --simulate      # offline demo, no network
    python makawan_bot.py --discover      # check the live markets are found
    python makawan_bot.py                 # live tracking, all three windows

Example numbers only. Not financial advice. Most short-window markets are
close to a coin flip after fees; treat any "edge" as unproven until the
records (makawan_out/*.csv) say otherwise.
"""
from __future__ import annotations

__version__ = "2026.11.13"   # makawan_web.py, makawan_tune.py and simulate_week.py must come from the same download

import argparse
import csv
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from types import SimpleNamespace
from typing import Optional

try:
    import requests
except ImportError:  # only needed for live mode
    requests = None
try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
HORIZON_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


@dataclass
class Config:
    gateway: str = "https://gateway.polymarket.us"
    asset: str = "btc"
    horizons: tuple = ("5m", "15m", "1h")

    # chance model (video step 3)
    avg_window_s: int = 1           # settlement = the last one-second index print before the close. Measured on
                                    # 2026-11-10: 98 of 98 logged closes matched that print to the cent, and every
                                    # price to beat equals the previous window's settlement. (The video preset keeps
                                    # the 60-second average its example numbers assume.)
    vol_lookback_s: int = 1800      # history used to measure "how jumpy"
    vol_floor_min: float = 5.0      # never trust a vol below $5 / minute
    w_market: float = 0.5           # weight on the market's price (video: (90%+66%)/2 = 78%)

    # beyond the video: volatility, fat tails, smarter blend (all tunable, see makawan_tune.py)
    vol_short_weight: float = 0.5   # 0 = long window only (video); 1 = recent-volatility estimate only (per window length)
    vol_halflife_s: float = 300.0   # memory of the recent-volatility estimate
    vol_refresh_s: float = 5.0      # re-measure volatility this often (it moves slowly; saves CPU)
    vol_scale: float = 1.0          # multiplier on the volatility estimate (the backtest finds it)
    tail_w: float = 0.10            # share of "wild" moves: Gaussian scale mixture, unit variance
    tail_s2: float = 2.0            #   ...and how wild they are (0 weight = the video's plain bell curve)
    blend_mode: str = "avg"         # "avg" = video; "logit" = a*logit(model) + b*logit(market) + c
    blend_a: float = 0.5
    blend_b: float = 0.5
    blend_c: float = 0.0

    # the call (video step 4)
    call_up: float = 0.57
    keep_up: float = 0.53
    keep_down: float = 0.47
    call_down: float = 0.43
    warmup_frac: float = 0.10       # no call until max(warmup_min_s, this share of the window) has passed
    warmup_min_s: float = 45.0      #   (video: 1/15 of the window, which is only 20 s on a 5m window)
    join_wait_s: float = 20.0       # a window the bot joins part-way waits this long before its first call
    min_secs_left: float = 5.0      # no new entries in the last seconds (my addition)
    confirm_s: float = 3.0          # a call change must hold this long before it counts (my addition)

    # value, fees, sizing (video steps 5-7)
    fee_theta: float = 0.095        # taker fee: theta * p * (1-p) per contract. Measured on the first real trade
                                    # (Nov 10, 2026: $0.26 on 11.44 @ 41.7c, i.e. 0.092-0.096); the published 0.07 was too low
    min_edge: float = 0.01          # needs >= 1c/share after fee ("good up to 67c" at 70%)
    min_buy_px: dict = field(default_factory=lambda: {"5m": 0.25})   # video step 8: no sides < 25c on 5m
    # Trades are only opened when this many seconds or fewer are left. Calls are still tracked all window long.
    # A window length that is missing here (1h) can trade at any time.
    trade_last_s: dict = field(default_factory=lambda: {"5m": 120.0, "15m": 300.0})
    bankroll: float = 1000.0
    kelly_frac: float = 0.25
    max_trade_frac: float = 0.05    # hard cap per trade (my addition)
    min_trade_usd: float = 1.0
    trade: bool = True              # False = calls only, no paper trades
    # rules added after the first day of paper results (2026-11-09); the "video" preset turns them off
    flat_stake: float = 20.0        # dollars per trade, fee included (0 = quarter-Kelly sizing as in the video)
    max_model_gap: float = 0.20     # skip a trade when the model is more than this far above the market's price (1 = off)
    no_trade_horizons: tuple = ("1h",)   # window lengths that are tracked and scored but never traded
    # risk guard: halts all new trades when a limit is hit (calls are still tracked)
    stop_loss_frac: float = 0.5     # halt for good once losses reach this share of the starting bankroll (0 = off)
    daily_loss_limit: float = 0.0   # pause until the next day (display time zone) after losing this many dollars (0 = off)
    # live trading (real orders; only used when makawan_live.py supplies a broker)
    live_slip: float = 0.00         # limit price = the ask it saw + this, never above the price the edge allows
    live_max_orders_day: int = 150  # runaway brake: no more than this many real orders in one day
    live_max_trades: int = 0        # halt after this many real fills (0 = no limit); 1 is a good first run
    live_retries: int = 0           # after an order is not filled, try again up to this many times in the same window
    live_retry_wait_s: float = 3.0  # seconds between tries (each try re-checks the call, price, edge and limits)
    # strategy lab options (all off by default; makawan_lab.py turns them on per strategy)
    strategy: str = ""              # a name for the page and the logs
    drift_lookback_s: int = 0       # trend / fade: look at Bitcoin's move over this many seconds...
    drift_beta: float = 0.0         # ...and assume this share of it continues (+, trend) or reverses (-, fade)
    max_buy_cap: float = 0.0        # never pay more than this for a share (0 = no cap)
    paper_delay_s: float = 0.0      # paper fills: the order "arrives" this many seconds later and fills only if the
                                    # price is still there (0 = the old instant fill, which flatters paper results)
    paper_min_fill_frac: float = 0.50  # reject tiny partial paper fills below this share of requested qty (0 = off)
    # cutting losses: sell a position before settlement (off unless --cut)
    cut_on_flip: bool = False       # sell when the call turns to the other side
    cut_on_value: bool = False      # sell when the bid (after the fee) is worth more than the model's chance of winning
    cut_margin: float = 0.01        # ...by at least this much a share
    cut_only_losing: bool = True    # ...and only while selling would get back less than was paid (winners are held)
    cut_slip: float = 0.01          # sell no lower than the bid it saw minus this
    cut_min_secs_left: float = 10.0 # no selling in the last seconds
    cut_max_tries: int = 3          # real sell orders per window
    spot_use: bool = False          # use Coinbase's live price to bring the index up to date in the model
    spot_ref_offset_s: float = 0.5  # an index print stamped T is matched with Coinbase's price at T + this. It was
                                    # 1.0, but prints stamped T reach the bot at about T + 0.6-0.9 s, so T + 1 was
                                    # still in the future: Coinbase's move always came out as zero (Nov 10, 2026)
    spot_max_move: float = 0.004    # ignore Coinbase if it says Bitcoin moved more than 0.4% since the last print
    spot_max_age_s: float = 3.0     # ignore Coinbase if its last price is older than this
    live_rest: bool = False         # hybrid: if the fill-now order misses, leave a limit order at that price instead
    live_rest_poll_s: float = 1.0   # how often a resting order is looked up
    live_rest_max_s: float = 5.0    # cancel a resting order that has not filled within this many seconds
    live_rest_stop_s: float = 20.0  # cancel a resting order this many seconds before the window closes
    exec_mode: str = "taker"        # "taker": buy at the ask now (pays the fee); "maker": post a limit bid and wait
    maker_fee_theta: float = 0.0    # maker tier 1: no fee and no rebate
    maker_improve: bool = True      # maker bid one tick above the best bid (if still below the ask), else join it

    out_dir: str = "makawan_out"
    log_ticks: bool = True          # ticks.csv feeds makawan_tune.py fit-blend
    tick_log_s: float = 5.0
    overrides: dict = field(default_factory=dict)    # per-window-length settings: {"5m": {...}, "1h": {...}}

    def view(self, horizon: str) -> SimpleNamespace:
        """Settings for one window length: global values with that length's overrides applied."""
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name != "overrides"}
        d.update(self.overrides.get(horizon, {}))
        return SimpleNamespace(**d)


# "video" reproduces the example numbers exactly; "pro" (the default) uses the upgrades above.
PRESETS = {
    "video": dict(vol_short_weight=0.0, vol_scale=1.0, tail_w=0.0, blend_mode="avg", w_market=0.5,
                  call_up=0.57, keep_up=0.53, keep_down=0.47, call_down=0.43,
                  warmup_frac=1 / 15, warmup_min_s=0.0, join_wait_s=0.0, trade_last_s={}, avg_window_s=60,
                  flat_stake=0.0, max_model_gap=1.0, no_trade_horizons=()),
    "pro": {},
}
# can be set per window length in params.json
TUNABLE = ("call_up", "keep_up", "keep_down", "call_down", "warmup_frac", "min_secs_left", "confirm_s",
           "w_market", "blend_mode", "blend_a", "blend_b", "blend_c", "vol_short_weight", "vol_scale", "tail_w", "tail_s2",
           "min_edge", "kelly_frac", "max_trade_frac", "warmup_min_s", "join_wait_s")
# only under "defaults" in params.json
GLOBAL_ONLY = ("strategy", "drift_lookback_s", "drift_beta", "max_buy_cap", "paper_delay_s", "paper_min_fill_frac", "cut_on_flip", "cut_on_value", "cut_margin", "cut_only_losing", "cut_slip", "cut_min_secs_left", "cut_max_tries",
               "spot_use", "spot_ref_offset_s", "spot_max_move", "spot_max_age_s", "live_rest", "live_rest_max_s", "live_rest_poll_s", "live_rest_stop_s", "live_retries", "live_retry_wait_s", "live_slip", "live_max_orders_day", "live_max_trades", "stop_loss_frac", "daily_loss_limit", "flat_stake", "max_model_gap", "no_trade_horizons", "exec_mode", "maker_fee_theta", "maker_improve", "vol_halflife_s", "vol_refresh_s", "vol_lookback_s", "vol_floor_min", "avg_window_s",
               "fee_theta", "min_buy_px", "trade_last_s", "bankroll", "min_trade_usd")


def validate_config(cfg: Config):
    for h in cfg.horizons:
        v = cfg.view(h)
        if not (0 < v.call_down < v.keep_down <= v.keep_up < v.call_up < 1):
            raise ValueError(f"{h}: need 0 < call_down < keep_down <= keep_up < call_up < 1 "
                             f"(got {v.call_down}, {v.keep_down}, {v.keep_up}, {v.call_up})")
        if not (0.0 <= v.tail_w < 1.0) or v.tail_w * v.tail_s2 ** 2 >= 1.0 or v.tail_s2 < 1.0:
            raise ValueError(f"{h}: tail_w * tail_s2^2 must be below 1 and tail_s2 >= 1 (got {v.tail_w}, {v.tail_s2})")
        lim = v.trade_last_s.get(h)
        if lim is not None and not (0 <= lim <= HORIZON_SECONDS.get(h, 10 ** 9)):
            raise ValueError(f"{h}: trade_last_s must be between 0 and the window length (got {lim})")
        if v.warmup_min_s < 0 or v.warmup_frac < 0 or v.join_wait_s < 0:
            raise ValueError(f"{h}: warm-up and join wait cannot be negative")
        if not (0 <= v.stop_loss_frac < 1) or v.daily_loss_limit < 0:
            raise ValueError("stop_loss_frac must be from 0 up to (not including) 1, and daily_loss_limit 0 or more")
        if v.flat_stake < 0 or not (0 < v.max_model_gap <= 1):
            raise ValueError("flat_stake must be 0 or more, and max_model_gap between 0 and 1")
        if any(x not in HORIZON_SECONDS for x in v.no_trade_horizons):
            raise ValueError(f"no_trade_horizons must be from 5m, 15m, 1h (got {v.no_trade_horizons})")
        if v.exec_mode not in ("taker", "maker"):
            raise ValueError(f"exec_mode must be 'taker' or 'maker' (got {v.exec_mode})")
        if v.vol_scale <= 0 or not (0.0 <= v.vol_short_weight <= 1.0) or not (0.0 <= v.w_market <= 1.0) \
                or v.blend_mode not in ("avg", "logit"):
            raise ValueError(f"{h}: bad vol_scale / vol_short_weight / w_market / blend_mode")
        if not (0.0 <= v.paper_min_fill_frac <= 1.0):
            raise ValueError(f"{h}: paper_min_fill_frac must be between 0 and 1 (got {v.paper_min_fill_frac})")


def load_params(cfg: Config, path: str):
    """Apply a params.json (written by makawan_tune.py or by hand) on top of the preset."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for key, val in data.items():
        if key.startswith("_"):
            continue
        if key == "defaults":
            for k, v in val.items():
                if k not in TUNABLE + GLOBAL_ONLY:
                    raise ValueError(f"{path}: unknown setting '{k}'")
                if k == "no_trade_horizons":
                    cfg.no_trade_horizons = tuple(v)
                elif k in ("min_buy_px", "trade_last_s"):
                    for hk in v:
                        if hk not in HORIZON_SECONDS:
                            raise ValueError(f"{path}: {k} keys must be 5m, 15m or 1h (got '{hk}')")
                    setattr(cfg, k, {hk: float(x) for hk, x in v.items()})
                else:
                    setattr(cfg, k, type(getattr(cfg, k))(v))
        elif key in HORIZON_SECONDS:
            for k, v in val.items():
                if k not in TUNABLE:
                    raise ValueError(f"{path}: '{k}' cannot be set per window length")
                cfg.overrides.setdefault(key, {})[k] = type(getattr(cfg, k))(v)
        else:
            raise ValueError(f"{path}: unknown section '{key}' (use 'defaults', '5m', '15m' or '1h')")
    validate_config(cfg)


def parse_trade_last(text: str) -> dict:
    """'5m=120,15m=300,1h=0' -> {'5m': 120.0, '15m': 300.0}  (0 = no limit; 'none' = no limits at all)."""
    lim = {}
    if text.strip().lower() in ("none", "off", "any"):
        return lim
    for part in text.split(","):
        h, _, sec = part.partition("=")
        if h.strip() not in HORIZON_SECONDS:
            raise ValueError(f"--trade-last: unknown window length '{h}' (use 5m, 15m, 1h)")
        if float(sec) > 0:
            lim[h.strip()] = float(sec)
    return lim


def build_config(a) -> Config:
    cfg = Config(**PRESETS[getattr(a, "preset", None) or "pro"])
    cfg.horizons = tuple(x.strip() for x in a.horizons.split(",") if x.strip())
    cfg.trade, cfg.out_dir = not a.no_trade, a.out
    stored = read_state(a.out).get("bankroll")
    cfg.bankroll = a.bankroll if getattr(a, "bankroll", None) is not None else (stored or 1000.0)
    if getattr(a, "w_market", None) is not None:
        cfg.w_market = a.w_market
    if getattr(a, "confirm_s", None) is not None:
        cfg.confirm_s = a.confirm_s
    if getattr(a, "exec_mode", None):
        cfg.exec_mode = a.exec_mode
    if getattr(a, "stake", None) is not None:
        cfg.flat_stake = a.stake
    if getattr(a, "stop_loss", None) is not None:
        cfg.stop_loss_frac = a.stop_loss
    if getattr(a, "spot", None) == "use":
        cfg.spot_use = True
    if getattr(a, "cut", False):
        cfg.cut_on_value = True                 # sell only when the bid is worth more than the maths (not on a turned call)
    if getattr(a, "daily_loss", None) is not None:
        cfg.daily_loss_limit = a.daily_loss
    if getattr(a, "max_gap", None) is not None:
        cfg.max_model_gap = a.max_gap
    if getattr(a, "no_trade_on", None) is not None:
        cfg.no_trade_horizons = () if a.no_trade_on.strip().lower() in ("none", "") else \
            tuple(x.strip() for x in a.no_trade_on.split(",") if x.strip())
    if getattr(a, "trade_last", None):                    # e.g. "5m=120,15m=300,1h=0" (0 = no limit)
        cfg.trade_last_s = parse_trade_last(a.trade_last)
    if getattr(a, "warmup_s", None) is not None:          # one number for every window length
        cfg.warmup_min_s, cfg.warmup_frac = a.warmup_s, 0.0
    cfg.log_ticks = not getattr(a, "no_ticks", False)
    if getattr(a, "params", None):
        load_params(cfg, a.params)
    validate_config(cfg)
    return cfg


def describe_model(cfg: Config) -> str:
    bits = [f"tails {cfg.tail_w:.0%} x{cfg.tail_s2:g}" if cfg.tail_w > 0 else "plain bell curve",
            f"vol {cfg.vol_short_weight:.0%} recent" if cfg.vol_short_weight > 0 else "vol long-window",
            "blend avg " + format(cfg.w_market, ".0%") + " market" if cfg.blend_mode == "avg" else "blend logit"]
    if cfg.overrides:
        bits.append("tuned: " + ", ".join(sorted(cfg.overrides)))
    if not cfg.trade:
        bits.append("calls only, no trades")
    else:
        bits.append("trades: " + ", ".join(
            f"{h} " + ("none (tracking only)" if h in cfg.no_trade_horizons else
                       f"last {mmss(cfg.trade_last_s[h])}" if cfg.trade_last_s.get(h) else "any time") for h in cfg.horizons))
        bits.append(f"stake ${cfg.flat_stake:g} flat" if cfg.flat_stake > 0 else "stake quarter-Kelly")
        if cfg.max_model_gap < 1:
            bits.append(f"skip if model {cfg.max_model_gap * 100:.0f}+ pts above market")
        if cfg.stop_loss_frac > 0:
            bits.append(f"stop at -{cfg.stop_loss_frac:.0%} (${cfg.bankroll * (1 - cfg.stop_loss_frac):,.0f})")
        if cfg.daily_loss_limit > 0:
            bits.append(f"daily loss limit ${cfg.daily_loss_limit:g}")
        if cfg.spot_use:
            bits.append("index brought up to date with Coinbase")
        if cfg.drift_beta:
            bits.append(("trend: " if cfg.drift_beta > 0 else "fade: ") +
                        f"{abs(cfg.drift_beta):.0%} of the last {cfg.drift_lookback_s / 60:g} min move "
                        + ("carries on" if cfg.drift_beta > 0 else "reverses"))
        if cfg.max_buy_cap:
            bits.append(f"price cap {cfg.max_buy_cap:.0%}")
        if cfg.paper_delay_s:
            bits.append(f"honest paper fills ({cfg.paper_delay_s:g}s delay)")
        if cfg.cut_on_flip or cfg.cut_on_value:
            bits.append("cuts losses (" + " / ".join(x for x, on in (("call turns", cfg.cut_on_flip),
                                                                     ("bid beats the maths", cfg.cut_on_value)) if on)
                        + (", losing positions only" if cfg.cut_only_losing else "") + ")")
        bits.append("settles on the closing index print" if cfg.avg_window_s <= 1 else
                    f"settles on a {cfg.avg_window_s}s average")
        bits.append("orders: " + ("MAKER (limit bids, no fee)" if cfg.exec_mode == "maker" else "taker (buy at the ask, pays fee)"))
    return " | ".join(bits)


# --------------------------------------------------------------------------
# Pure maths (everything the video shows lives here; unit-tested)
# --------------------------------------------------------------------------
def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def settle_stats(S, tau, sigma_sec, obs_avg=None, L=60.0):
    """Mean and std-dev of the settlement value.

    Up/Down windows settle on the AVERAGE of the L one-second index prints before
    the close, not on the last print. If tau >= L the whole average is still in the
    future (variance sigma^2 * (tau - L + L/3)); inside the last L seconds part of
    it is already printed and only tau seconds of it can still move.
    """
    m = max(0.0, min(tau, L))
    if obs_avg is None:
        obs_avg = S
    mean = ((L - m) * obs_avg + m * S) / L
    if tau >= L:
        var = sigma_sec ** 2 * ((tau - L) + L / 3.0)
    else:
        var = sigma_sec ** 2 * (m ** 3) / (3.0 * L * L)
    return mean, math.sqrt(max(var, 0.0))


def mix_scales(w, s2):
    """Scale of the calm component so the mixture keeps unit variance."""
    v = min(w * s2 * s2, 0.98)
    return math.sqrt((1.0 - v) / (1.0 - w))


def tail_cdf(z, w=0.0, s2=2.0):
    """CDF of a fat-tailed, unit-variance distribution: (1-w) calm moves + w wild moves (w=0: normal)."""
    if w <= 0.0:
        return norm_cdf(z)
    s1 = mix_scales(w, s2)
    return (1.0 - w) * norm_cdf(z / s1) + w * norm_cdf(z / s2)


def chance_up(S, K, tau, sigma_sec, obs_avg=None, L=60.0, tail_w=0.0, tail_s2=2.0) -> float:
    """P(settlement value >= price-to-beat K).  Ties settle UP."""
    mean, sd = settle_stats(S, tau, sigma_sec, obs_avg, L)
    if sd < 1e-9:
        return 1.0 if mean >= K else 0.0
    return tail_cdf((mean - K) / sd, tail_w, tail_s2)


def estimate_sigma_per_sec(prices) -> Optional[float]:
    """Dollar std-dev per sqrt(second) from 1-second index prints.
    Uses 60-second changes when there is enough history (captures any
    mean-reversion in the index), else 1-second changes."""
    n = len(prices)
    if n >= 300:
        d = [prices[i + 60] - prices[i] for i in range(n - 60)]
        return math.sqrt(sum(x * x for x in d) / len(d)) / math.sqrt(60.0)
    if n >= 30:
        d = [prices[i + 1] - prices[i] for i in range(n - 1)]
        return math.sqrt(sum(x * x for x in d) / len(d))
    return None


def estimate_vol_parts(prices, halflife_s=300.0):
    """(long-window sigma, recent sigma), dollars per sqrt(second).
    Long: 60-second changes over the whole lookback. Recent: EWMA of 1-second changes, rescaled by the
    long-window variance ratio so both are on the same footing."""
    n = len(prices)
    if n < 300:
        s = estimate_sigma_per_sec(prices)
        return None if s is None else (s, s)
    d1 = [prices[i + 1] - prices[i] for i in range(n - 1)]
    d60 = [prices[i + 60] - prices[i] for i in range(n - 60)]
    s_long = math.sqrt(sum(x * x for x in d60) / len(d60)) / math.sqrt(60.0)
    rms1 = math.sqrt(sum(x * x for x in d1) / len(d1))
    if rms1 <= 0:
        return s_long, s_long
    ratio = min(2.0, max(0.5, s_long / rms1))
    lam = 0.5 ** (1.0 / halflife_s)
    num = den = 0.0
    for x in d1:
        num = num * lam + x * x
        den = den * lam + 1.0
    return s_long, math.sqrt(num / den) * ratio


def mix_vol(parts, short_weight):
    sl, ss = parts
    return math.sqrt((1.0 - short_weight) * sl * sl + short_weight * ss * ss)


def estimate_vol(prices, short_weight=0.5, halflife_s=300.0) -> Optional[float]:
    parts = estimate_vol_parts(prices, halflife_s)
    return None if parts is None else mix_vol(parts, short_weight)


def _logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def blend(p_model, p_mkt, w_market=0.5, mode="avg", a=0.5, b=0.5, c=0.0):
    """Combine the model's chance with the market's price.
    avg   : (1-w)*model + w*market            (the video)
    logit : sigmoid(a*logit(model) + b*logit(market) + c)   (coefficients fitted by makawan_tune.py fit-blend)"""
    if p_mkt is None:
        return p_model
    if mode == "logit":
        return _sigmoid(a * _logit(p_model) + b * _logit(p_mkt) + c)
    return (1 - w_market) * p_model + w_market * p_mkt


def fee_per_share(p, theta=0.0695):
    return theta * p * (1.0 - p)


def exch_fee(p, shares, theta=0.0695):
    """Fee the exchange would charge on a fill: rounded to the cent, banker's rounding."""
    raw = Decimal(str(theta)) * Decimal(str(shares)) * Decimal(str(p)) * (Decimal(1) - Decimal(str(p)))
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def all_in_cost(p, theta=0.0695):
    return p + fee_per_share(p, theta)


def ev_per_share(p_win, price, theta=0.0695):
    return p_win - all_in_cost(price, theta)


def max_buy_price(p_win, theta=0.0695, min_edge=0.01):
    """Highest whole-cent price at which the share still has >= min_edge after fee."""
    lo, hi = 0.0, 0.99
    if all_in_cost(0.01, theta) > p_win - min_edge:
        return 0.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if all_in_cost(mid, theta) <= p_win - min_edge:
            lo = mid
        else:
            hi = mid
    return math.floor(lo * 100 + 1e-9) / 100.0


def kelly_dollars(p_win, price, bankroll, theta=0.0695, frac=0.25):
    """Fractional Kelly stake in dollars for a $1-payout binary share."""
    c = all_in_cost(price, theta)
    if p_win <= c or c >= 1:
        return 0.0
    return bankroll * frac * (p_win - c) / (1.0 - c)


def next_call(state, p, cfg: Config):
    """Hysteresis so the call doesn't flip-flop on every wiggle (video step 4)."""
    if state == "UP":
        if p <= cfg.call_down:
            return "DOWN"
        return "UP" if p >= cfg.keep_up else None
    if state == "DOWN":
        if p >= cfg.call_up:
            return "UP"
        return "DOWN" if p <= cfg.keep_down else None
    if p >= cfg.call_up:
        return "UP"
    if p <= cfg.call_down:
        return "DOWN"
    return None


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
DISPLAY_TZ = timezone.utc
DISPLAY_TZ_NAME = "UTC"


def set_display_tz(name: str = "America/New_York") -> bool:
    """Time zone used for on-screen times and the *_et columns. Needs `pip install tzdata` on Windows."""
    global DISPLAY_TZ, DISPLAY_TZ_NAME
    try:
        DISPLAY_TZ, DISPLAY_TZ_NAME = ZoneInfo(name), name
        return True
    except Exception:
        DISPLAY_TZ, DISPLAY_TZ_NAME = timezone.utc, "UTC"
        return False


def iso(ts: float) -> str:
    """UTC, for machine-readable columns and API parameters."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def tz_abbr(ts: float) -> str:
    return datetime.fromtimestamp(ts, DISPLAY_TZ).strftime("%Z")


def hms(ts: float) -> str:
    return datetime.fromtimestamp(ts, DISPLAY_TZ).strftime("%H:%M:%S")


set_display_tz("America/New_York")


def mmss(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}" if sec < 3600 else f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def parse_ts(v) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e11 else float(v)
    if isinstance(v, dict):
        return float(v["seconds"]) + float(v.get("nanos", 0)) / 1e9 if "seconds" in v else None
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return parse_ts(float(s))
    s = s.replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)          # trim nanoseconds
    return datetime.fromisoformat(s).timestamp()


def amount(x) -> Optional[float]:
    if x is None or x == "":
        return None
    if isinstance(x, dict):
        return amount(x.get("value"))
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Order book
# --------------------------------------------------------------------------
@dataclass
class Book:
    bids: list      # [(px, qty)] best (highest) first
    asks: list      # [(px, qty)] best (lowest) first
    last_px: Optional[float] = None    # last trade price (of the UP/Yes contract), from the book's stats
    volume: Optional[float] = None     # total shares traded so far (to spot new trades between polls)

    @property
    def bid(self):
        return self.bids[0][0] if self.bids else None

    @property
    def ask(self):
        return self.asks[0][0] if self.asks else None

    def mid(self):
        return (self.bid + self.ask) / 2 if self.bids and self.asks else None

    def flipped(self) -> "Book":
        """The DOWN side of the same contract: DOWN ask = 1 - UP bid, DOWN bid = 1 - UP ask."""
        return Book([(round(1 - p, 4), q) for p, q in self.asks],
                    [(round(1 - p, 4), q) for p, q in self.bids],
                    None if self.last_px is None else round(1 - self.last_px, 4), self.volume)


def walk(levels, shares):
    """Fill `shares` from price levels (best first). Returns (avg price, filled)."""
    need, cost, got = shares, 0.0, 0.0
    for px, q in levels:
        take = min(q, need)
        cost += take * px
        got += take
        need -= take
        if need <= 1e-9:
            break
    return (cost / got if got else None), got


# --------------------------------------------------------------------------
# Market model + parsing of Polymarket US responses
# --------------------------------------------------------------------------
@dataclass
class MarketInfo:
    slug: str
    horizon: str
    ws: float                       # window start (epoch s)
    we: float                       # window end = settlement instant
    ptb: Optional[float] = None     # price to beat
    settle: Optional[float] = None  # exchange-stamped settlement value
    index_symbol: str = ""
    decimals: int = 2
    state: str = ""
    tick: float = 0.01              # orderPriceMinTickSize: smallest price step for orders
    min_qty: float = 1.0            # minimumTradeQty: smallest order size, in contracts


def horizon_label(seconds: float) -> str:
    for name, sec in HORIZON_SECONDS.items():
        if abs(seconds - sec) < 1:
            return name
    return f"{int(round(seconds / 60))}m"


def parse_market(raw: dict, asset: str = "btc") -> Optional[MarketInfo]:
    """Return MarketInfo for a <asset> Up/Down window, else None."""
    t = raw.get("assetPriceTerms") or {}
    if t.get("marketType") != "ASSET_PRICE_MARKET_TYPE_UP_DOWN":
        return None
    if str((t.get("asset") or {}).get("symbol", "")).lower() != asset.lower():
        return None
    ws, we = parse_ts(t.get("windowStart")), parse_ts(t.get("windowEnd"))
    if ws is None or we is None:
        return None
    return MarketInfo(
        slug=raw.get("slug", ""), horizon=horizon_label(we - ws), ws=ws, we=we,
        ptb=amount(t.get("priceToBeat")), settle=amount(t.get("settlementPrice")),
        index_symbol=str(t.get("indexSymbol") or ""),
        decimals=int(t.get("priceDecimals") or 2),
        tick=float(raw.get("orderPriceMinTickSize") or 0.01),
        min_qty=float(raw.get("minimumTradeQty") or 1.0),
        state="closed" if raw.get("closed") else ("active" if raw.get("active", True) else "inactive"),
    )


def parse_book(raw: dict) -> Optional[Book]:
    md = (raw or {}).get("marketData") or {}
    def lv(rows):
        out = []
        for r in rows or []:
            px, q = amount(r.get("px")), amount(r.get("qty"))
            if px is not None and q:
                out.append((px, q))
        return out
    bids = sorted(lv(md.get("bids")), key=lambda x: -x[0])
    asks = sorted(lv(md.get("offers")), key=lambda x: x[0])
    stats = md.get("stats") or {}
    vol = amount(stats.get("sharesTraded"))
    return Book(bids, asks, amount(stats.get("lastTradePx")), vol)


def parse_candles(raw: dict, asset: str = "btc"):
    """-> list of (unix_second, close, tick_count) ascending."""
    for s in (raw or {}).get("series", []):
        if str((s.get("asset") or {}).get("symbol", "")).lower() == asset.lower():
            return [(int(c["timestamp"]), float(c["close"]), int(c.get("tickCount", 1)))
                    for c in s.get("candles", [])]
    return []


def slug_candidates(asset, horizon, ws):
    dt = datetime.fromtimestamp(ws, timezone.utc)
    toks = {"5m": ["5m"], "15m": ["15m"], "1h": ["1h", "60m"]}.get(horizon, [horizon])
    return [f"cpc-{asset}-updown-{t}-{dt:%Y-%m-%d-%H%M}z" for t in toks]


# --------------------------------------------------------------------------
# API layer (transport is swappable: real HTTP or the offline simulator)
# --------------------------------------------------------------------------
class ApiError(Exception):
    pass


class NotFound(ApiError):
    pass


class HttpTransport:
    def __init__(self, base, timeout=8.0):
        if requests is None:
            sys.exit("Live mode needs 'requests':  pip install requests rich")
        self.base, self.timeout = base.rstrip("/"), timeout
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "makawan-btc-tracker/1.0"
        self.backoff_until = 0.0

    CACHE_HEADERS = ("age", "cache-control", "cf-cache-status", "x-cache", "via", "date", "x-amz-cf-pop")

    def get(self, path, params=None, headers=None):
        wait = self.backoff_until - time.time()
        if wait > 0:
            time.sleep(wait)
        r = self.s.get(self.base + path, params=params, timeout=self.timeout, headers=headers)
        if r.status_code == 429:                      # rate limit is 25 req/s; we use ~6
            self.backoff_until = time.time() + 2.0
            raise ApiError("429 Too Many Requests")
        if r.status_code == 404:
            e = NotFound(path)
            e.headers = {k: v for k, v in r.headers.items() if k.lower() in self.CACHE_HEADERS}
            raise e
        r.raise_for_status()
        return r.json()


class Api:
    def __init__(self, transport, cfg: Config):
        self.t, self.cfg = transport, cfg

    def brti(self, frm: int, to: int = 0):
        raw = self.t.get("/v1/asset-prices/history", {
            "assets": f"crypto:{self.cfg.asset}", "from": int(frm), "to": int(to),
            "bucket": "ASSET_PRICE_BUCKET_1S"})
        return parse_candles(raw, self.cfg.asset)

    def market(self, slug: str) -> Optional[MarketInfo]:
        try:
            raw = self.t.get(f"/v1/market/slug/{slug}")
        except NotFound:
            return None
        return parse_market(raw.get("market", raw), self.cfg.asset)

    def book(self, slug: str) -> Optional[Book]:
        """A market's order book. Polymarket sometimes answers 'not found' for minutes after a new window opens
        while the market itself exists: an old 'not found' kept by a cache in front of the API. Asking again with
        a one-off extra in the address gets past that cache; once it has, every request for that market does so."""
        path = f"/v1/markets/{slug}/book"
        bust = getattr(self, "bust", None)
        if bust is None:
            bust = self.bust = set()
        fresh = {"_": str(int(time.time() * 1000))}
        nocache = {"Cache-Control": "no-cache"}
        try:
            raw = (self.t.get(path, fresh, nocache) if slug in bust else self.t.get(path))
        except NotFound as e:
            self.last_404 = {"slug": slug, "headers": getattr(e, "headers", {}), "time": iso(time.time())}
            if slug in bust:
                raise
            raw = self.t.get(path, fresh, nocache)          # still 'not found' -> the market really has no book
            bust.add(slug)
            if len(bust) > 50:
                bust.clear()
        return parse_book(raw)

    def settlement(self, slug: str) -> Optional[float]:
        try:
            raw = self.t.get(f"/v1/markets/{slug}/settlement")
        except NotFound:
            return None
        return amount(raw.get("settlement"))

    def list_windows(self, now: float):
        out, offset, limit = [], 0, 100
        for _ in range(8):
            raw = self.t.get("/v1/markets", {
                "categories": "crypto", "closed": "false", "active": "true",
                "limit": limit, "offset": offset,
                "endDateMin": iso(now - 3600), "endDateMax": iso(now + 4 * 3600)})
            rows = raw.get("markets", [])
            for r in rows:
                m = parse_market(r, self.cfg.asset)
                if m:
                    out.append(m)
            if len(rows) < limit:
                break
            offset += limit
        return out

    def find_active(self, now: float):
        """{horizon: MarketInfo} for the window that contains `now`."""
        found = {}
        try:
            for m in self.list_windows(now):
                if m.horizon in self.cfg.horizons and m.ws <= now < m.we:
                    found[m.horizon] = m
        except (ApiError, KeyError, ValueError) as e:
            self.last_list_error = str(e)
        for h in self.cfg.horizons:                    # fallback: build the slug from the clock
            if h in found:
                continue
            dur = HORIZON_SECONDS.get(h)
            if not dur:
                continue
            ws = math.floor(now / dur) * dur
            for slug in slug_candidates(self.cfg.asset, h, ws):
                try:
                    m = self.market(slug)
                except ApiError:
                    m = None
                if m and m.ws <= now < m.we:
                    found[h] = m
                    break
        return found


# --------------------------------------------------------------------------
# Live order-book stream (optional; needs your API key, read-only)
# --------------------------------------------------------------------------
class BookStream:
    """Keeps the latest order book for each wanted market from Polymarket US's markets WebSocket, on a background
    thread. It only listens: it cannot place, change or cancel orders. If it is not connected or has nothing for a
    market, get() returns None and the engine falls back to polling."""

    def __init__(self, key_id, secret_key, base_url="wss://api.polymarket.us"):
        self.key_id, self.secret_key, self.base_url = key_id, secret_key, base_url
        self.lock = threading.Lock()
        self.wanted: set = set()
        self.books: dict = {}             # slug -> (Book, time received)
        self.subs: dict = {}              # slug -> request id
        self.connected = False
        self.disabled = False             # set by the engine if the stream keeps disagreeing with polled books
        self.last_msg = 0.0
        self.updates = 0
        self.reconnects = 0
        self.error = ""
        self._stop = False
        self._n = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop = True

    def want(self, slugs):
        with self.lock:
            self.wanted = set(slugs)

    def get(self, slug):
        """Latest book for slug, or None if the stream cannot be trusted right now."""
        if self.disabled or not self.connected or time.time() - self.last_msg > 15.0:
            return None
        with self.lock:
            item = self.books.get(slug)
        return item[0] if item else None

    def status(self):
        if self.disabled:
            return "off (kept disagreeing with polled prices); polling"
        if not self.connected:
            return f"connecting... polling meanwhile{' (' + self.error + ')' if self.error else ''}"
        quiet = time.time() - self.last_msg
        if quiet > 15.0:
            return f"silent for {quiet:.0f}s; polling meanwhile"
        return f"live ({self.updates:,} updates)"

    def _on_market_data(self, msg):
        md = (msg or {}).get("marketData") or {}
        slug = md.get("marketSlug")
        if not slug:                                        # fall back to the request id we subscribed with
            rid = (msg or {}).get("requestId")
            slug = next((k for k, v in self.subs.items() if v == rid), None)
        if not slug:
            return
        book = parse_book({"marketData": md})
        with self.lock:
            self.books[slug] = (book, time.time())
        self.updates += 1

    def _touch(self, *_):
        self.last_msg = time.time()

    def _run(self):
        import asyncio
        try:
            asyncio.run(self._main())
        except Exception as e:                              # the bot keeps running on polling
            self.error, self.connected = f"{type(e).__name__}: {e}", False

    async def _main(self):
        import asyncio
        from polymarket_us.websocket import MarketsWebSocket
        backoff = 1.0
        while not self._stop and not self.disabled:
            ws = MarketsWebSocket(key_id=self.key_id, secret_key=self.secret_key, base_url=self.base_url)
            ws.on("message", self._touch)
            ws.on("market_data", self._on_market_data)
            ws.on("error", lambda e: setattr(self, "error", str(e)[:120]))
            try:
                await ws.connect()
            except Exception as e:
                self.error = f"{type(e).__name__}: {str(e)[:100]}"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            backoff, self.error = 1.0, ""
            self.subs = {}
            with self.lock:
                self.books = {}
            self.connected, self.last_msg = True, time.time()
            try:
                while not self._stop and not self.disabled:
                    task = ws._message_task
                    if task is None or task.done() or not ws.is_connected:
                        break
                    if time.time() - self.last_msg > 45.0:   # no heartbeat either: treat the connection as dead
                        break
                    with self.lock:
                        wanted = set(self.wanted)
                    for slug in wanted - set(self.subs):
                        self._n += 1
                        rid = f"md-{self._n}"
                        await ws.subscribe_market_data(rid, [slug])
                        self.subs[slug] = rid
                    for slug in set(self.subs) - wanted:
                        await ws.unsubscribe(self.subs.pop(slug))
                        with self.lock:
                            self.books.pop(slug, None)
                    await asyncio.sleep(0.2)
            except Exception as e:
                self.error = f"{type(e).__name__}: {str(e)[:100]}"
            finally:
                self.connected = False
                try:
                    await ws.close()
                except Exception:
                    pass
            if not self._stop and not self.disabled:
                self.reconnects += 1
                await asyncio.sleep(backoff)


def keep_awake(say=print):
    """On a Mac, stop idle sleep for as long as this process runs (macOS's own `caffeinate`; nothing is changed
    permanently, and closing the lid still sleeps the Mac). Returns the helper process or None."""
    if sys.platform != "darwin" or os.environ.get("MAKAWAN_AWAKE") == "1":
        return None
    exe = shutil.which("caffeinate")
    if not exe:
        return None
    try:
        proc = subprocess.Popen([exe, "-i", "-s", "-w", str(os.getpid())])
    except OSError:
        return None
    os.environ["MAKAWAN_AWAKE"] = "1"
    say("Keeping this Mac awake while the bot runs (closing the lid still puts it to sleep; --allow-sleep turns this off).")
    return proc


def use_certifi_certificates():
    """Python from python.org on a Mac has no list of trusted certificates until 'Install Certificates.command' is run,
    so secure WebSocket connections fail with CERTIFICATE_VERIFY_FAILED (plain web requests work: they bring their
    own list, certifi). Point Python's SSL at certifi's list too, unless the user already chose one."""
    try:
        import certifi
    except ImportError:
        return None
    path = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", path)                  # also covers Polymarket's own WebSocket (--stream)
    return path


def ssl_context():
    import ssl
    path = use_certifi_certificates()
    return ssl.create_default_context(cafile=path) if path else ssl.create_default_context()


class SpotFeed:
    """Coinbase's live BTC-USD price from its public WebSocket (no account, no key), on a background thread.
    Coinbase is one of the exchanges the BRTI index is built from, and its feed arrives within milliseconds,
    while the index reaches the bot 0.6-0.9 s old, once a second. Read-only: it can't trade."""

    URL = "wss://advanced-trade-ws.coinbase.com"

    def __init__(self, url=None, product="BTC-USD", keep_s=900):
        self.url, self.product, self.keep_s = url or self.URL, product, keep_s
        self.lock = threading.Lock()
        self.ticks = deque()              # (local time received, price)
        self.connected = False
        self.last_msg = 0.0
        self.updates = 0
        self.reconnects = 0
        self.error = ""
        self._stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop = True

    def on_message(self, raw, now=None):
        """Read one message. Ticker messages carry the latest trade price."""
        now = time.time() if now is None else now
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except ValueError:
            return
        self.last_msg = now
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "error":
            self.error = str(msg.get("message") or msg)[:120]
            return
        if msg.get("channel") not in ("ticker", "ticker_batch", "market_trades"):
            return
        for ev in msg.get("events") or []:
            for t in (ev.get("tickers") or []) + (ev.get("trades") or []):
                if t.get("product_id", self.product) != self.product:
                    continue
                try:
                    px = float(t.get("price"))
                except (TypeError, ValueError):
                    continue
                if px > 0:
                    with self.lock:
                        self.ticks.append((now, px))
                        while self.ticks and self.ticks[0][0] < now - self.keep_s:
                            self.ticks.popleft()
                    self.updates += 1

    def latest(self):
        """(price, seconds old) or (None, None)."""
        with self.lock:
            if not self.ticks:
                return None, None
            t, px = self.ticks[-1]
        return px, time.time() - t

    def price_at(self, ts):
        """The last price received at or before `ts`, or None if the feed has nothing that old."""
        with self.lock:
            if not self.ticks or self.ticks[0][0] > ts:
                return None
            lo, hi = 0, len(self.ticks) - 1
            while lo < hi:                                 # last tick with time <= ts
                mid = (lo + hi + 1) // 2
                if self.ticks[mid][0] <= ts:
                    lo = mid
                else:
                    hi = mid - 1
            return self.ticks[lo][1]

    def status(self):
        if not self.connected:
            return f"Coinbase: connecting...{' (' + self.error + ')' if self.error else ''}"
        px, age = self.latest()
        if px is None:
            return "Coinbase: connected, no price yet"
        return f"Coinbase: ${px:,.2f} ({age:.1f}s old, {self.updates:,} updates)"

    def _run(self):
        import asyncio
        try:
            asyncio.run(self._main())
        except Exception as e:                              # the bot carries on with the index alone
            self.error, self.connected = f"{type(e).__name__}: {e}", False

    async def _main(self):
        import asyncio
        import websockets
        backoff = 1.0
        while not self._stop:
            try:
                extra = {"ssl": ssl_context()} if self.url.startswith("wss://") else {}
                async with websockets.connect(self.url, open_timeout=10, ping_interval=20, max_size=2 ** 22,
                                              **extra) as ws:
                    for ch in ("heartbeats", "ticker"):
                        await ws.send(json.dumps({"type": "subscribe", "product_ids": [self.product], "channel": ch}))
                    self.connected, self.error, backoff = True, "", 1.0
                    while not self._stop:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            break                           # silent for 30 s: reconnect
                        self.on_message(raw)
            except Exception as e:
                self.error = f"{type(e).__name__}: {str(e)[:100]}"
            self.connected = False
            if self._stop:
                break
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def make_spot(mode, say=print, url=None):
    """mode: 'off', 'watch' (log Coinbase next to the index) or 'use' (also trade on it)."""
    if mode == "off":
        return None
    try:
        import websockets  # noqa: F401
    except ImportError:
        say("Coinbase price feed needs the websockets library:  pip3 install websockets   Continuing without it.")
        return None
    use_certifi_certificates()
    return SpotFeed(url=url).start()


def make_stream(say=print):
    """Start a BookStream from the API key in the environment, or explain why not and return None."""
    kid = os.environ.get("POLYMARKET_KEY_ID", "").strip()
    sec = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    if not kid or not sec:
        say("--stream needs your API key in this Terminal window (POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY). "
            "Continuing with polling.")
        return None
    try:
        import polymarket_us.websocket  # noqa: F401
    except ImportError:
        say("--stream needs Polymarket's library:  pip3 install polymarket-us   Continuing with polling.")
        return None
    use_certifi_certificates()
    return BookStream(kid, sec).start()


# --------------------------------------------------------------------------
# Paper-trading state
# --------------------------------------------------------------------------
@dataclass
class Position:
    side: str
    shares: float
    avg_px: float
    fee: float
    cost: float          # shares * avg_px + fee
    opened: float


@dataclass
class WindowState:
    info: MarketInfo
    call: Optional[str] = None
    n_changes: int = 0
    p_model: Optional[float] = None
    p_mkt: Optional[float] = None
    p_final: Optional[float] = None
    book: Optional[Book] = None
    tau: float = 0.0
    note: str = ""
    value_note: str = ""
    position: Optional[Position] = None
    entered: bool = False
    snap50: Optional[float] = None
    sigma_min: Optional[float] = None
    last_poll: float = 0.0
    ended_at: Optional[float] = None
    final_call: Optional[str] = None
    outcome: Optional[str] = None
    provisional: bool = False
    pend_state: object = None          # (candidate call,) waiting to be confirmed
    pend_since: float = 0.0
    first_call: Optional[str] = None
    first_call_p: Optional[float] = None
    first_call_left: Optional[float] = None
    trade: Optional[dict] = None       # what we bought
    order: Optional[dict] = None       # maker mode: the resting limit bid {side, px, shares, posted}
    order_status: str = ""             # maker mode: filled / partial / unfilled / cancelled
    order_px: Optional[float] = None
    p_model_index: Optional[float] = None   # the model on the official index alone
    p_model_spot: Optional[float] = None    # the model with Coinbase bringing the index up to date
    spot_est: Optional[float] = None
    spot_px: Optional[float] = None
    book_error: str = ""                    # why there is no order book this tick, if a request failed
    paper_order: Optional[dict] = None      # honest paper fills: an order "in flight" {side, limit, qty, at, ask}
    drift_adj: float = 0.0                  # dollars the trend / fade rule added to Bitcoin's price in the maths
    cut_pnl: float = 0.0                    # profit or loss already taken by selling part or all of the position
    cut_tries: int = 0
    empty_since: Optional[float] = None
    empty_logged: float = 0.0
    live_order: Optional[dict] = None  # hybrid: a REAL resting limit order {id, side, limit, qty, filled, fee, posted, checked}
    prev_volume: Optional[float] = None  # shares traded at the previous poll
    vol_delta: float = 0.0               # shares traded since the previous poll
    hist: list = field(default_factory=list)   # [(unix second, chance)] for the charts
    last_tick_log: float = 0.0
    joined_at: float = 0.0
    joined_late: bool = False          # the bot started watching this window part-way through


@dataclass
class Stats:
    windows: int = 0
    called: int = 0
    correct: int = 0
    trades: int = 0
    staked: float = 0.0
    pnl: float = 0.0                 # net, after fees (settled trades)
    fees: float = 0.0                # fees on settled trades
    orders: int = 0                  # maker mode: windows where a limit bid was posted
    fills: int = 0                   # maker mode: ... and it was (at least partly) filled
    brier: float = 0.0
    brier_n: int = 0


SIM_MAKER_MSG = ("Maker mode cannot be simulated fairly (the fake market has no trades, so paper bids only fill when the price moves against them). Run it on live paper data instead, or drop --exec maker for a simulation.")

STATE_FILE = "state.json"
LOG_FILES = ("events.csv", "windows.csv", "ticks.csv", STATE_FILE)


def read_state(out_dir: str) -> dict:
    try:
        with open(os.path.join(out_dir, STATE_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_state(out_dir: str, bankroll: float = None, **extra):
    """Update state.json, keeping what is already there (a trading halt must survive a restart)."""
    os.makedirs(out_dir, exist_ok=True)
    data = read_state(out_dir)
    if bankroll is not None:
        data["bankroll"] = bankroll
    for k, v in extra.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    data["updated"] = iso(time.time())
    with open(os.path.join(out_dir, STATE_FILE), "w", encoding="utf-8") as f:
        json.dump(data, f)


def clear_halt(out_dir: str, rec, resume: bool):
    """With --resume: clear a stop-loss halt. Without it: say that the halt is still in force."""
    why = read_state(out_dir).get("halted")
    if not why:
        return
    if resume:
        write_state(out_dir, halted=None, halted_at=None)
        rec.system(time.time(), "RESUME", "halt cleared with --resume")
        print("Stop-loss halt cleared with --resume. If the account is still at or under the stop, it will halt again.")
    else:
        print(f"TRADING IS HALTED in {out_dir}: {why}\n  It keeps tracking but will not trade. "
              f"To trade again, restart with --resume (or --fresh to start a new record).")


def day_key(ts: float) -> str:
    return datetime.fromtimestamp(ts, DISPLAY_TZ).strftime("%Y-%m-%d")


def last_logged_time(out_dir: str) -> Optional[float]:
    """Latest moment recorded in this folder (for resuming a simulation's clock after it)."""
    best = None
    for name, col in (("events.csv", "time_utc"), ("windows.csv", "window_end")):
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    t = parse_ts(r.get(col))
                except (ValueError, TypeError):
                    t = None
                if t and (best is None or t > best):
                    best = t
    return best


def sim_start(out_dir: str):
    """(start time, seconds of fake history) for a simulation: just before an hour boundary, after anything already
    logged in this folder, with enough fake history behind it to settle positions carried over from the last run."""
    last = last_logged_time(out_dir)
    base = max(time.time(), (last or 0) + 120)
    start = (int(base) // 3600 + 1) * 3600 - 20
    hist = 4500 if last is None else int(min(start - last + 4500, 3 * 86400))
    return start, hist


def archive_outputs(out_dir: str):
    """Move the history files into a dated sub-folder so the next run starts from scratch. Returns the folder or None."""
    present = [f for f in LOG_FILES if os.path.exists(os.path.join(out_dir, f))]
    if not present:
        return None
    dest = os.path.join(out_dir, "archive-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dest, exist_ok=True)
    for f in present:
        shutil.move(os.path.join(out_dir, f), os.path.join(dest, f))
    return dest


class Recorder:
    EVENT = ["time_utc", "time_et", "horizon", "slug", "event", "side", "chance", "model", "market",
             "price", "shares", "fee", "pnl", "note"]
    WIN = ["window_start", "window_end", "window_start_et", "window_end_et", "horizon", "slug", "price_to_beat", "settlement",
           "outcome", "provisional", "first_call", "first_call_chance", "first_call_secs_left",
           "first_call_correct", "final_call", "call_changes", "chance_at_50pct",
           "traded_side", "shares", "avg_px", "liquidity", "gross_pnl", "fee", "net_pnl", "exit", "pnl",
           "order_status", "order_px"]
    # liquidity: "taker" (bought at the ask, pays the fee) or "maker" (a resting order was filled, no fee at maker tier 1)
    # gross_pnl = payout - shares x price;  net_pnl = gross_pnl - fee;  pnl = net_pnl (kept for older files and tools)

    TICK = ["time_utc", "horizon", "slug", "secs_left", "brti", "price_to_beat", "sigma_min",
            "p_model", "p_market", "p_final", "bid", "ask", "call", "coinbase", "brti_now_est", "p_model_index",
            "p_model_spot"]

    def __init__(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        self.lock = threading.Lock()
        self.fe, self.we = self._open(os.path.join(out_dir, "events.csv"), self.EVENT)
        self.fw, self.ww = self._open(os.path.join(out_dir, "windows.csv"), self.WIN)
        self.ft, self.wt = self._open(os.path.join(out_dir, "ticks.csv"), self.TICK)

    def tick(self, now, ws, S):
        def r(x, n):
            return "" if x is None else round(x, n)
        b = ws.book
        with self.lock:
            self.wt.writerow([iso(now), ws.info.horizon, ws.info.slug, int(ws.tau), r(S, 2), r(ws.info.ptb, 2),
                              r(ws.sigma_min, 2), r(ws.p_model, 4), r(ws.p_mkt, 4), r(ws.p_final, 4),
                              r(b.bid if b else None, 4), r(b.ask if b else None, 4), ws.call or "",
                              r(ws.spot_px, 2), r(ws.spot_est, 2), r(ws.p_model_index, 4), r(ws.p_model_spot, 4)])
            self.ft.flush()

    @staticmethod
    def _open(path, cols):
        if os.path.exists(path) and os.path.getsize(path) > 0:      # an older version wrote different columns:
            with open(path, newline="", encoding="utf-8") as f:     # keep the history, line it up with the new layout
                rows = list(csv.reader(f))
            if rows and rows[0] != cols:
                shutil.copy(path, path + ".bak")
                old = [dict(zip(rows[0], r)) for r in rows[1:]]
                for d in old:
                    if "net_pnl" in cols and d.get("traded_side") and d.get("pnl", "") != "":
                        try:
                            fee = float(d.get("fee") or 0)
                            d.setdefault("liquidity", "taker")
                            d.setdefault("net_pnl", d["pnl"])
                            d.setdefault("gross_pnl", f"{float(d['pnl']) + fee:.2f}")
                        except ValueError:
                            pass
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(cols)
                    w.writerows([d.get(c, "") for c in cols] for d in old)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        f = open(path, "a", newline="", encoding="utf-8")
        w = csv.writer(f)
        if new:
            w.writerow(cols)
            f.flush()
        return f, w

    def event(self, now, ws: WindowState, event, side="", price="", shares="", fee="", pnl="", note=""):
        def r(x, n):
            return round(x, n) if isinstance(x, (int, float)) else ""
        with self.lock:
            self.we.writerow([iso(now), local_iso(now), ws.info.horizon, ws.info.slug, event, side,
                              r(ws.p_final, 4), r(ws.p_model, 4), r(ws.p_mkt, 4),
                              r(price, 4) if price != "" else "", shares, fee, r(pnl, 2) if pnl != "" else "", note])
            self.fe.flush()

    def system(self, now, event, note=""):
        with self.lock:
            self.we.writerow([iso(now), local_iso(now), "", "", event, "", "", "", "", "", "", "", "", note])
            self.fe.flush()

    def window(self, row: dict):
        row = dict(row, window_start_et=local_iso(row["_ws"]), window_end_et=local_iso(row["_we"]))
        with self.lock:
            self.ww.writerow([row.get(c, "") for c in self.WIN])
            self.fw.flush()


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------
class Engine:
    def __init__(self, cfg: Config, api: Api, clock, rec: Recorder, parallel=True, stream=None, broker=None, spot=None):
        self.cfg, self.api, self.clock, self.rec = cfg, api, clock, rec
        self.spot = spot                      # optional SpotFeed: Coinbase's live price
        self.broker = broker                  # None = paper. Set by makawan_live.py to send REAL orders
        self.sent: dict = {}                  # market slug -> real orders sent for it (first try + retries)
        self.last_send: dict = {}             # market slug -> time of its last real order
        self.orphans: list = []
        self.done: set = set()                # market slugs whose tries are over (filled, rejected, error, or used up)
        self.sends_by_day: dict = {}          # day -> real orders sent
        self.paper_tries = 0                  # honest paper fills: orders sent / filled
        self.paper_fills = 0
        self.real = None                      # live: Polymarket's own balance {balance, buying_power, at}
        self._real_at = 0.0
        self.live_fills = 0                   # real fills on record / in this run
        self.session_fills = 0
        self.order_errors = 0                 # consecutive failed order requests
        self.stream = stream                  # optional BookStream: live order books instead of polling
        self.stream_used = 0                  # books taken from the stream / from polling (for the page)
        self.polled = 0
        self._xcheck: dict = {}               # slug -> [last check time, consecutive disagreements]
        self.halted = ""                      # risk guard: why trading is halted for good ("" = not halted)
        self.day_pnl: dict = {}               # day -> settled P&L that day (for the daily loss limit)
        self.day_paused = ""                  # the day trading is paused for
        self._last_tick = 0.0
        self.sleep_gaps = 0                   # times the clock jumped (computer asleep, or the bot paused)
        self.sleep_secs = 0.0
        self.parallel = parallel              # fetch the books on threads (live); False is faster for the simulator
        self.prints: dict = {}
        self.last_print_ts = 0
        self.sigma_sec: Optional[float] = None      # for display (scaled)
        self.sig_parts = None                       # (long, recent) volatility; each window length mixes + scales them
        self.active: dict = {}
        self.pending: list = []
        self.cash = cfg.bankroll
        self.stats = {h: Stats() for h in cfg.horizons}
        self.events = deque(maxlen=14)       # (seq, text), newest first
        self.event_seq = 0
        self.history = deque(maxlen=60)      # recent finished windows, newest first
        self.notes: list = []                # one-off messages for the page (what was restored)
        self.restored: dict = {}             # slug -> window with a position carried over from an earlier run
        self.unresolved_cost = 0.0           # cost of earlier positions whose market could not be found
        self.warnings: list = []
        self.last_discovery = 0.0
        self._vol_at = -1e18
        self.last_error = ""
        self.errors = 0
        self.pool = ThreadPoolExecutor(max_workers=6)

    # ---- helpers -------------------------------------------------------
    @property
    def S(self):
        return self.prints.get(self.last_print_ts)

    def equity(self):
        open_cost = sum(w.position.cost for w in self.active.values() if w.position) + \
                    sum(w.position.cost for w in self.pending if w.position) + \
                    sum(w.position.cost for w in self.restored.values() if w.position) + self.unresolved_cost
        return self.cash + open_cost

    def log(self, now, ws, text):
        self.event_seq += 1
        self.events.appendleft((self.event_seq, f"{hms(now)} {ws.info.horizon:>3}  {text}"))

    def warn(self, text):
        if text not in self.warnings:
            self.warnings.append(text)

    # ---- data refresh --------------------------------------------------
    def bootstrap(self):
        now = self.clock.time()
        self._refresh_prices(now, first=True)
        if self.S is None:
            self.warn("No index prints yet.")
        elif abs(now - self.last_print_ts) > 10:
            self.warn(f"Latest index print is {int(now - self.last_print_ts)}s old: stale feed or a wrong PC clock.")

    def _refresh_prices(self, now, first=False):
        n = int(now)
        frm = n - self.cfg.vol_lookback_s - 90 if first or not self.last_print_ts else self.last_print_ts - 5
        for ts, close, ticks in self.api.brti(frm, 0):
            self.prints[ts] = close
            if ticks > 0 and ts > self.last_print_ts:
                self.last_print_ts = ts
        keep_from = n - self.cfg.vol_lookback_s - 400
        if len(self.prints) > self.cfg.vol_lookback_s + 900:
            for ts in [t for t in self.prints if t < keep_from]:
                del self.prints[ts]
        if self.last_print_ts and (self.sig_parts is None or now - self._vol_at >= self.cfg.vol_refresh_s):
            self._vol_at = now
            series, prev = [], None
            for ts in range(self.last_print_ts - self.cfg.vol_lookback_s, self.last_print_ts + 1):
                prev = self.prints.get(ts, prev)
                if prev is not None:
                    series.append(prev)
            parts = estimate_vol_parts(series, self.cfg.vol_halflife_s)
            if parts is not None:
                self.sig_parts = parts
                self.sigma_sec = max(mix_vol(parts, self.cfg.vol_short_weight) * self.cfg.vol_scale,
                                     self.cfg.vol_floor_min / math.sqrt(60.0))

    def _refresh_windows(self, now):
        missing = [h for h in self.cfg.horizons if h not in self.active or now >= self.active[h].info.we]
        if not missing and now - self.last_discovery < 30:
            return
        if missing and now - self.last_discovery < 3:      # don't hammer while a window is unlisted
            return
        self.last_discovery = now
        found = self.api.find_active(now)
        for h in self.cfg.horizons:
            m = found.get(h)
            if not m:
                continue
            cur = self.active.get(h)
            if cur and cur.info.slug == m.slug:
                if m.ptb is not None:
                    cur.info.ptb = m.ptb
                continue
            if cur:
                self._end_window(h, now)
            ws = self.restored.pop(m.slug, None)             # a position carried over from the last run
            if ws is None:
                ws = WindowState(m, joined_at=now, joined_late=(now - m.ws) > 5.0)
            else:
                ws.info, ws.joined_at, ws.joined_late = m, now, True
            self.active[h] = ws
            if m.index_symbol and m.index_symbol.upper() != "BRTI":
                self.warn(f"{h}: settles on '{m.index_symbol}', not BRTI - probabilities are a proxy.")

        for slug, ws in list(self.restored.items()):         # carried-over windows that have since closed
            if now >= ws.info.we:
                ws.ended_at = now
                self.pending.append(ws)
                del self.restored[slug]

    # ---- pick up where the last run stopped ----------------------------
    @staticmethod
    def _event_text(r):
        def num(x):
            try:
                return float(x) if x not in ("", None) else None
            except ValueError:
                return None
        side, ev = r.get("side", ""), r.get("event", "")
        sc = lambda x: None if x is None or side not in ("UP", "DOWN") else side_chance(x, side)
        f = lambda x: "-" if x is None else format(x, ".0%")
        chance, model, mkt = sc(num(r.get("chance"))), sc(num(r.get("model"))), sc(num(r.get("market")))
        if ev == "CALL":
            text = f"CALL {side}: {f(chance)} chance of {side} (model {f(model)}, market {f(mkt)})"
        elif ev == "RELEASE":
            text = f"RELEASE {side}: chance of {side} fell to {f(chance)}"
        elif ev == "BUY" and "maker" in (r.get("note") or ""):
            text = f"FILLED {side} {num(r.get('shares')) or 0:.2f} @ {num(r.get('price')) or 0:.1%} (maker)"
        elif ev == "BUY":
            text = (f"BUY {side} {num(r.get('shares')) or 0:.2f} @ {num(r.get('price')) or 0:.1%} "
                    f"(fee ${num(r.get('fee')) or 0:.2f}, chance {f(chance)})")
        elif ev == "ORDER":
            text = f"ORDER {side} {num(r.get('shares')) or 0:.2f} @ {num(r.get('price')) or 0:.1%} (maker bid)"
        elif ev == "CANCEL":
            what = "resting order" if "LIVE" in (r.get("note") or "") else "bid"
            text = f"CANCEL {side} {what} @ {num(r.get('price')) or 0:.1%} ({r.get('note', '')})"
        elif ev in ("HALT", "PAUSE"):
            text = ("TRADING HALTED: " if ev == "HALT" else "paused until tomorrow: ") + (r.get("note") or "")
        elif ev == "TRY":
            text = f"paper order: {side} {num(r.get('shares')) or 0:g} @ up to {num(r.get('price')) or 0:.1%}"
        elif ev == "SELL":
            text = f"SOLD {side} {num(r.get('shares')) or 0:g} @ {num(r.get('price')) or 0:.1%} ({r.get('note', '')})"
        elif ev == "CUT":
            text = f"SELLING real {side} {num(r.get('shares')) or 0:g} ({r.get('note', '')})"
        elif ev == "REST":
            text = f"RESTING real limit order: {side} {num(r.get('shares')) or 0:g} @ {num(r.get('price')) or 0:.1%}"
        elif ev == "SEND":
            m = re.search(r"retry \d+ of \d+", r.get("note") or "")
            text = (f"SENT real order: {side} {num(r.get('shares')) or 0:g} @ up to {num(r.get('price')) or 0:.1%}"
                    + (f" ({m.group(0)})" if m else ""))
        elif ev in ("NOFILL", "ERROR"):
            text = ("order not filled" if ev == "NOFILL" else "ORDER ERROR") + f": {r.get('note', '')}"
        elif ev == "SETTLE":
            text = f"SETTLED {(r.get('note') or '').split(' ')[0]}: {side} position P&L ${num(r.get('pnl')) or 0:+.2f}"
        else:
            return None
        t = parse_ts(r["time_utc"]) if r.get("time_utc") else 0
        return f"{hms(t)} {r.get('horizon', ''):>3}  {text}"

    def resolve_orphans(self):
        """A resting order left when the bot last stopped: ask Polymarket how it ended (cancelling it if still open)
        and write that down, so the restart neither halts for nothing nor misses a fill."""
        if self.broker is None or getattr(self.broker, "rehearse", False):
            return []
        path = os.path.join(self.cfg.out_dir, "events.csv")
        if not os.path.exists(path):
            return []
        with open(path, newline="", encoding="utf-8") as f:
            evs = list(csv.DictReader(f))
        rested = {}
        for e in evs:
            m = re.search(r"resting limit order (\S+);", e.get("note") or "")
            if e.get("event") == "REST" and m:
                rested[m.group(1)] = e
            elif e.get("event") in ("CANCEL", "NOFILL", "ERROR", "BUY"):
                if e.get("event") == "ERROR" and "not confirmed" in (e.get("note") or ""):
                    continue                                      # its ending was never seen: ask Polymarket
                for oid in list(rested):
                    if oid in (e.get("note") or ""):
                        rested.pop(oid)
        def num(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        now, done = self.clock.time(), []
        for oid, e in rested.items():
            side, limit = e.get("side"), num(e.get("price"))
            ws = SimpleNamespace(info=SimpleNamespace(horizon=e.get("horizon"), slug=e.get("slug")),
                                 p_final=None, p_model=None, p_mkt=None)
            try:
                try:
                    self.broker.cancel(oid, e.get("slug"))
                except Exception:
                    pass                                          # already ended: the lookup says how
                res = self.broker.check(oid, side, limit)
            except Exception as ex:
                done.append((oid, f"could not be looked up ({type(ex).__name__})"))
                continue
            if not res.get("final"):
                done.append((oid, "still open after a cancel"))
                continue
            got, px, fee = float(res.get("filled") or 0.0), res.get("price"), res.get("fee") or 0.0
            if got > 0 and px is not None:
                self.rec.event(now, ws, "BUY", side=side, price=px, shares=round(got, 4), fee=fee,
                               note=f"maker; LIVE resting order {oid}; limit {limit:.3f}; found filled after a restart")
                done.append((oid, f"had filled {got:g} @ {px:.1%}: added to the record"))
            else:
                self.rec.event(now, ws, "CANCEL", side=side, price=limit, shares=num(e.get("shares")) or "",
                               note=f"LIVE resting order {oid}: ended unfilled while the bot was stopped ({res.get('status', '')})")
                done.append((oid, "ended unfilled"))
        return done

    def restore(self):
        """Rebuild the scorecard, recent windows, log, paper cash and still-open positions from windows.csv and
        events.csv, so a restart carries on instead of starting fresh."""
        cfg = self.cfg

        def read(name):
            path = os.path.join(cfg.out_dir, name)
            if not os.path.exists(path):
                return []
            with open(path, newline="", encoding="utf-8") as f:
                return list(csv.DictReader(f))

        def num(x, d=None):
            try:
                return float(x) if x not in ("", None) else d
            except ValueError:
                return d

        wins, evs = read("windows.csv"), read("events.csv")
        settled, realized = set(), 0.0
        for r in wins:
            settled.add(r.get("slug"))
            pnl = num(r.get("pnl"))
            if r.get("traded_side") and pnl is not None:
                realized += pnl
            st = self.stats.get(r.get("horizon"))
            if st is None:
                continue
            st.windows += 1
            if r.get("first_call"):
                st.called += 1
                st.correct += int(num(r.get("first_call_correct", r.get("call_correct")), 0))
            if r.get("traded_side"):
                st.trades += 1
                st.staked += num(r.get("shares"), 0) * num(r.get("avg_px"), 0) + num(r.get("fee"), 0)
                st.fees += num(r.get("fee"), 0)
                st.pnl += pnl or 0.0
            if r.get("order_status"):
                st.orders += 1
                st.fills += int(r["order_status"] in ("filled", "partial"))
            c50 = num(r.get("chance_at_50pct"))
            if c50 is not None:
                st.brier += (c50 - (1.0 if r.get("outcome") == "UP" else 0.0)) ** 2
                st.brier_n += 1
        for r in reversed(wins):                                  # newest first, like the live list
            if r.get("horizon") in self.stats and len(self.history) < self.history.maxlen and r.get("window_start"):
                fcc = r.get("first_call_correct", "")
                self.history.append({
                    "ws": parse_ts(r["window_start"]), "we": parse_ts(r["window_end"]), "horizon": r["horizon"],
                    "outcome": r.get("outcome", ""), "first_call": r.get("first_call", ""),
                    "first_call_correct": int(num(fcc, 0)) if fcc != "" else "",
                    "side": r.get("traded_side", ""), "shares": num(r.get("shares")), "avg": num(r.get("avg_px")),
                    "liquidity": r.get("liquidity") or ("taker" if r.get("traded_side") else ""),
                    "fee": num(r.get("fee")),
                    "gross": num(r.get("gross_pnl")) if r.get("gross_pnl") not in ("", None) else
                             (None if num(r.get("pnl")) is None else round(num(r.get("pnl")) + num(r.get("fee"), 0), 2)),
                    "exit": r.get("exit", ""), "pnl": num(r.get("pnl")),
                    "order_status": r.get("order_status", ""), "order_px": num(r.get("order_px"))})

        first_call = {}
        for e in evs:
            if e.get("event") == "CALL" and e.get("slug") not in first_call and e.get("side") in ("UP", "DOWN"):
                first_call[e["slug"]] = (e["side"], side_chance(num(e.get("chance")), e["side"]), parse_ts(e["time_utc"]))
        now = self.clock.time()
        open_cost, n_open, n_lost = 0.0, 0, 0
        buys = {}                                                 # a resting order can fill in several pieces
        for e in evs:
            if e.get("event") == "BUY" and e.get("slug") not in settled:
                sh, px, fe = num(e.get("shares"), 0.0), num(e.get("price"), 0.0), num(e.get("fee"), 0.0)
                if e["slug"] in buys:
                    b = buys[e["slug"]]
                    b["shares"] = str(num(b["shares"], 0.0) + sh)
                    b["_cost"] += sh * px
                    b["fee"] = str(num(b["fee"], 0.0) + fe)
                else:
                    buys[e["slug"]] = dict(e, _cost=sh * px)
        for b in buys.values():
            b["price"] = str(b["_cost"] / num(b["shares"], 1.0)) if num(b["shares"], 0.0) else b["price"]
        sells = {}                                                # positions partly or wholly sold before the stop
        for e in evs:
            if e.get("event") == "SELL" and e.get("slug") in buys:
                d = sells.setdefault(e["slug"], [0.0, 0.0])
                d[0] += num(e.get("shares"), 0.0)
                d[1] += num(e.get("pnl"), 0.0)
        for e in buys.values():                                   # bought, but never settled: the bot was stopped first
            if e.get("slug") in self.restored:
                continue
            shares, px, fee = num(e.get("shares"), 0.0), num(e.get("price"), 0.0), num(e.get("fee"), 0.0)
            cost = shares * px + fee
            sold, cut_pnl = sells.get(e["slug"], (0.0, 0.0))
            if sold:
                frac = min(1.0, sold / shares) if shares else 1.0
                cost, shares = cost * (1 - frac), max(0.0, shares - sold)
                realized += cut_pnl                               # already in the cash; settles with the window
            open_cost += cost
            st = self.stats.get(e.get("horizon"))
            if st is not None:
                st.trades += 1
                st.staked += cost
            try:
                info = self.api.market(e["slug"])
            except Exception:                                     # offline, or the market no longer exists
                info = None
            if info is None:
                self.unresolved_cost += cost
                n_lost += 1
                continue
            ws = WindowState(info, entered=True)
            ws.position = Position(e["side"], shares, px, fee, cost, parse_ts(e["time_utc"])) if shares > 1e-6 else None
            ws.cut_pnl = cut_pnl
            liq = "maker" if "maker" in (e.get("note") or "") else "taker"
            ws.trade = {"side": e["side"], "shares": num(e.get("shares"), 0.0), "avg": px, "fee": fee,
                        "exit": ("cut" if shares <= 1e-6 else "part cut") if sold else "", "liquidity": liq}
            if liq == "maker":
                ws.order_status, ws.order_px = "filled", px
            fc = first_call.get(e["slug"])
            if fc:
                ws.first_call, ws.first_call_p, ws.first_call_left = fc[0], fc[1], max(0.0, info.we - fc[2])
            n_open += 1
            if now < info.we:
                self.restored[e["slug"]] = ws
            else:
                ws.ended_at = now
                self.pending.append(ws)
        self.cash = cfg.bankroll + realized - open_cost
        for r in wins:
            if r.get("traded_side") and num(r.get("pnl")) is not None and r.get("window_end"):
                k = day_key(parse_ts(r["window_end"]))
                self.day_pnl[k] = self.day_pnl.get(k, 0.0) + num(r.get("pnl"))
        self.halted = read_state(cfg.out_dir).get("halted", "") or ""
        answered, filled, acked = {}, set(), set()
        for e in evs:                                             # real orders: every one must have a result
            ev, slug = e.get("event"), e.get("slug")
            if ev == "TRY":
                self.paper_tries += 1
                self.sent[slug] = self.sent.get(slug, 0) + 1
                self.done.add(slug)
                continue
            if ev == "BUY" and "paper, arrived" in (e.get("note") or ""):
                self.paper_fills += 1
            if ev == "RESUME":                                    # you checked and resumed: earlier unknowns are settled
                acked |= {s for s, n in self.sent.items() if answered.get(s, 0) < n}
                continue
            if ev == "SEND":
                self.sent[slug] = self.sent.get(slug, 0) + 1
                self.last_send[slug] = parse_ts(e["time_utc"]) or 0.0
                k = day_key(parse_ts(e["time_utc"]))
                self.sends_by_day[k] = self.sends_by_day.get(k, 0) + 1
            elif ev in ("NOFILL", "ERROR") or (ev == "BUY" and "LIVE" in (e.get("note") or "")):
                answered[slug] = answered.get(slug, 0) + 1
                if ev != "NOFILL":
                    filled.add(slug)                              # a fill or an error ends that window's tries
                self.live_fills += int(ev == "BUY")
        self.done |= filled
        lost = sorted(s for s, n in self.sent.items() if answered.get(s, 0) < n and s not in filled and s not in acked)
        rested = {}
        for e in evs:                                             # hybrid: a resting order with no recorded end
            m = re.search(r"resting limit order (\S+);", e.get("note") or "")
            if e.get("event") == "REST" and m:
                rested[m.group(1)] = e
            elif e.get("event") == "RESUME":
                rested.clear()
            elif e.get("event") in ("CANCEL", "NOFILL", "ERROR", "BUY"):
                for oid in list(rested):
                    if oid in (e.get("note") or ""):
                        rested.pop(oid)
        self.orphans = list(rested.items())
        if self.orphans and self.broker is not None and not self.halted:
            oid = self.orphans[-1][0]
            self.halt(now, f"resting order {oid} was left when the bot stopped. It expires by itself before its window "
                           f"closed, but check your Polymarket account for a fill, then restart with --resume")
        if lost and self.broker is not None and not self.halted:
            self.halt(now, f"an order sent for {lost[-1]} has no recorded result (the bot stopped mid-order). "
                           f"Check your Polymarket account, run makawan_reconcile.py, then restart with --resume")

        for r in evs[-self.events.maxlen:]:                       # the calls-and-trades log, newest on top
            text = self._event_text(r)
            if text:
                self.event_seq += 1
                self.events.appendleft((self.event_seq, text))
        if wins or evs:
            msg = (f"Picked up from {cfg.out_dir}/: {len(wins)} settled windows, paper P&L ${realized:+,.2f}"
                   f"{f', {n_open} open position(s) carried over' if n_open else ''}.")
            if n_lost:
                msg += f" {n_lost} earlier position(s) could not be looked up and stay counted at cost."
                self.warn(f"{n_lost} position(s) from an earlier run could not be settled (market not found).")
            self.notes.append(msg)
        return {"windows": len(wins), "realized": realized, "open": n_open, "unresolved": n_lost}

    # ---- per-tick ------------------------------------------------------
    # ---- risk guard ------------------------------------------------------
    def floor(self):
        return self.cfg.bankroll * (1.0 - self.cfg.stop_loss_frac) if self.cfg.stop_loss_frac > 0 else None

    def halt(self, now, reason):
        """Stop opening trades for good (until the bot is restarted with --resume). Open positions still settle."""
        if self.halted:
            return
        self.halted = reason
        for w in list(self.active.values()):
            if w.order:
                self._cancel_order(w, now, "trading halted")
            if w.live_order:
                self._end_live_order(w, now, "trading halted", wait=True)
        self.rec.system(now, "HALT", reason)
        write_state(self.cfg.out_dir, halted=reason, halted_at=iso(now))
        self.event_seq += 1
        self.events.appendleft((self.event_seq, f"{hms(now)}      TRADING HALTED: {reason}"))

    def guard_check(self, now):
        """Called every tick: halt at the stop-loss floor, pause for the day at the daily loss limit."""
        fl = self.floor()
        if not self.halted and fl is not None and self.equity() <= fl + 1e-9:
            self.halt(now, f"stop-loss: equity ${self.equity():,.2f} reached the ${fl:,.2f} floor "
                           f"({self.cfg.stop_loss_frac:.0%} of the ${self.cfg.bankroll:,.0f} start is lost)")
        today = day_key(now)
        lim = self.cfg.daily_loss_limit
        if lim > 0 and self.day_paused != today and self.day_pnl.get(today, 0.0) <= -lim + 1e-9:
            self.day_paused = today
            for w in list(self.active.values()):
                if w.order:
                    self._cancel_order(w, now, "daily loss limit")
                if w.live_order:
                    self._end_live_order(w, now, "daily loss limit", wait=True)
            self.rec.system(now, "PAUSE", f"daily loss limit: down ${-self.day_pnl[today]:,.2f} today (limit ${lim:,.2f})")
            self.event_seq += 1
            self.events.appendleft((self.event_seq, f"{hms(now)}      paused until tomorrow: down "
                                                    f"${-self.day_pnl[today]:,.2f} today (limit ${lim:,.2f})"))

    def resting(self, skip=None):
        """Dollars in unfilled maker bids that could still fill (other than `skip`'s own)."""
        return (sum(w.order["shares"] * w.order["px"] for w in self.active.values() if w.order and w is not skip)
                + sum((o["qty"] - o["filled"]) * o["limit"] for w in self.active.values()
                      if (o := w.live_order) and w is not skip))

    def guard_blocks(self, now, stake, w=None):
        """Reason a new trade of `stake` dollars is not allowed right now, or ''.
        Judged on the worst case: every open trade and resting bid loses, and so does this one."""
        if self.halted:
            return "trading halted (stop-loss)"
        if self.cfg.daily_loss_limit > 0 and self.day_paused == day_key(now):
            return "paused for today (daily loss limit)"
        fl = self.floor()
        if fl is None:
            return ""
        resting = self.resting(w)
        if self.cash - resting - stake >= fl - 1e-9:
            return ""
        if self.equity() - self.cash <= 1e-9 and resting <= 1e-9:      # nothing open: this is final
            self.halt(now, f"stop-loss: the account (${self.equity():,.2f}) cannot take another ${stake:,.2f} trade "
                           f"without risking the ${fl:,.2f} floor")
            return "trading halted (stop-loss)"
        return "waiting: open trades could already take the account to its stop"

    def guard_status(self):
        today = day_key(self.clock.time())
        return {"halted": self.halted, "floor": self.floor(), "stop_loss_frac": self.cfg.stop_loss_frac,
                "daily_loss_limit": self.cfg.daily_loss_limit, "today_pnl": round(self.day_pnl.get(today, 0.0), 2),
                "paused_today": self.day_paused == today and self.cfg.daily_loss_limit > 0}

    def guard_note(self):
        if self.halted:
            return f"TRADING HALTED: {self.halted}. No new trades until you restart with --resume."
        if self.cfg.daily_loss_limit > 0 and self.day_paused == day_key(self.clock.time()):
            return "Trading is paused until tomorrow: the daily loss limit was reached."
        return None

    def sleep_note(self):
        if not self.sleep_gaps:
            return None
        m = self.sleep_secs / 60.0
        return (f"This computer was asleep (or the bot was paused) {self.sleep_gaps} time(s), "
                f"{f'{m / 60:.1f} hours' if m >= 90 else f'{m:.0f} minutes'} in total, since the bot started. "
                f"Windows during those gaps were missed.")

    def tick(self):
        now = self.clock.time()
        if self._last_tick and now - self._last_tick > 30.0:      # the clock jumped: the computer slept
            gap = now - self._last_tick
            self.sleep_gaps += 1
            self.sleep_secs += gap
            self.event_seq += 1
            self.events.appendleft((self.event_seq, f"{hms(now)}      asleep or paused for {gap / 60:.0f} min; "
                                                    f"waiting {self.cfg.join_wait_s:.0f}s before new calls"))
            for w in self.active.values():                       # treat every window as freshly joined
                w.joined_at, w.joined_late, w.pend_state = now, True, None
        self._last_tick = now
        try:
            self._refresh_prices(now)
            self._refresh_windows(now)
            live = {}
            if self.stream is not None:
                self.stream.want([w.info.slug for w in self.active.values()])
                for h, w in self.active.items():
                    b = self.stream.get(w.info.slug)
                    if b is not None and (b.bids or b.asks):      # an empty streamed book is checked by polling
                        live[h] = b
            need = {h: w for h, w in self.active.items() if h not in live or self._due_crosscheck(w, now)}
            futs = {h: self.pool.submit(self.api.book, w.info.slug) for h, w in need.items()} if self.parallel else {}
            for h, w in list(self.active.items()):
                polled, w.book_error = None, ""
                if h in need:
                    try:
                        polled = futs[h].result() if self.parallel else self.api.book(w.info.slug)
                    except NotFound:
                        w.book_error = "Polymarket answered 'not found' for this market's order book"
                    except ApiError as e:
                        w.book_error = f"order book request failed ({e})"
                    except Exception as e:                        # one bad book must not stop the other windows
                        w.book_error = f"order book request failed ({type(e).__name__}: {str(e)[:80]})"
                if h in live:
                    if polled is not None:
                        self._crosscheck(w, live[h], polled, now)
                    self.stream_used += 1
                    book = live[h]
                else:
                    self.polled += 1
                    book = polled
                self._note_empty_book(w, now, book, "stream" if h in live else "poll")
                self._eval(w, now, book)
            for h, w in list(self.active.items()):
                if now >= w.info.we:
                    self._end_window(h, now)
            self._settle_pending(now)
            self._refresh_real_balance(now)
            self.guard_check(now)
            self.last_error = ""
        except (ApiError, OSError, ValueError, KeyError) as e:
            self.errors += 1
            self.last_error = f"{type(e).__name__}: {e}"
            if requests is not None and isinstance(e, requests.RequestException):
                time.sleep(1.0)

    def _note_empty_book(self, w, now, book, source):
        """Write down each time a window's order book goes empty (or fails), with what Polymarket actually sent,
        to book_debug.jsonl, so 'nobody quoting' can be checked against the website."""
        empty = book is None or not (book.bids or book.asks)
        if not empty:
            w.empty_since = None
            return
        if w.empty_since is not None and now - w.empty_logged < 60:
            return
        w.empty_since = w.empty_since or now
        w.empty_logged = now
        last404 = getattr(self.api, "last_404", None) or {}
        rec = {"time": iso(now), "time_et": local_iso(now), "slug": w.info.slug, "secs_left": int(w.info.we - now),
               "source": source, "error": w.book_error,
               "book_was_empty": book is not None and not (book.bids or book.asks),
               "not_found_headers": last404.get("headers") if last404.get("slug") == w.info.slug else None,
               "cache_busted": w.info.slug in (getattr(self.api, "bust", None) or ())}
        try:
            with open(os.path.join(self.cfg.out_dir, "book_debug.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    # ---- live stream safety check ---------------------------------------
    def _due_crosscheck(self, w, now):
        """Every 15 s per market, also poll the book so the stream can be checked against it."""
        if self.stream is None:
            return False
        return now - self._xcheck.get(w.info.slug, [0.0, 0])[0] >= 15.0

    def _crosscheck(self, w, streamed, polled, now):
        """The stream is newer than a polled book, so one disagreement means nothing. Five in a row (over a minute)
        means the stream is being read wrongly: switch it off and go back to polling."""
        rec = self._xcheck.setdefault(w.info.slug, [0.0, 0])
        rec[0] = now
        newest = self.stream.get(w.info.slug) or streamed
        same = any((b.bid, b.ask) == (polled.bid, polled.ask) for b in (streamed, newest))
        rec[1] = 0 if same else rec[1] + 1
        if rec[1] >= 5:
            self.stream.disabled = True
            self.warn("The live stream disagreed with the polled order book five checks in a row, so it was switched "
                      "off. The bot is back on polling.")

    def _refresh_real_balance(self, now, every=15.0):
        """Live only: read the real balance from Polymarket every 15 s (read-only)."""
        if self.broker is None or getattr(self.broker, "rehearse", False) or not hasattr(self.broker, "balance"):
            return
        if now - self._real_at < every:
            return
        self._real_at = now
        try:
            b = self.broker.balance()
        except Exception as e:
            if self.real is not None:
                self.real["error"] = f"{type(e).__name__}"
            return
        if b and b.get("balance") is not None:
            self.real = {"balance": b["balance"], "buying_power": b.get("buying_power"),
                         "open_orders": b.get("open_orders"), "at": now, "error": ""}

    def real_status(self):
        """Polymarket's balance next to the bot's own books, for the page."""
        if not self.real:
            return None
        open_cost = self.equity() - self.cash
        real_equity = self.real["balance"] + open_cost
        return {"balance": round(self.real["balance"], 2),
                "buying_power": None if self.real.get("buying_power") is None else round(self.real["buying_power"], 2),
                "open_positions": round(open_cost, 2), "equity": round(real_equity, 2),
                "books": round(self.equity(), 2), "diff": round(real_equity - self.equity(), 2),
                "age": round(self.clock.time() - self.real["at"]), "error": self.real.get("error", "")}

    def spot_estimate(self, now):
        """The index brought up to now with Coinbase: last index print + Coinbase's move since that print.
        Returns (estimate, Coinbase price) or (None, Coinbase price or None)."""
        if self.spot is None or self.S is None:
            return None, None
        px, age = self.spot.latest()
        if px is None or age is None or age > self.cfg.spot_max_age_s:
            return None, px
        ref_t = self.last_print_ts + self.cfg.spot_ref_offset_s
        if ref_t > time.time():                                   # not reached yet: the print can't be matched
            return None, px
        ref = self.spot.price_at(ref_t)
        if ref is None:
            return None, px
        move = px - ref
        if abs(move) > self.cfg.spot_max_move * self.S:
            return None, px
        return self.S + move, px

    def _drift(self, cfg, w, S):
        """Trend / fade: dollars to add to Bitcoin's price in the maths. With a positive drift_beta the move over
        the last `drift_lookback_s` is assumed to carry on in part for the rest of the window (trend following);
        with a negative one it is assumed to partly reverse (mean reversion). 0 when the option is off."""
        look = int(cfg.drift_lookback_s)
        if not cfg.drift_beta or look <= 0 or not self.last_print_ts:
            return 0.0
        past = None
        for back in range(0, 6):                                  # the print `look` seconds ago (or just before)
            past = self.prints.get(self.last_print_ts - look - back)
            if past is not None:
                break
        if past is None:
            return 0.0
        adj = cfg.drift_beta * (S - past) * min(1.0, max(0.0, w.tau) / look)
        cap = 0.005 * S                                           # never more than 0.5% of the price
        return max(-cap, min(cap, adj))

    def spot_status(self):
        if self.spot is None:
            return "off"
        return self.spot.status() + (" (used in the model)" if self.cfg.spot_use else " (logged only)")

    def stream_status(self):
        return "polling (1 request a second)" if self.stream is None else self.stream.status()

    def _eval(self, w: WindowState, now, book):
        info = w.info
        cfg = self.cfg.view(info.horizon)
        w.tau = info.we - now
        new_vol = book.volume if book is not None else None
        w.vol_delta = (new_vol - w.prev_volume) if (new_vol is not None and w.prev_volume is not None) else 0.0
        if new_vol is not None:
            w.prev_volume = new_vol
        w.book = book if (book and (book.bids or book.asks)) else None
        if info.ptb is None:
            w.note = "waiting for the price to beat"
            if now >= info.ws and now - w.last_poll >= 2.0:
                w.last_poll = now
                m = self.api.market(info.slug)
                if m and m.ptb is not None:
                    info.ptb = m.ptb
            return
        S = self.S
        if S is None or self.sig_parts is None:
            w.note = "warming up (collecting index history)"
            return
        stale = now - self.last_print_ts > 8
        L = int(cfg.avg_window_s)
        obs_avg = None
        if w.tau < L:
            T = int(info.we)
            obs = [self.prints[t] for t in range(T - L, int(now) + 1) if t in self.prints]
            if obs:
                obs_avg = sum(obs) / len(obs)
        sig = max(mix_vol(self.sig_parts, cfg.vol_short_weight) * cfg.vol_scale, cfg.vol_floor_min / math.sqrt(60.0))
        w.sigma_min = sig * math.sqrt(60.0)
        w.drift_adj = self._drift(cfg, w, S)
        w.p_model_index = chance_up(S + w.drift_adj, info.ptb, w.tau, sig, obs_avg, float(L), cfg.tail_w, cfg.tail_s2)
        w.spot_est, w.spot_px = self.spot_estimate(now)
        w.p_model_spot = (None if w.spot_est is None else
                          chance_up(w.spot_est + w.drift_adj, info.ptb, w.tau, sig, obs_avg, float(L),
                                    cfg.tail_w, cfg.tail_s2))
        w.p_model = w.p_model_spot if (cfg.spot_use and w.p_model_spot is not None) else w.p_model_index
        w.p_mkt = w.book.mid() if w.book else None
        w.p_final = blend(w.p_model, w.p_mkt, cfg.w_market, cfg.blend_mode, cfg.blend_a, cfg.blend_b, cfg.blend_c)
        if cfg.log_ticks and now - w.last_tick_log >= cfg.tick_log_s:
            w.last_tick_log = now
            self.rec.tick(now, w, S)
        if not w.hist or w.hist[-1][0] != int(now):
            w.hist.append((int(now), round(w.p_final, 4)))
        dur = info.we - info.ws
        elapsed = now - info.ws
        if w.snap50 is None and elapsed >= 0.5 * dur:
            w.snap50 = w.p_final

        if stale:
            w.note = f"index feed stale ({int(now - self.last_print_ts)}s)"
            return
        warm = max(cfg.warmup_min_s, cfg.warmup_frac * dur)
        join_left = (cfg.join_wait_s - (now - w.joined_at)) if w.joined_late else 0.0
        if elapsed < warm or join_left > 0:
            w.note = f"first call waits ({int(max(warm - elapsed, join_left)) + 1}s)"
            w.pend_state = None
        else:
            w.note = ""
            cand = next_call(w.call, w.p_final, cfg)
            if cand == w.call:
                w.pend_state = None
            elif w.pend_state is None or w.pend_state[0] != cand:
                w.pend_state, w.pend_since = (cand,), now
            if w.pend_state is not None and now - w.pend_since >= cfg.confirm_s:
                w.n_changes += 1
                side = cand or w.call
                sc = lambda x: None if x is None else side_chance(x, side)
                mk = "-" if w.p_mkt is None else format(sc(w.p_mkt), ".0%")
                self.rec.event(now, w, "CALL" if cand else "RELEASE", side=side)
                if cand:
                    self.log(now, w, f"CALL {cand}: {sc(w.p_final):.0%} chance of {cand} "
                                     f"(model {sc(w.p_model):.0%}, market {mk})")
                else:
                    self.log(now, w, f"RELEASE {side}: chance of {side} fell to {sc(w.p_final):.0%}")
                if cand and w.first_call is None:
                    w.first_call, w.first_call_p, w.first_call_left = cand, sc(w.p_final), w.tau
                w.call, w.pend_state = cand, None
        if w.live_order:                         # hybrid: a real limit order is resting for this window
            return self._work_live_order(w, now, cfg)
        if w.paper_order:                        # honest paper fills: has the order "arrived" yet?
            self._paper_arrive(w, now, cfg)
            if w.paper_order or w.position:
                return
        if w.book is None:                       # nobody is quoting: no market price, nothing to trade
            if w.order:
                w.value_note = "maker bid resting (order book empty)"
            else:
                w.value_note = (w.book_error + ", so no trade") if w.book_error else \
                               ("" if w.note else "order book empty: nobody is quoting, so no trade")
            return
        if not cfg.trade:
            w.value_note = ""
            return
        if w.position:
            why = self._cut_reason(w, now, cfg)
            if why:
                self._cut(w, now, cfg, why)
            else:
                w.value_note = "holding to settlement"
        elif w.order:
            self._work_order(w, now)
        elif w.call and not w.entered and join_left > 0:
            w.value_note = "waiting after a gap before trading"   # just woke or just joined: no buying on an old call
        elif w.call and not w.entered:
            self._maybe_buy(w, now)
        elif not w.call:
            w.value_note = ""

    def _side(self, w, side):
        return w.book if side == "UP" else w.book.flipped()

    @staticmethod
    def _model_gap(w, side, sb=None):
        """How far the model's chance for `side` sits above the market's. With a one-sided book there is no mid
        price, so the price you would pay (the ask) stands in for the market's view."""
        if w.p_model is None:
            return 0.0
        if w.p_mkt is not None:
            return side_chance(w.p_model, side) - side_chance(w.p_mkt, side)
        if sb is not None and sb.ask is not None:
            return side_chance(w.p_model, side) - sb.ask
        return 0.0

    def _stake(self, cfg, p_side, px, theta):
        if cfg.flat_stake > 0:
            return min(cfg.flat_stake, self.cash)
        return min(kelly_dollars(p_side, px, self.equity(), theta, cfg.kelly_frac),
                   cfg.max_trade_frac * self.equity(), self.cash)

    def _maybe_buy(self, w: WindowState, now):
        cfg = self.cfg.view(w.info.horizon)
        if self.halted or self.day_paused == day_key(now):
            block = self.guard_blocks(now, 0.0)
            if block:
                w.value_note = block
                return
        if w.info.horizon in cfg.no_trade_horizons:
            w.value_note = "tracking only: no trades on this window length"
            return
        limit = cfg.trade_last_s.get(w.info.horizon)
        if limit and w.tau > limit:                       # not yet: trades only open late in the window
            w.value_note = f"trading opens at {mmss(limit)} left"
            return
        side = w.call
        sb = self._side(w, side)
        p_side = w.p_final if side == "UP" else 1.0 - w.p_final
        ask = sb.ask
        ceiling = max_buy_price(p_side, cfg.fee_theta, cfg.min_edge)
        if ask is None:
            w.value_note = "no sellers"
            return
        floor_px = cfg.min_buy_px.get(w.info.horizon, 0.0)
        if ask < floor_px:
            w.value_note = f"skip: side under {floor_px:.0%}"
            return
        if cfg.max_buy_cap and ask > cfg.max_buy_cap + 1e-9:
            w.value_note = f"skip: {ask:.0%} is over the {cfg.max_buy_cap:.0%} price cap"
            return
        if ask > ceiling or ask >= 0.99:
            w.value_note = f"too pricey: {ask:.0%} > {ceiling:.0%}"
            return
        gap = self._model_gap(w, side, sb)
        if gap > cfg.max_model_gap + 1e-9:
            w.value_note = f"skip: model {gap * 100:.0f} pts above the market"
            return
        w.value_note = f"good up to {ceiling:.0%}"
        if w.tau < cfg.min_secs_left:
            return
        if cfg.exec_mode == "maker":
            return self._post_order(w, now, cfg, side, sb, p_side)
        stake = self._stake(cfg, p_side, ask, cfg.fee_theta)
        block = self.guard_blocks(now, stake)
        if block:
            w.value_note = block
            return
        if self.broker is not None and self._live_buy(w, now, cfg, side, p_side, ask, ceiling, stake):
            return
        if cfg.paper_delay_s > 0 and self.broker is None:
            return self._paper_send(w, now, cfg, side, ask, ceiling, stake)
        c = all_in_cost(ask, cfg.fee_theta)
        shares = math.floor(stake / c * 100) / 100.0
        if shares <= 0 or stake < cfg.min_trade_usd:
            return
        avg, got = walk(sb.asks, shares)
        if not got:
            return
        shares = math.floor(got * 100) / 100.0
        avg, got = walk(sb.asks, shares)
        for _ in range(6):                                # deeper levels cost more: shrink until it fits the stake
            cost = shares * avg + exch_fee(avg, shares, cfg.fee_theta)
            if shares <= 0 or cost <= stake + 0.005:
                break
            shares = math.floor(shares * stake / cost * 100) / 100.0 - 0.01
            avg, got = walk(sb.asks, max(shares, 0.0))
        if shares <= 0 or avg is None or ev_per_share(p_side, avg, cfg.fee_theta) < cfg.min_edge:
            w.value_note = "skip: edge gone at the fill price"
            return
        fee = exch_fee(avg, shares, cfg.fee_theta)
        cost = shares * avg + fee
        if cost > self.cash:
            return
        self.cash -= cost
        w.position = Position(side, shares, avg, fee, cost, now)
        w.entered = True
        w.trade = {"side": side, "shares": shares, "avg": avg, "fee": fee, "exit": "", "liquidity": "taker"}
        st = self.stats[w.info.horizon]
        st.trades += 1
        st.staked += cost
        self.rec.event(now, w, "BUY", side=side, price=avg, shares=shares, fee=fee,
                       note=f"taker; edge {ev_per_share(p_side, avg, cfg.fee_theta):+.3f}/sh")
        self.log(now, w, f"BUY {side} {shares:.2f} @ {avg:.1%} (fee ${fee:.2f}, chance {p_side:.0%})")

    # ---- honest paper fills: the price has to still be there when the order "arrives" ----
    def _paper_send(self, w, now, cfg, side, ask, ceiling, stake):
        """Paper version of the live order: a limit at the ask it saw + 1c (never above the edge or the cap),
        sized so the whole cost fits the stake. It fills, or misses, on a later tick (see _paper_arrive)."""
        tick = w.info.tick or 0.01
        slug = w.info.slug
        tries = self.sent.get(slug, 0)
        if slug in self.done or tries >= 1 + cfg.live_retries:
            w.entered = True
            return
        cap = cfg.max_buy_cap or 0.99
        limit = min(ceiling, ask + cfg.live_slip, cap, 0.99)
        limit = math.floor(limit / tick + 1e-9) * tick
        if limit < ask - 1e-9:
            return
        qty = math.floor((stake - 0.03) / all_in_cost(limit, cfg.fee_theta) * 100 + 1e-9) / 100.0
        if qty <= 0 or qty * limit < min(cfg.min_trade_usd, stake * 0.5):
            return
        self.sent[slug] = tries + 1
        self.paper_tries += 1
        w.entered = True
        w.paper_order = {"side": side, "limit": limit, "qty": qty, "at": now, "ask": ask, "stake": stake}
        self.rec.event(now, w, "TRY", side=side, price=limit, shares=qty,
                       note=f"paper order; fills only if the price is still there in {cfg.paper_delay_s:g}s; ask seen {ask:.3f}")
        w.value_note = f"paper order sent: {side} up to {limit:.0%}"

    def _paper_arrival_reject(self, w, now, cfg, o, sb, avg, got):
        """Re-validate entry rules when a delayed paper order arrives (returns rejection reason, or '')."""
        side = o["side"]
        if w.tau < cfg.min_secs_left:
            return f"only {w.tau:.0f}s left"
        if w.call != side:
            return f"call changed to {w.call or 'none'}"
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
        if w.p_final is None:
            return "no blended chance"
        p_side = side_chance(w.p_final, side)
        edge = ev_per_share(p_side, avg, cfg.fee_theta)
        if edge < cfg.min_edge - 1e-9:
            return f"edge {edge:+.3f} < {cfg.min_edge:.3f} at {avg:.3f}"
        gap = self._model_gap(w, side, sb)
        if gap > cfg.max_model_gap + 1e-9:
            return f"model gap {gap * 100:.0f} pts > {cfg.max_model_gap * 100:.0f}"
        return ""

    def _paper_arrive(self, w, now, cfg):
        """The paper order reaches the book: re-check entry filters and fill what is on offer at or under the limit, or miss."""
        o = w.paper_order
        if now - o["at"] < cfg.paper_delay_s - 1e-9:
            return
        w.paper_order = None
        slug, side = w.info.slug, o["side"]
        sb = self._side(w, side) if w.book is not None else None
        levels = [(px, q) for px, q in (sb.asks if sb else []) if px <= o["limit"] + 1e-9]
        avg, got = walk(levels, o["qty"])
        got = math.floor((got or 0.0) * 100 + 1e-9) / 100.0
        reason = ""
        fee = cost = 0.0
        if got <= 0 or avg is None:
            reason = "no sellers" if not (sb and sb.asks) else f"ask moved to {sb.asks[0][0]:.3f}"
        else:
            avg, got = walk(levels, got)
            reason = self._paper_arrival_reject(w, now, cfg, o, sb, avg, got)
            if not reason:
                fee = exch_fee(avg, got, cfg.fee_theta)
                cost = got * avg + fee
                if cost > self.cash + 1e-9:
                    reason = f"cost ${cost:.2f} > cash ${self.cash:.2f}"
        if reason:
            self.rec.event(now, w, "NOFILL", side=side, price=o["limit"], shares=o["qty"], note=f"paper: {reason}")
            self.log(now, w, f"paper order missed ({reason}; limit {o['limit']:.1%})")
            if self.sent.get(slug, 0) < 1 + cfg.live_retries:
                w.entered = False                                  # a retry is allowed
            else:
                self.done.add(slug)
                w.value_note = f"paper order missed ({reason}); window skipped"
            return
        self.done.add(slug)
        self.paper_fills += 1
        self.cash -= cost
        w.position = Position(side, got, avg, fee, cost, now)
        w.trade = {"side": side, "shares": got, "avg": avg, "fee": fee, "exit": "", "liquidity": "taker"}
        st = self.stats[w.info.horizon]
        st.trades += 1
        st.staked += cost
        self.rec.event(now, w, "BUY", side=side, price=avg, shares=got, fee=fee,
                       note=f"taker; paper, arrived {now - o['at']:.0f}s later; limit {o['limit']:.3f}; "
                            f"ask seen {o['ask']:.3f}; slip {(avg - o['ask']) * 100:+.1f}c")
        self.log(now, w, f"BUY {side} {got:.2f} @ {avg:.1%} (paper, limit {o['limit']:.1%}, fee ${fee:.2f})")

    # ---- live orders (real money; only with a broker from makawan_live.py) ----
    def _live_buy(self, w, now, cfg, side, p_side, ask, ceiling, stake):
        """Send ONE real, price-capped, fill-now-or-cancel order for this window and record what really happened.
        Returns True when the window is dealt with; False only in a rehearsal (then the paper fill follows)."""
        slug, tick = w.info.slug, (w.info.tick or 0.01)
        tries = self.sent.get(slug, 0)
        if slug in self.done:
            w.entered = True
            w.value_note = "this window's order is done"
            return True
        if tries >= 1 + cfg.live_retries:
            w.entered = True
            w.value_note = f"skipped: {tries} order(s) sent for this window, none filled" if tries > 1 else \
                           "an order was already sent for this window"
            return True
        if tries and now - self.last_send.get(slug, 0.0) < cfg.live_retry_wait_s:
            w.value_note = f"not filled; retry {tries} of {cfg.live_retries} in a moment"
            return True
        if now - self.last_print_ts > 5:
            w.value_note = "live: price feed too old to send an order"
            return True
        today = day_key(now)
        if self.sends_by_day.get(today, 0) >= cfg.live_max_orders_day:
            w.value_note = f"live: daily order limit ({cfg.live_max_orders_day}) reached"
            return True
        limit = min(ceiling, ask + cfg.live_slip, cfg.max_buy_cap or 0.99, 0.99)
        limit = math.floor(limit / tick + 1e-9) * tick                 # never round a buy limit up
        if limit < ask - 1e-9:
            return True
        step = w.info.min_qty if 0 < w.info.min_qty <= 1 else 1.0
        room = stake - 0.03                                            # each partial fill's fee is rounded to a cent
        qty = math.floor(room / all_in_cost(limit, cfg.fee_theta) / step + 1e-9) * step
        qty = round(qty, 2)
        if qty < max(step, w.info.min_qty) or qty * limit < cfg.min_trade_usd:
            w.value_note = "live: stake too small for one order at this price"
            return True
        if getattr(self.broker, "rehearse", False):
            try:
                self.broker.buy(slug, side, limit, qty, tick)
            except Exception as e:
                self.warn(f"Rehearsal: Polymarket did not accept the order format ({type(e).__name__}: {str(e)[:120]}).")
            return False
        self.sent[slug] = tries + 1
        self.last_send[slug] = now
        w.entered = True                                               # re-opened below only after a clean "not filled"
        self.sends_by_day[today] = self.sends_by_day.get(today, 0) + 1
        attempt = f"; retry {tries} of {cfg.live_retries}" if tries else ""
        self.rec.event(now, w, "SEND", side=side, price=limit, shares=qty,
                       note=f"LIVE limit, fill now or cancel; ask seen {ask:.3f}{attempt}")
        self.log(now, w, f"SENT real order: {side} {qty:g} @ up to {limit:.1%}")
        try:
            fill = self.broker.buy(slug, side, limit, qty, tick)
        except Exception as e:
            self.order_errors += 1
            self.done.add(slug)
            why = f"{type(e).__name__}: {str(e)[:160]}"
            self.rec.event(now, w, "ERROR", side=side, price=limit, shares=qty, note=why)
            self.log(now, w, f"ORDER ERROR: {why}")
            # a failed request may still have reached the exchange, so its result is not known
            self.halt(now, f"a real order for {slug} failed in transit and its result is unknown ({why}). Check your "
                           f"Polymarket account, run makawan_reconcile.py, then restart with --resume")
            return True
        self.order_errors = self.order_errors + 1 if fill.get("rejected") else 0
        if fill.get("status") == "unknown":
            self.done.add(slug)
            self.rec.event(now, w, "ERROR", side=side, price=limit, shares=qty, note=f"result unknown; order {fill.get('id', '')}")
            self.halt(now, f"the result of real order {fill.get('id', '')} for {slug} could not be confirmed. Check your "
                           f"Polymarket account, run makawan_reconcile.py, then restart with --resume")
            return True
        got, paid = float(fill.get("filled") or 0.0), fill.get("price")
        if got <= 0 or paid is None:
            self.rec.event(now, w, "NOFILL", side=side, price=limit, shares=qty,
                           note=f"LIVE order {fill.get('id', '')}: {fill.get('status', 'not filled')}")
            self.log(now, w, f"order not filled ({fill.get('status', 'cancelled')})")
            w.value_note = "live order was not filled"
            if self.order_errors >= 3:
                self.halt(now, f"Polymarket rejected three real orders in a row (last: {fill.get('status', '')[:140]}). "
                               f"Stopped so the cause can be fixed")
            elif not fill.get("rejected") and cfg.live_rest and not getattr(self.broker, "rehearse", False):
                self.done.add(slug)
                self._rest_live_order(w, now, cfg, side, limit, qty, tick)
            elif not fill.get("rejected") and self.sent[slug] < 1 + cfg.live_retries:
                w.entered = False                                      # a confirmed miss: free to try again
                w.value_note = f"not filled; retry {self.sent[slug]} of {cfg.live_retries} in {cfg.live_retry_wait_s:g}s"
            else:
                self.done.add(slug)
            return True
        fee = fill.get("fee")
        if fee is None or fee <= 0:
            fee = exch_fee(paid, got, cfg.fee_theta)
        cost = got * paid + fee
        self.done.add(slug)
        self.cash -= cost
        self.live_fills += 1
        self.session_fills += 1
        w.position = Position(side, got, paid, fee, cost, now)
        w.trade = {"side": side, "shares": got, "avg": paid, "fee": fee, "exit": "", "liquidity": "taker"}
        st = self.stats[w.info.horizon]
        st.trades += 1
        st.staked += cost
        self.rec.event(now, w, "BUY", side=side, price=paid, shares=got, fee=fee,
                       note=f"taker; LIVE order {fill.get('id', '')}; limit {limit:.3f}; ask seen {ask:.3f}; "
                            f"slip {(paid - ask) * 100:+.1f}c")
        self.log(now, w, f"BUY {side} {got:g} @ {paid:.1%} REAL (fee ${fee:.2f}, limit {limit:.1%})")
        if paid > limit + tick + 1e-9 or cost > stake * 1.25 + 0.05:
            self.halt(now, f"a real fill came back worse than its limit ({got:g} @ {paid:.3f}, limit {limit:.3f}, "
                           f"cost ${cost:.2f} for a ${stake:.2f} stake). Stopped so you can check it")
        elif cfg.live_max_trades and self.session_fills >= cfg.live_max_trades:
            self.halt(now, f"reached the limit of {cfg.live_max_trades} real trade(s) you set with --max-trades. "
                           f"Check the fill against Polymarket, then restart with --resume to continue")
        return True

    # ---- cutting losses ---------------------------------------------------
    def _side_bids(self, w, side):
        """[(price, shares)] someone will pay for `side`, best first. DOWN's buyers are UP's sellers."""
        b = w.book
        if b is None:
            return []
        if side == "UP":
            return list(b.bids)
        return [(round(1.0 - px, 6), q) for px, q in b.asks]

    def _cut_reason(self, w, now, cfg):
        """Why the position should be sold now, or ''."""
        pos = w.position
        if not (cfg.cut_on_flip or cfg.cut_on_value) or pos is None or w.tau < cfg.cut_min_secs_left:
            return ""
        if w.cut_tries >= cfg.cut_max_tries or self.halted:
            return ""
        if now - self.last_print_ts > 5:
            return ""
        bids = self._side_bids(w, pos.side)
        if not bids:
            return ""
        bid = bids[0][0]
        worth = bid - fee_per_share(bid, cfg.fee_theta)
        paid = (pos.cost / pos.shares) if pos.shares else 0.0
        if cfg.cut_only_losing and worth >= paid:
            return ""                                             # winning (or level): hold to settlement
        losing = f"; losing: {worth:.1%} back vs {paid:.1%} paid" if cfg.cut_only_losing else ""
        if cfg.cut_on_flip and w.call and w.call != pos.side:
            return f"the call turned {w.call}{losing}"
        if cfg.cut_on_value and w.p_model is not None:
            mine = side_chance(w.p_model, pos.side)
            if worth > mine + cfg.cut_margin:
                return (f"the bid ({bid:.0%}, {worth:.1%} after the fee) is worth more than the {mine:.0%} chance in "
                        f"the maths{losing}")
        return ""

    def _cut(self, w, now, cfg, why):
        pos = w.position
        bids = self._side_bids(w, pos.side)
        best = bids[0][0]
        floor_px = max(0.01, best - cfg.cut_slip)
        w.cut_tries += 1
        if self.broker is not None and not getattr(self.broker, "rehearse", False):
            return self._cut_live(w, now, cfg, why, floor_px, best)
        if self.broker is not None:                                    # rehearsal: ask Polymarket, then paper
            try:
                self.broker.sell_preview(w.info.slug, pos.side, floor_px, pos.shares, w.info.tick or 0.01)
            except Exception as e:
                self.warn(f"Rehearsal: Polymarket did not accept a sell order ({type(e).__name__}: {str(e)[:100]}).")
        left, got, proceeds, fee = pos.shares, 0.0, 0.0, 0.0                # paper: sell into the bids it sees
        for px, q in bids:
            if px < floor_px or left <= 1e-9:
                break
            take = min(q, left)
            got += take
            proceeds += take * px
            fee += fee_per_share(px, cfg.fee_theta) * take
            left -= take
        if got <= 0:
            return
        self._book_sale(w, now, got, proceeds / got, round(fee, 2), why, "paper")

    def _cut_live(self, w, now, cfg, why, floor_px, best):
        pos = w.position
        self.rec.event(now, w, "CUT", side=pos.side, price=floor_px, shares=pos.shares,
                       note=f"LIVE sell, fill now or cancel; bid seen {best:.3f}; {why}")
        self.log(now, w, f"SELLING real {pos.side} {pos.shares:g} @ no less than {floor_px:.1%}: {why}")
        try:
            fill = self.broker.sell(w.info.slug, pos.side, floor_px, pos.shares, w.info.tick or 0.01)
        except Exception as e:
            why2 = f"{type(e).__name__}: {str(e)[:160]}"
            self.rec.event(now, w, "ERROR", side=pos.side, price=floor_px, shares=pos.shares, note=f"sell: {why2}")
            self.halt(now, f"a real SELL for {w.info.slug} failed in transit and its result is unknown ({why2}). Check "
                           f"your Polymarket position, run makawan_reconcile.py, then restart with --resume")
            return
        if fill.get("status") == "unknown":
            self.rec.event(now, w, "ERROR", side=pos.side, price=floor_px, shares=pos.shares,
                           note=f"sell result unknown; order {fill.get('id', '')}")
            self.halt(now, f"the result of real sell order {fill.get('id', '')} could not be confirmed. Check your "
                           f"Polymarket position, then restart with --resume")
            return
        got, px = float(fill.get("filled") or 0.0), fill.get("price")
        if got <= 0 or px is None:
            self.rec.event(now, w, "NOFILL", side=pos.side, price=floor_px, shares=pos.shares,
                           note=f"LIVE sell order {fill.get('id', '')}: {fill.get('status', 'not filled')}")
            self.log(now, w, f"sell not filled ({fill.get('status', '')}); still holding")
            w.value_note = f"tried to sell ({why}); not filled, holding"
            return
        fee = fill.get("fee")
        if fee is None:
            fee = exch_fee(px, got, cfg.fee_theta)
        if px < floor_px - (w.info.tick or 0.01) - 1e-9:
            self._book_sale(w, now, min(got, pos.shares), px, fee, why, f"LIVE order {fill.get('id', '')}")
            self.halt(now, f"a real sell filled below its limit ({got:g} @ {px:.3f}, limit {floor_px:.3f})")
            return
        self._book_sale(w, now, min(got, pos.shares), px, fee, why, f"LIVE order {fill.get('id', '')}")

    def _book_sale(self, w, now, got, px, fee, why, how):
        pos = w.position
        frac = got / pos.shares
        cost_part = pos.cost * frac
        realized = got * px - fee - cost_part
        self.cash += got * px - fee
        w.cut_pnl += realized
        pos.shares = round(pos.shares - got, 6)
        pos.cost -= cost_part
        if w.trade:
            w.trade["fee"] = (w.trade.get("fee") or 0.0) + fee
            w.trade["exit"] = "cut" if pos.shares <= 1e-6 else "part cut"
        self.rec.event(now, w, "SELL", side=pos.side, price=px, shares=round(got, 4), fee=fee, pnl=realized,
                       note=f"cut; {how}; {why}")
        self.log(now, w, f"SOLD {pos.side} {got:g} @ {px:.1%} (fee ${fee:.2f}): {why}; P&L ${realized:+.2f}")
        w.value_note = f"sold: {why}"
        if pos.shares <= 1e-6:
            w.position = None
            w.entered = True

    # ---- hybrid: a real resting limit order after a missed fill-now order ----
    def _rest_live_order(self, w, now, cfg, side, limit, qty, tick):
        expire = w.info.we - cfg.live_rest_stop_s
        if expire - now < 5:
            return
        try:
            res = self.broker.rest(w.info.slug, side, limit, qty, tick, expire)
        except Exception as e:
            why = f"{type(e).__name__}: {str(e)[:160]}"
            self.rec.event(now, w, "ERROR", side=side, price=limit, shares=qty, note=f"resting order: {why}")
            self.halt(now, f"a resting order for {w.info.slug} failed in transit and may or may not exist ({why}). It "
                           f"expires by itself before the window closes. Check your account, then restart with --resume")
            return
        if not res.get("id"):
            self.rec.event(now, w, "NOFILL", side=side, price=limit, shares=qty,
                           note=f"resting order refused: {res.get('status', '')}")
            return
        w.live_order = {"id": res["id"], "side": side, "limit": limit, "qty": qty, "filled": 0.0, "cost": 0.0,
                        "fee": 0.0, "posted": now, "checked": now, "expires": expire}
        self.rec.event(now, w, "REST", side=side, price=limit, shares=qty,
                       note=f"LIVE resting limit order {res['id']}; expires {iso(expire)}")
        self.log(now, w, f"RESTING real limit order: {side} {qty:g} @ {limit:.1%} (until {mmss(cfg.live_rest_stop_s)} left)")
        w.value_note = f"limit order resting at {limit:.0%}"

    def _live_fill_update(self, w, now, res, cfg):
        """Book any new shares a resting order has filled since the last look."""
        o = w.live_order
        got, px = float(res.get("filled") or 0.0), res.get("price")
        new = round(got - o["filled"], 4)
        if new <= 1e-9 or px is None:
            return
        total_cost = got * px                                          # average over everything filled so far
        add_cost = max(0.0, total_cost - o["cost"])
        fee_total = res.get("fee") or 0.0
        add_fee = max(0.0, fee_total - o["fee"])
        o["filled"], o["cost"], o["fee"] = got, total_cost, fee_total
        add_px = add_cost / new
        self.cash -= add_cost + add_fee
        self.live_fills += int(w.position is None)
        self.session_fills += int(w.position is None)
        if w.position is None:
            w.position = Position(o["side"], new, add_px, add_fee, add_cost + add_fee, now)
            st = self.stats[w.info.horizon]
            st.trades += 1
        else:
            p = w.position
            p.shares, p.fee, p.cost = p.shares + new, p.fee + add_fee, p.cost + add_cost + add_fee
            p.avg_px = (p.cost - p.fee) / p.shares
        self.stats[w.info.horizon].staked += add_cost + add_fee
        w.trade = {"side": o["side"], "shares": w.position.shares, "avg": w.position.avg_px, "fee": w.position.fee,
                   "exit": "", "liquidity": "maker"}
        w.order_status, w.order_px = ("filled" if got >= o["qty"] - 1e-6 else "partial"), o["limit"]
        self.rec.event(now, w, "BUY", side=o["side"], price=add_px, shares=new, fee=add_fee,
                       note=f"maker; LIVE resting order {o['id']}; limit {o['limit']:.3f}")
        self.log(now, w, f"BUY {o['side']} {new:g} @ {add_px:.1%} REAL (resting limit order filled)")
        if add_px > o["limit"] + 0.011:
            self.halt(now, f"a resting order filled above its limit ({new:g} @ {add_px:.3f}, limit {o['limit']:.3f})")
        elif cfg.live_max_trades and self.session_fills >= cfg.live_max_trades and not self.halted:
            self.halt(now, f"reached the limit of {cfg.live_max_trades} real trade(s) you set with --max-trades. "
                           f"Check the fill against Polymarket, then restart with --resume to continue")

    def _work_live_order(self, w, now, cfg):
        o = w.live_order
        if not o:
            return
        if o.get("cancelling"):
            return self._finish_cancelling(w, now, cfg)
        side = o["side"]
        p_side = None if w.p_final is None else (w.p_final if side == "UP" else 1.0 - w.p_final)
        why = ""
        if w.tau <= cfg.live_rest_stop_s:
            why = "window closing"
        elif now - o["posted"] >= cfg.live_rest_max_s:
            why = f"not filled within {cfg.live_rest_max_s:g}s"
        elif w.call != side:
            why = "the call changed" if w.call else "the call was released"
        elif p_side is not None and ev_per_share(p_side, o["limit"], cfg.maker_fee_theta) < cfg.min_edge:
            why = "edge gone at the order's price"
        if why:
            return self._end_live_order(w, now, why, wait=(why == "window closing"))
        if now - o["checked"] < cfg.live_rest_poll_s:
            w.value_note = (f"limit order resting at {o['limit']:.0%}" +
                            (f", {o['filled']:g} of {o['qty']:g} filled" if o["filled"] else ""))
            return
        o["checked"] = now
        try:
            res = self.broker.check(o["id"], side, o["limit"])
        except Exception as e:
            w.value_note = f"could not look up the resting order ({type(e).__name__})"
            return                                                     # try again next time; it expires by itself
        self._live_fill_update(w, now, res, cfg)
        if res.get("final") and w.live_order:
            if o["filled"] <= 0:
                self.rec.event(now, w, "NOFILL", side=side, price=o["limit"], shares=o["qty"],
                               note=f"LIVE resting order {o['id']}: {res.get('status', 'ended')}")
            w.live_order = None
            w.order_status = w.order_status or "unfilled"

    def _end_live_order(self, w, now, why, wait=False):
        """Cancel a resting order and book whatever it filled before the cancel arrived. Polymarket handles a cancel
        a moment after accepting it, so an order can still show as open right after: then it is marked 'cancelling'
        and checked again each second (wait=True checks for up to 3 s now, for halts and closing windows)."""
        o = w.live_order
        if not o:
            return
        self._send_cancel(w, o)
        res = self._check_live(o)
        tries = 0
        while wait and not (res and res.get("final")) and tries < 6:
            time.sleep(0.5)
            tries += 1
            if tries == 3:
                self._send_cancel(w, o)
            res = self._check_live(o)
        if res:
            self._live_fill_update(w, now, res, self.cfg)
        if res and res.get("final"):
            return self._close_live_order(w, now, why)
        if not wait:
            o["cancelling"], o["cancel_at"], o["checked"] = why, now, now
            w.value_note = f"cancelling the resting order ({why})"
            return
        w.live_order = None
        self.rec.event(now, w, "ERROR", side=o["side"], price=o["limit"], shares=o["qty"],
                       note=f"resting order {o['id']}: cancel not confirmed ({why})")
        self.halt(now, f"resting order {o['id']} could not be confirmed cancelled. It expires by itself at "
                       f"{local_iso(o.get('expires', now))[11:19]}; check your account, then restart with --resume")

    def _send_cancel(self, w, o):
        try:
            self.broker.cancel(o["id"], w.info.slug)
        except Exception as e:
            self.warn(f"Cancel of resting order {o['id']} failed ({type(e).__name__}: {str(e)[:80]}); it expires by "
                      f"itself.")

    def _check_live(self, o):
        try:
            return self.broker.check(o["id"], o["side"], o["limit"])
        except Exception:
            return None

    def _close_live_order(self, w, now, why):
        o = w.live_order
        w.live_order = None
        self.rec.event(now, w, "CANCEL", side=o["side"], price=o["limit"], shares=o["qty"] - o["filled"],
                       note=f"LIVE resting order {o['id']}: {why}; {o['filled']:g} of {o['qty']:g} filled")
        self.log(now, w, f"resting order cancelled ({why}); {o['filled']:g} of {o['qty']:g} filled")
        if not o["filled"]:
            w.order_status, w.order_px = "unfilled", o["limit"]

    def _finish_cancelling(self, w, now, cfg):
        """Each second after a cancel: has Polymarket finished it? Send it again after 3 s; halt if it outlives its
        own expiry."""
        o = w.live_order
        if now - o["checked"] < 1.0:
            return
        o["checked"] = now
        res = self._check_live(o)
        if res:
            self._live_fill_update(w, now, res, cfg)
            if res.get("final") and w.live_order:
                return self._close_live_order(w, now, o["cancelling"])
        if now - o["cancel_at"] >= 3 and not o.get("recancelled"):
            o["recancelled"] = True
            self._send_cancel(w, o)
        if now > o.get("expires", o["cancel_at"]) + 10:
            w.live_order = None
            self.rec.event(now, w, "ERROR", side=o["side"], price=o["limit"], shares=o["qty"],
                           note=f"resting order {o['id']}: still open after its expiry ({o['cancelling']})")
            self.halt(now, f"resting order {o['id']} still shows as open after it should have expired. Check your "
                           f"account, then restart with --resume")

    # ---- maker mode (paper) --------------------------------------------
    @staticmethod
    def _maker_price(cfg, tick, sb, p_side):
        """Limit price for a resting bid: one tick above the best bid if that is still below the ask, never more
        than the edge allows (no fee for a maker), never at or above the ask (a maker order must not cross)."""
        ceiling = max_buy_price(p_side, cfg.maker_fee_theta, cfg.min_edge)
        bid, ask = sb.bid, sb.ask
        px = (bid or 0.0) + (tick if cfg.maker_improve else 0.0)
        if ask is not None:
            px = min(px, ask - tick)
        px = min(px, ceiling)
        px = math.floor(px / tick + 1e-9) * tick
        return round(px, 4), ceiling

    def _post_order(self, w, now, cfg, side, sb, p_side):
        px, ceiling = self._maker_price(cfg, w.info.tick, sb, p_side)
        floor_px = cfg.min_buy_px.get(w.info.horizon, 0.0)
        if px <= 0 or px < floor_px:
            w.value_note = "maker: no room to bid"
            return
        stake = self._stake(cfg, p_side, px, cfg.maker_fee_theta)
        block = self.guard_blocks(now, stake, w)
        if block:
            w.value_note = block
            return
        shares = math.floor(stake / px * 100) / 100.0
        if shares <= 0 or stake < cfg.min_trade_usd:
            return
        w.order = {"side": side, "px": px, "shares": shares, "posted": now, "stake": stake,
                   "alone": sb.bid is None or px > sb.bid + 1e-9}   # alone at a new best price (front of the queue)
        w.order_px, w.order_status = px, "resting"
        self.stats[w.info.horizon].orders += 1
        self.rec.event(now, w, "ORDER", side=side, price=px, shares=shares, note=f"maker limit bid; good up to {ceiling:.2f}")
        self.log(now, w, f"ORDER {side} {shares:.2f} @ {px:.1%} (maker bid, ask {sb.ask:.0%})" if sb.ask is not None
                 else f"ORDER {side} {shares:.2f} @ {px:.1%} (maker bid)")
        self._work_order(w, now)

    def _cancel_order(self, w, now, why):
        o = w.order
        if not o:
            return
        self.rec.event(now, w, "CANCEL", side=o["side"], price=o["px"], shares=o["shares"], note=why)
        self.log(now, w, f"CANCEL {o['side']} bid @ {o['px']:.1%} ({why})")
        w.order = None
        if w.order_status == "resting":
            w.order_status = "unfilled"

    def _work_order(self, w, now):
        """Each tick: cancel if the call changed or time ran out, fill if the market traded down to our bid,
        otherwise keep the bid at the right price. Paper fill rules, for a resting bid at P:
          (1) the best ask on our side drops to P or lower: fill up to the size offered at or below P;
          (2) the book's stats show new volume since the last poll and the last trade printed below P, or at P when
              our bid was alone at a new best price (front of the queue): fill up to the new volume.
        Only the LAST trade price between two polls is visible, so (2) is approximate. Maker mode is not allowed in
        --simulate: the fake market has no trades, so only rule (1) could apply and the result would be unfair."""
        cfg = self.cfg.view(w.info.horizon)
        o = w.order
        sb = self._side(w, o["side"])
        if w.call != o["side"]:
            return self._cancel_order(w, now, "call changed" if w.call else "call released")
        if w.tau < cfg.min_secs_left:
            return self._cancel_order(w, now, "too close to the close")
        if sb.ask is not None and sb.ask <= o["px"] + 1e-9:          # (1) the ask came down to our bid
            avail = sum(q for p, q in sb.asks if p <= o["px"] + 1e-9)
            shares = math.floor(min(o["shares"], avail) * 100) / 100.0
            if shares > 0:
                return self._fill_order(w, now, cfg, shares, "ask reached the bid")
        if w.vol_delta > 0 and sb.last_px is not None:                # (2) a seller traded at or through our bid
            hit = sb.last_px < o["px"] - 1e-9 or (o.get("alone") and sb.last_px <= o["px"] + 1e-9)
            if hit:
                shares = math.floor(min(o["shares"], w.vol_delta) * 100) / 100.0
                if shares > 0:
                    return self._fill_order(w, now, cfg, shares, f"trade printed at {sb.last_px * 100:.1f}c")
        if self._model_gap(w, o["side"], sb) > cfg.max_model_gap + 1e-9:
            return self._cancel_order(w, now, "model too far above the market")
        p_side = w.p_final if o["side"] == "UP" else 1.0 - w.p_final
        px, ceiling = self._maker_price(cfg, w.info.tick, sb, p_side)
        floor_px = cfg.min_buy_px.get(w.info.horizon, 0.0)
        if px <= 0 or px < floor_px:
            return self._cancel_order(w, now, "edge gone")
        if abs(px - o["px"]) > 1e-9:                     # re-price: a new price means a new place in the queue
            o["px"] = w.order_px = px
            if o.get("stake"):                           # same dollars at the new price, not the same share count
                o["shares"] = math.floor(o["stake"] / px * 100) / 100.0
            o["alone"] = sb.bid is None or px > sb.bid + 1e-9
        w.value_note = f"maker bid {px * 100:.0f}c resting (ask {'-' if sb.ask is None else f'{sb.ask * 100:.0f}c'})"

    def _fill_order(self, w, now, cfg, shares, how=""):
        o = w.order
        px = o["px"]
        fee = exch_fee(px, shares, cfg.maker_fee_theta) if cfg.maker_fee_theta else 0.0
        cost = shares * px + fee
        if cost > self.cash:
            shares = math.floor((self.cash - fee) / px * 100) / 100.0
            if shares <= 0:
                return self._cancel_order(w, now, "not enough cash")
            cost = shares * px + fee
        self.cash -= cost
        w.position = Position(o["side"], shares, px, fee, cost, now)
        w.entered = True
        w.order_status = "filled" if shares >= o["shares"] - 1e-9 else "partial"
        w.trade = {"side": o["side"], "shares": shares, "avg": px, "fee": fee, "exit": "", "liquidity": "maker"}
        st = self.stats[w.info.horizon]
        st.trades += 1
        st.fills += 1
        st.staked += cost
        p_side = w.p_final if o["side"] == "UP" else 1.0 - w.p_final
        self.rec.event(now, w, "BUY", side=o["side"], price=px, shares=shares, fee=fee,
                       note=f"maker {w.order_status}; {how}; waited {int(now - o['posted'])}s; chance {p_side:.2f}")
        self.log(now, w, f"FILLED {o['side']} {shares:.2f} @ {px:.1%} (maker, fee ${fee:.2f}, waited {int(now - o['posted'])}s)")
        w.order = None

    # ---- window end + settlement --------------------------------------
    def _end_window(self, h, now):
        w = self.active.pop(h, None)
        if not w:
            return
        w.ended_at = now
        w.final_call = w.call
        if w.order:
            self._cancel_order(w, now, "window closed unfilled")
        if w.live_order:
            self._end_live_order(w, now, "window closed", wait=True)
        self.pending.append(w)

    def _own_outcome(self, w: WindowState):
        T, L = int(w.info.we), int(self.cfg.avg_window_s)
        vals = [self.prints[t] for t in range(T - L, T) if t in self.prints]
        if len(vals) < L * 0.9 or w.info.ptb is None:
            return None
        return "UP" if sum(vals) / len(vals) >= w.info.ptb else "DOWN"

    def _settle_pending(self, now):
        for w in list(self.pending):
            if now - w.last_poll < 5.0:
                continue
            w.last_poll = now
            outcome = None
            m = self.api.market(w.info.slug)
            if m:
                w.info.ptb = w.info.ptb if w.info.ptb is not None else m.ptb
                if m.settle is not None and w.info.ptb is not None:
                    w.info.settle = m.settle
                    outcome = "UP" if m.settle >= w.info.ptb else "DOWN"
            if outcome is None:
                sv = self.api.settlement(w.info.slug)
                if sv is not None:
                    outcome = "UP" if sv >= 0.5 else "DOWN"
            if outcome is None and now - w.info.we > 600:
                outcome, w.provisional = self._own_outcome(w), True
                if outcome is None:                       # cannot tell how it ended: keep it counted at cost
                    if w.position:
                        self.unresolved_cost += w.position.cost
                        self.warn(f"{w.info.horizon} {w.info.slug}: could not find how this window settled; "
                                  f"its position stays counted at cost.")
                    self.pending.remove(w)
                    continue
            if outcome is None:
                continue
            self._finalize(w, outcome, now)
            self.pending.remove(w)

    def _finalize(self, w: WindowState, outcome, now):
        st = self.stats.setdefault(w.info.horizon, Stats())
        w.outcome = outcome
        st.windows += 1
        correct = ""
        if w.first_call:                      # score the first call: the final one is trivially right
            st.called += 1
            correct = int(w.first_call == outcome)
            st.correct += correct
        if w.snap50 is not None:
            y = 1.0 if outcome == "UP" else 0.0
            st.brier += (w.snap50 - y) ** 2
            st.brier_n += 1
        pnl = None
        if w.trade and w.trade.get("fee"):           # fees count when the trade settles, alongside its profit
            st.fees += w.trade["fee"]
        if not w.position and w.trade and w.trade.get("exit") == "cut":
            pnl = w.cut_pnl                           # sold before the end: the sale was the result
            st.pnl += pnl
            self.log(now, w, f"SETTLED {outcome}: position was sold earlier, P&L ${pnl:+.2f}")
        if w.position:
            pos = w.position
            payoff = pos.shares if pos.side == outcome else 0.0
            self.cash += payoff
            pnl = payoff - pos.cost + w.cut_pnl
            st.pnl += pnl
            if w.trade:
                w.trade["exit"] = "settled"
            self.rec.event(now, w, "SETTLE", side=pos.side, price=1.0 if pos.side == outcome else 0.0,
                           shares=pos.shares, pnl=pnl, note=f"{outcome}{' (provisional)' if w.provisional else ''}")
            self.log(now, w, f"SETTLED {outcome}: {pos.side} position P&L ${pnl:+.2f}")
            w.position = None
        elif pnl is None:
            tag = "no call" if not w.first_call else f"first call {w.first_call} {'right' if correct else 'wrong'}"
            self.log(now, w, f"SETTLED {outcome} - {tag}")
        t = w.trade or {}
        self.rec.window({
            "_ws": w.info.ws, "_we": w.info.we,
            "window_start": iso(w.info.ws), "window_end": iso(w.info.we), "horizon": w.info.horizon,
            "slug": w.info.slug, "price_to_beat": w.info.ptb,
            "settlement": w.info.settle if w.info.settle is not None else "",
            "outcome": outcome, "provisional": int(w.provisional),
            "first_call": w.first_call or "",
            "first_call_chance": "" if w.first_call_p is None else round(w.first_call_p, 4),
            "first_call_secs_left": "" if w.first_call_left is None else int(w.first_call_left),
            "first_call_correct": correct, "final_call": w.final_call or "", "call_changes": w.n_changes,
            "chance_at_50pct": "" if w.snap50 is None else round(w.snap50, 4),
            "traded_side": t.get("side", ""), "shares": t.get("shares", ""),
            "avg_px": "" if "avg" not in t else round(t["avg"], 4), "liquidity": t.get("liquidity", ""),
            "gross_pnl": "" if pnl is None else round(pnl + (t.get("fee") or 0), 2), "fee": t.get("fee", ""),
            "net_pnl": "" if pnl is None else round(pnl, 2),
            "exit": t.get("exit", ""), "pnl": "" if pnl is None else round(pnl, 2),
            "order_status": w.order_status, "order_px": "" if w.order_px is None else round(w.order_px, 4)})
        if pnl is not None and t:
            k = day_key(now)
            self.day_pnl[k] = self.day_pnl.get(k, 0.0) + pnl
        self.history.appendleft({
            "ws": w.info.ws, "we": w.info.we, "horizon": w.info.horizon, "outcome": outcome,
            "first_call": w.first_call or "", "first_call_correct": correct,
            "side": t.get("side", ""), "shares": t.get("shares"), "avg": t.get("avg"),
            "liquidity": t.get("liquidity", ""), "fee": t.get("fee"),
            "order_status": w.order_status, "order_px": w.order_px,
            "gross": None if pnl is None else round(pnl + (t.get("fee") or 0), 2),
            "exit": t.get("exit", ""), "pnl": None if pnl is None else round(pnl, 2)})


# --------------------------------------------------------------------------
# Offline simulator (same JSON shapes as the real API, so parsing is exercised)
# --------------------------------------------------------------------------
class RealClock:
    def time(self):
        return time.time()


class SimClock:
    def __init__(self, start):
        self.t = float(start)

    def time(self):
        return self.t

    def advance(self, dt=1.0):
        self.t += dt


class SimTransport:
    """Pretends to be gateway.polymarket.us: a random-walk index, BTC + one ETH decoy
    window set, books priced near (but not exactly at) fair value, and settlements."""

    def __init__(self, clock, seed=11, start_price=84250.0, sigma_min=25.0, history_s=4500, calm=False,
                 book_noise=0.02, vol_view=(0.9, 1.2), avg_s=1):
        self.clock, self.lock = clock, threading.Lock()
        self.rng = random.Random(seed)
        self.sig = sigma_min / math.sqrt(60.0)
        self.regime = 1.0
        self.calm = calm                      # True = constant volatility (for tests)
        self.avg_s = int(avg_s)                # settlement averaging window (1 = the last print, as on the exchange)
        self.book_noise, self.vol_view = book_noise, vol_view   # how wrong the fake market's prices are (0, (1,1) = perfectly priced)
        self.t0 = int(clock.time()) - int(history_s)
        self.last = self.t0
        self.prices = {self.t0: start_price}
        self.index = {}

    def _extend(self, upto):
        with self.lock:
            while self.last < upto:
                self.last += 1
                if self.last % 60 == 0 and not self.calm:
                    self.regime = max(0.6, min(2.2, math.exp(0.9 * math.log(self.regime) + self.rng.gauss(0, 0.12))))
                self.prices[self.last] = round(self.prices[self.last - 1] + self.rng.gauss(0, self.sig * self.regime), 2)

    def _avg(self, a, b):
        return sum(self.prices[t] for t in range(int(a), int(b))) / (int(b) - int(a))

    def _raw_market(self, asset, h, ws, now):
        dur = HORIZON_SECONDS[h]
        we = ws + dur
        dt = datetime.fromtimestamp(ws, timezone.utc)
        slug = f"cpc-{asset}-updown-{h}-{dt:%Y-%m-%d-%H%M}z"
        self.index[slug] = (asset, h, ws, we)
        ptb = settle = None
        if asset == "btc":
            self._extend(int(min(now, we + 10)))
            if now >= ws + 3:
                ptb = {"value": f"{self._avg(ws - self.avg_s, ws):.2f}", "currency": "USD"}
            if now >= we + 5:
                settle = {"value": f"{self._avg(we - self.avg_s, we):.2f}", "currency": "USD"}
        return {
            "id": zlib.crc32(slug.encode()), "slug": slug, "category": "crypto",
            "question": f"{asset.upper()} Up or Down - {dt:%b %d %H:%M} UTC", "active": now < we, "closed": now >= we + 5,
            "assetPriceTerms": {
                "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
                "asset": {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": asset},
                "indexSymbol": "BRTI", "horizon": h,
                "windowStart": iso(ws).replace("Z", ".000Z"), "windowEnd": iso(we),
                "priceToBeat": ptb, "settlementPrice": settle, "priceDecimals": 2}}

    def _book(self, slug, now):
        asset, h, ws, we = self.index[slug]
        if now >= we:
            return {"marketData": {"marketSlug": slug, "bids": [], "offers": [], "state": "MARKET_STATE_EXPIRED"}}
        self._extend(int(now))
        rng = random.Random(zlib.crc32(slug.encode()) + int(now) // 4)
        S = self.prices[int(now)]
        K = self._avg(ws - self.avg_s, ws)
        tau = we - now
        obs = None
        if tau < self.avg_s:
            vals = [self.prices[t] for t in range(int(we) - self.avg_s, int(now) + 1)]
            obs = sum(vals) / len(vals)
        fair = chance_up(S, K, tau, self.sig * self.regime * (self.vol_view[0] + (self.vol_view[1] - self.vol_view[0]) * (zlib.crc32(slug.encode()) % 100) / 100),
                         obs, float(self.avg_s)) + rng.gauss(0, self.book_noise)
        mid = min(0.97, max(0.03, fair))
        bid = math.floor((mid - 0.01) * 100) / 100
        ask = round(bid + 0.02, 2)
        def lv(start, step, n):
            return [{"px": {"value": f"{min(0.99, max(0.01, start + step * i)):.2f}", "currency": "USD"},
                     "qty": str(rng.randint(80, 400))} for i in range(n)]
        return {"marketData": {"marketSlug": slug, "bids": lv(bid, -0.01, 5), "offers": lv(ask, 0.01, 5),
                               "state": "MARKET_STATE_OPEN", "stats": {}, "transactTime": iso(now)}}

    def get(self, path, params=None, headers=None):
        now = self.clock.time()
        params = params or {}
        if path == "/v1/asset-prices/history":
            frm, to = int(params["from"]), (int(params.get("to") or 0) or int(now))   # to=0 means "now"
            to = min(to, int(now))
            self._extend(to)
            candles = [{"timestamp": t, "open": f"{self.prices[t]:.2f}", "high": f"{self.prices[t]:.2f}",
                        "low": f"{self.prices[t]:.2f}", "close": f"{self.prices[t]:.2f}", "tickCount": 1}
                       for t in range(max(frm, self.t0), to + 1)]
            return {"series": [{"asset": {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"}, "currency": "USD",
                                "bucket": params.get("bucket"), "from": frm, "to": to, "candles": candles}]}
        if path == "/v1/markets":
            out = []
            for h, dur in HORIZON_SECONDS.items():
                ws0 = math.floor(now / dur) * dur
                for ws in (ws0 - dur, ws0, ws0 + dur):
                    if ws + dur <= now or ws - 90 > now:
                        continue
                    out.append(self._raw_market("btc", h, ws, now))
            out.append(self._raw_market("eth", "15m", math.floor(now / 900) * 900, now))   # decoy
            return {"markets": out}
        m = re.fullmatch(r"/v1/market/slug/(.+)", path)
        if m:
            slug = m.group(1)
            mm = re.match(r"cpc-(\w+)-updown-(\w+)-(\d{4}-\d\d-\d\d)-(\d{4})z", slug)
            if not mm:
                raise NotFound(path)
            ws = datetime.strptime(mm.group(3) + mm.group(4), "%Y-%m-%d%H%M").replace(tzinfo=timezone.utc).timestamp()
            h = {"60m": "1h"}.get(mm.group(2), mm.group(2))
            if h not in HORIZON_SECONDS:
                raise NotFound(path)
            return {"market": self._raw_market(mm.group(1), h, ws, now)}
        m = re.fullmatch(r"/v1/markets/(.+)/book", path)
        if m:
            return self._book(m.group(1), now)
        m = re.fullmatch(r"/v1/markets/(.+)/settlement", path)
        if m:
            asset, h, ws, we = self.index[m.group(1)]
            if now < we + 5:
                raise NotFound(path)
            return {"slug": m.group(1), "settlement": 1.0 if self._avg(we - self.avg_s, we) >= self._avg(ws - self.avg_s, ws) else 0.0}
        raise NotFound(path)


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------
def pct(x):
    return "-" if x is None else f"{x:.0%}"


def lean_side(w):
    """The side the display is about: the position held, else the current call, else the more likely side."""
    if w.position:
        return w.position.side
    if w.call:
        return w.call
    if w.p_final is None:
        return None
    return "UP" if w.p_final >= 0.5 else "DOWN"


def side_chance(x, side):
    """The model/market/blended numbers are all stored as the chance of UP; flip them for a DOWN view."""
    return None if x is None or side is None else (x if side == "UP" else 1.0 - x)


def render(eng: Engine, clock):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    cfg = eng.cfg
    now = clock.time()
    t = Table(title=f"Makawan - BTC Up/Down tracker (Polymarket US)   {hms(now)} {tz_abbr(now)}   BRTI "
                    f"{'-' if eng.S is None else format(eng.S, ',.2f')}   "
                    f"typical minute ~${'-' if eng.sigma_sec is None else format(eng.sigma_sec * math.sqrt(60), '.0f')}",
              title_style="bold", expand=True)
    t.caption = ("Model, Market, Chance and Bid / ask are all for the Side shown: the side held, else the call, "
                 "else the more likely side. A low UP chance means a high DOWN chance.")
    for c in ("Window", "Closes in", "Line", "vs line", "Side", "Model", "Market", "Chance", "Call", "Bid / ask", "Value",
              "Paper position"):
        t.add_column(c, justify="left" if c in ("Window", "Side", "Value", "Paper position", "Call") else "right")
    for h in cfg.horizons:
        w = eng.active.get(h)
        if not w:
            t.add_row(h, "-", "-", "-", "-", "-", "-", "-", "-", "-", "listing not found yet", "-")
            continue
        i = w.info
        diff = "-" if (eng.S is None or i.ptb is None) else f"{eng.S - i.ptb:+,.2f}"
        call = Text("-")
        if w.call:
            call = Text(w.call, style="bold green" if w.call == "UP" else "bold red")
        side = lean_side(w)
        sb = None if not w.book else (w.book.flipped() if side == "DOWN" else w.book)
        ba = "-" if sb is None else f"{(sb.bid or 0) * 100:.0f} / {(sb.ask or 0) * 100:.0f}c"
        pos = "-"
        if w.position:
            p = w.position
            pos = f"{p.side} {p.shares:.1f} @ {p.avg_px * 100:.1f}c"
        t.add_row(h, mmss(w.tau), "-" if i.ptb is None else f"{i.ptb:,.2f}", diff, side or "-",
                  pct(side_chance(w.p_model, side)), pct(side_chance(w.p_mkt, side)), pct(side_chance(w.p_final, side)),
                  call, ba, w.note or w.value_note or "-", pos)

    s = Table(title="Scorecard (paper money, example sizing)", expand=True)
    for c in ("Window", "Settled", "1st calls", "Right", "Hit rate", "Trades", "Staked", "Gross P&L", "Fees", "Net P&L",
              "Brier @50%"):
        s.add_column(c, justify="right")
    for h in cfg.horizons:
        st = eng.stats[h]
        s.add_row(h, str(st.windows), str(st.called), str(st.correct),
                  "-" if not st.called else f"{st.correct / st.called:.0%}", str(st.trades),
                  f"${st.staked:,.2f}", f"${st.pnl + st.fees:+,.2f}", f"${st.fees:,.2f}", f"${st.pnl:+,.2f}",
                  "-" if not st.brier_n else f"{st.brier / st.brier_n:.3f}")
    foot = Text(f"cash ${eng.cash:,.2f}   equity ${eng.equity():,.2f}   start ${cfg.bankroll:,.2f}   "
                f"(0.250 Brier = coin flip)", style="dim")
    log = Text("\n".join(t for _, t in eng.events) or "no events yet")
    warn = Text("\n".join("! " + x for x in ([eng.guard_note()] if eng.guard_note() else []) + list(eng.warnings))
                + (f"\n! feed: {eng.last_error}" if eng.last_error else ""),
                style="yellow")
    return Group(t, s, foot, Panel(log, title="Calls and trades", height=min(18, len(eng.events) + 2 or 3)), warn)


# --------------------------------------------------------------------------
# Self-test: reproduce the numbers on screen in the video
# --------------------------------------------------------------------------
def selftest(verbose=True):
    sig = lambda per_min: per_min / math.sqrt(60.0)
    rows = []
    def chk(name, got, want, tol):
        ok = abs(got - want) <= tol
        rows.append((name, got, want, ok))
    chk("chance: +$10, 10:00 left, $25/min", chance_up(84010, 84000, 600, sig(25)) * 100, 55, 0.6)
    chk("chance: +$59, 10:00 left, $25/min", chance_up(84059, 84000, 600, sig(25)) * 100, 78, 0.6)
    chk("chance: +$60, 2:00 left, $25/min", chance_up(84060, 84000, 120, sig(25)) * 100, 98, 0.6)
    chk("chance: +$60, 2:00 left, $40/min", chance_up(84060, 84000, 120, sig(40)) * 100, 90, 0.6)
    chk("blend 90% model + 66% market", blend(0.90, 0.66, 0.5) * 100, 78, 0.01)
    chk("fee on a 60c share (cents)", fee_per_share(0.60) * 100, 1.7, 0.05)
    chk("edge, 70% chance at 60c (cents)", ev_per_share(0.70, 0.60) * 100, 8.3, 0.05)
    chk("edge, 70% chance at 80c (cents)", ev_per_share(0.70, 0.80) * 100, -11.1, 0.05)
    chk("good up to (cents) at 70%", max_buy_price(0.70) * 100, 67, 0.01)
    chk("size: $1,000, 70% share at 66c ($)", kelly_dollars(0.70, 0.66, 1000), 18.81, 0.01)
    chk("size: $1,000, 70% share at 56c ($)", kelly_dollars(0.70, 0.56, 1000), 72.64, 0.01)
    chk("size: $1,000, 70% share at 55c ($)", kelly_dollars(0.70, 0.55, 1000), 76.71, 0.01)
    ok = all(r[3] for r in rows)
    if verbose:
        print(f"{'check':45s} {'ours':>9s} {'video':>9s}")
        for n, g, w, o in rows:
            print(f"{n:45s} {g:9.2f} {w:9.2f}  {'OK' if o else 'MISMATCH'}")
        print("\nAll match the video." if ok else "\nSome numbers differ from the video.")
    return ok


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def discover(cfg: Config):
    api = Api(HttpTransport(cfg.gateway), cfg)
    now = time.time()
    print(f"Gateway {cfg.gateway}   local time {iso(now)}")
    c = api.brti(int(now) - 15, 0)
    if c:
        ts, px, _ = c[-1]
        print(f"BRTI latest print: {px:,.2f} at {iso(ts)}  ({now - ts:.0f}s ago)")
    else:
        print("No index candles returned.")
    found = api.find_active(now)
    for h in cfg.horizons:
        m = found.get(h)
        if not m:
            print(f"  {h:>3}: no active BTC Up/Down window found")
            continue
        b = api.book(m.slug)
        print(f"  {h:>3}: {m.slug}  {iso(m.ws)} -> {iso(m.we)}  price to beat {m.ptb}  index {m.index_symbol or '?'}  "
              f"UP bid/ask {b.bid if b else '-'}/{b.ask if b else '-'}")
    all_btc = api.list_windows(now)
    print(f"Listed BTC Up/Down windows in range: {len(all_btc)}  horizons: {sorted({m.horizon for m in all_btc})}")


def main():
    ap = argparse.ArgumentParser(description="Makawan: BTC 5m/15m/1h tracker for Polymarket US (paper trading)")
    ap.add_argument("--selftest", action="store_true", help="reproduce the numbers from the video and exit")
    ap.add_argument("--discover", action="store_true", help="show the live windows the bot would track and exit")
    ap.add_argument("--simulate", action="store_true", help="offline demo with a fake market, no network")
    ap.add_argument("--speed", type=float, default=30.0, help="simulation speed (virtual seconds per real second, 0 = as fast as possible)")
    ap.add_argument("--max-seconds", type=int, default=0, help="stop after this many (virtual) seconds")
    ap.add_argument("--horizons", default="5m,15m,1h")
    ap.add_argument("--bankroll", type=float, default=None,
                    help="starting paper bankroll (default: what this folder started with, else 1000)")
    ap.add_argument("--w-market", type=float, default=None, help="weight on the market price when blend is 'avg'")
    ap.add_argument("--preset", choices=sorted(PRESETS), default="pro",
                    help="'video' = the video's example numbers exactly; 'pro' = fat tails + recent-vol blend (default)")
    ap.add_argument("--params", default=None, help="params.json from makawan_tune.py (per-window-length settings)")
    ap.add_argument("--no-ticks", action="store_true", help="don't write ticks.csv")
    ap.add_argument("--exec", dest="exec_mode", choices=("taker", "maker"), default=None,
                    help="taker = buy at the ask (default); maker = post a paper limit bid and wait to be filled "
                         "(no fee; live paper only, not with --simulate)")
    ap.add_argument("--stake", type=float, default=None, help="flat dollars per trade (default 20; 0 = quarter-Kelly sizing)")
    ap.add_argument("--stop-loss", type=float, default=None,
                    help="halt trading once this share of the starting bankroll is lost (default 0.5; 0 = off)")
    ap.add_argument("--daily-loss", type=float, default=None,
                    help="pause until tomorrow after losing this many dollars in a day (default off)")
    ap.add_argument("--resume", action="store_true", help="clear a stop-loss halt and trade again")
    ap.add_argument("--max-gap", type=float, default=None,
                    help="skip a trade when the model is more than this far above the market (default 0.20; 1 = off)")
    ap.add_argument("--no-trade-on", dest="no_trade_on", default=None,
                    help="window lengths to track but never trade, e.g. 1h (default) or 'none'")
    ap.add_argument("--trade-last", default=None,
                    help="only open trades in the last N seconds of each window, e.g. 5m=120,15m=300,1h=0 (0 = any time)")
    ap.add_argument("--warmup-s", type=float, default=None,
                    help="wait this many seconds into every window before its first call (default: the longer of 45 s and 10%% of the window)")
    ap.add_argument("--confirm-s", type=float, default=None, help="seconds a call change must hold before it counts (default 3)")
    ap.add_argument("--no-trade", action="store_true", help="calls only, no paper trades")
    ap.add_argument("--out", default=None, help="log folder (default makawan_out, or makawan_out_sim with --simulate)")
    ap.add_argument("--fresh", action="store_true", help="archive the old history in the log folder and start from scratch")
    ap.add_argument("--spot", choices=("off", "watch", "use"), default="watch",
                    help="Coinbase live price: watch = log it next to the index (default); use = also use it in the "
                         "model; off")
    ap.add_argument("--cut", action="store_true",
                    help="sell a LOSING position when buyers pay more for it (after the fee) than the maths says it is worth")
    ap.add_argument("--stream", action="store_true",
                    help="get order books from Polymarket's live stream instead of polling (needs your API key; read-only)")
    ap.add_argument("--allow-sleep", action="store_true", help="do not keep the Mac awake while the bot runs")
    ap.add_argument("--plain", action="store_true", help="plain log lines instead of the live dashboard")
    ap.add_argument("--tz", default="America/New_York", help="time zone for on-screen times (default US Eastern)")
    a = ap.parse_args()
    if not set_display_tz(a.tz):
        print(f"Time zone '{a.tz}' not available, showing UTC. On Windows run: pip install tzdata")

    if a.selftest:
        sys.exit(0 if selftest() else 1)
    a.out = a.out or ("makawan_out_sim" if a.simulate else "makawan_out")
    if a.fresh:
        moved = archive_outputs(a.out)
        if moved:
            print(f"Old history moved to {moved}/ - starting from scratch.")
    try:
        cfg = build_config(a)
    except (ValueError, OSError) as e:
        sys.exit(f"Settings problem: {e}")
    if a.discover:
        return discover(cfg)

    if a.simulate and cfg.exec_mode == "maker":
        sys.exit(SIM_MAKER_MSG)
    if a.simulate:
        start, hist = sim_start(cfg.out_dir)       # resume the fake clock after the last run in this folder
        clock = SimClock(start)
        api = Api(SimTransport(clock, history_s=hist), cfg)
    else:
        clock = RealClock()
        api = Api(HttpTransport(cfg.gateway), cfg)

    rec = Recorder(cfg.out_dir)
    write_state(cfg.out_dir, cfg.bankroll)
    clear_halt(cfg.out_dir, rec, a.resume)
    stream = make_stream() if (a.stream and not a.simulate) else None
    if a.stream and a.simulate:
        print("--stream is ignored in a simulation (there is nothing to stream).")
    if not a.simulate and not a.allow_sleep:
        keep_awake()
    spot = make_spot("off" if a.simulate else a.spot)
    eng = Engine(cfg, api, clock, rec, stream=stream, spot=spot)
    eng.restore()
    for n in eng.notes:
        print(n)
    eng.bootstrap()
    plain = a.plain or not sys.stdout.isatty()
    live = None
    if not plain:
        try:
            from rich.live import Live
            live = Live(render(eng, clock), refresh_per_second=4, screen=False)
            live.start()
        except ImportError:
            plain = True
    last_seq, t_start, last_status = 0, clock.time(), 0.0
    try:
        while True:
            eng.tick()
            now = clock.time()
            if live:
                live.update(render(eng, clock))
            elif plain:
                for seq, line in reversed([e for e in eng.events if e[0] > last_seq]):
                    print(line)
                    last_seq = seq
                if now - last_status >= 15:
                    last_status = now
                    bits = [f"{h}:{pct(w.p_final)}{'/' + w.call if w.call else ''}" for h, w in eng.active.items()]
                    print(f"{hms(now)} BRTI {eng.S or 0:,.2f}  " + "  ".join(bits) +
                          f"  equity ${eng.equity():,.2f}" + (f"  [{eng.last_error}]" if eng.last_error else ""), flush=True)
            if a.max_seconds and now - t_start >= a.max_seconds:
                break
            if a.simulate:
                clock.advance(1.0)
                if a.speed > 0:
                    time.sleep(1.0 / a.speed)
            else:
                time.sleep(max(0.05, math.floor(time.time()) + 1.15 - time.time()))
    except KeyboardInterrupt:
        pass
    finally:
        if live:
            live.stop()
    print("\nSummary")
    for h in cfg.horizons:
        st = eng.stats[h]
        print(f"  {h:>3}: windows {st.windows}  first calls {st.called}  right {st.correct}  "
              f"trades {st.trades}  staked ${st.staked:,.2f}  P&L ${st.pnl:+,.2f}")
    print(f"  equity ${eng.equity():,.2f} (started ${cfg.bankroll:,.2f})   logs in {cfg.out_dir}/")


if __name__ == "__main__":
    main()
