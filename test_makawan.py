"""Run with:  pytest -q test_makawan.py"""
import csv
import json
import time
import math
import os

import pytest

import makawan_bot as tb

START = (1_800_000_000 // 3600 + 1) * 3600 - 20


def sig(per_min):
    return per_min / math.sqrt(60.0)


def run_sim(out, seconds, clock=None, transport=None, **cfg_over):
    clock = clock or tb.SimClock(START)
    transport = transport or tb.SimTransport(clock, seed=5)
    cfg = tb.Config(out_dir=str(out), **cfg_over)
    eng = tb.Engine(cfg, tb.Api(transport, cfg), clock, tb.Recorder(cfg.out_dir), parallel=False)
    eng.restore()
    eng.bootstrap()
    for _ in range(seconds):
        eng.tick()
        clock.advance(1)
    return eng, clock


def rows(out, name):
    with open(os.path.join(out, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def slug_start(slug):
    from datetime import datetime, timezone
    d, hm = slug.split("-updown-")[1].split("-", 1)[1].rsplit("-", 1)
    return datetime.strptime(d + hm.rstrip("z"), "%Y-%m-%d%H%M").replace(tzinfo=timezone.utc).timestamp()


# ---- the maths ----------------------------------------------------------------------------------------
def test_video_numbers():
    assert tb.selftest(verbose=False)


@pytest.mark.parametrize("shares,price,fee", [(1000, 0.10, 6.26), (1000, 0.65, 15.81), (1000, 0.50, 17.38)])
def test_fee_matches_published_examples(shares, price, fee):
    assert tb.exch_fee(price, shares, 0.0695) == fee


def test_chance_moves_the_right_way_and_ties_settle_up():
    assert tb.chance_up(84050, 84000, 300, sig(25)) > tb.chance_up(84010, 84000, 300, sig(25))
    assert tb.chance_up(84050, 84000, 30, sig(25)) > tb.chance_up(84050, 84000, 300, sig(25))
    assert tb.chance_up(84000, 84000, 0, sig(25), obs_avg=84000) == 1.0


def test_hysteresis():
    cfg, s, seq = tb.Config(), None, []
    for p in (0.50, 0.58, 0.54, 0.52, 0.58, 0.42, 0.46, 0.48):
        s = tb.next_call(s, p, cfg)
        seq.append(s)
    assert seq == [None, "UP", "UP", None, "UP", "DOWN", "DOWN", None]


def test_side_chance_and_lean():
    ws = tb.WindowState(tb.MarketInfo("s", "5m", 0, 300))
    ws.p_final = 0.03
    assert tb.lean_side(ws) == "DOWN" and tb.side_chance(0.03, "DOWN") == pytest.approx(0.97)
    ws.position = tb.Position("UP", 1, 0.5, 0.0, 0.5, 0)
    assert tb.lean_side(ws) == "UP"


# ---- trading rules ---------------------------------------------------------------------------------------
def test_sim_end_to_end_and_rules(tmp_path):
    eng, _ = run_sim(tmp_path, 7300)
    assert eng.errors == 0, eng.last_error
    ev, win = rows(tmp_path, "events.csv"), rows(tmp_path, "windows.csv")
    assert not any(r["event"] == "SELL" for r in ev)                         # no early selling
    for r in win:
        assert (r["outcome"] == "UP") == (float(r["settlement"]) >= float(r["price_to_beat"]))
    limit = {"5m": 120, "15m": 300}
    for r in ev:
        if r["event"] == "BUY" and r["horizon"] in limit:                       # trades only late in the window
            left = slug_start(r["slug"]) + tb.HORIZON_SECONDS[r["horizon"]] - tb.parse_ts(r["time_utc"])
            assert left <= limit[r["horizon"]] + 1
    assert eng.equity() - 1000 == pytest.approx(sum(s.pnl for s in eng.stats.values()), abs=0.05)


def test_trade_last_parser():
    assert tb.parse_trade_last("5m=120,15m=300,1h=0") == {"5m": 120.0, "15m": 300.0}
    assert tb.parse_trade_last("none") == {}
    with pytest.raises(ValueError):
        tb.parse_trade_last("3m=10")


# ---- picking up where the last run stopped -----------------------------------------------------------------
def test_restart_carries_on_instead_of_starting_fresh(tmp_path):
    clock = tb.SimClock(START)
    tr = tb.SimTransport(clock, seed=5)
    e1, _ = run_sim(tmp_path, 3200, clock, tr)
    while not any(w.position for w in e1.active.values()):                      # stop while a position is open
        e1.tick()
        clock.advance(1)
    before = {h: (s.windows, s.called, s.correct, s.trades, round(s.staked, 2), round(s.pnl, 2)) for h, s in e1.stats.items()}
    eq, held = e1.equity(), [w.info.slug for w in e1.active.values() if w.position]
    hist = [(r["horizon"], r["side"], r["pnl"]) for r in e1.history]

    e2 = tb.Engine(e1.cfg, tb.Api(tr, e1.cfg), clock, tb.Recorder(e1.cfg.out_dir), parallel=False)
    info = e2.restore()
    after = {h: (s.windows, s.called, s.correct, s.trades, round(s.staked, 2), round(s.pnl, 2)) for h, s in e2.stats.items()}
    assert info["open"] == len(held) >= 1
    for h in before:                                                             # the sheet rounds each trade to the cent
        assert after[h][:4] == before[h][:4]
        assert after[h][4] == pytest.approx(before[h][4], abs=0.05) and after[h][5] == pytest.approx(before[h][5], abs=0.05)
    assert e2.equity() == pytest.approx(eq, abs=0.05)                            # CSVs round to the cent
    assert [(r["horizon"], r["side"], r["pnl"]) for r in e2.history] == hist
    assert e2.events and e2.notes and "Picked up" in e2.notes[0]

    e2.bootstrap()
    for _ in range(4000):
        e2.tick()
        clock.advance(1)
    for slug in held:                                                            # each carried position settles exactly once
        assert sum(1 for r in rows(tmp_path, "events.csv") if r["event"] == "SETTLE" and r["slug"] == slug) == 1
        assert sum(1 for r in rows(tmp_path, "windows.csv") if r["slug"] == slug) == 1
    assert e2.equity() - 1000 == pytest.approx(sum(s.pnl for s in e2.stats.values()), abs=0.05)


def test_a_position_whose_window_closed_while_stopped_still_settles(tmp_path):
    clock = tb.SimClock(START)
    tr = tb.SimTransport(clock, seed=5)
    e1, _ = run_sim(tmp_path, 3200, clock, tr)
    while not any(w.position for w in e1.active.values()):
        e1.tick()
        clock.advance(1)
    held = [w for w in e1.active.values() if w.position]
    clock.advance(max(w.info.we for w in held) - clock.time() + 30)              # stopped through the close
    e2, _ = run_sim(tmp_path, 60, clock, tr)
    for w in held:
        assert sum(1 for r in rows(tmp_path, "windows.csv") if r["slug"] == w.info.slug) == 1


def test_bankroll_is_remembered_and_flag_overrides(tmp_path):
    import argparse
    out = str(tmp_path)
    tb.write_state(out, 2500.0)
    a = argparse.Namespace(horizons="5m,15m,1h", bankroll=None, no_trade=False, out=out)
    assert tb.build_config(a).bankroll == 2500.0
    a.bankroll = 500.0
    assert tb.build_config(a).bankroll == 500.0
    a.out, a.bankroll = str(tmp_path / "new"), None
    assert tb.build_config(a).bankroll == 1000.0


def test_fresh_archives_the_old_history(tmp_path):
    run_sim(tmp_path, 1500)
    dest = tb.archive_outputs(str(tmp_path))
    assert dest and os.path.exists(os.path.join(dest, "windows.csv")) and not os.path.exists(tmp_path / "windows.csv")
    eng, _ = run_sim(tmp_path, 10)
    assert eng.notes == [] and all(s.windows == 0 for s in eng.stats.values())
    assert tb.archive_outputs(str(tmp_path / "empty")) is None


def test_older_csv_layout_is_upgraded_not_lost(tmp_path):
    path = tmp_path / "windows.csv"
    with open(path, "w", newline="") as f:                                      # a file from an older version
        w = csv.writer(f)
        w.writerow(["window_start", "window_end", "horizon", "slug", "outcome", "first_call", "traded_side", "pnl"])
        w.writerow(["2027-01-01T00:00:00Z", "2027-01-01T00:05:00Z", "5m", "old-1", "UP", "UP", "UP", "3.5"])
    tb.Recorder(str(tmp_path))
    r = rows(tmp_path, "windows.csv")
    assert list(r[0]) == tb.Recorder.WIN and r[0]["slug"] == "old-1" and r[0]["pnl"] == "3.5"
    assert os.path.exists(str(path) + ".bak")
    eng, _ = run_sim(tmp_path, 5)
    assert eng.stats["5m"].windows >= 1


def test_restore_survives_a_broken_or_odd_file(tmp_path):
    with open(tmp_path / "events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(tb.Recorder.EVENT)
        w.writerow(["2027-01-01T00:00:00Z", "", "5m", "cpc-btc-updown-5m-2027-01-01-0000z", "BUY", "UP", "",
                    "", "", "oops", "abc", "", "", ""])
    eng, _ = run_sim(tmp_path, 5)
    assert eng.errors == 0


# ---- web page, launcher, week simulation ------------------------------------------------------------------
def test_web_snapshot_routes_and_notes(tmp_path):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    import makawan_web as tw
    run_sim(tmp_path, 1500)
    eng, clock = run_sim(tmp_path, 300)
    snap = tw.snapshot(eng, clock, "simulation")
    assert snap["notes"] and snap["out_dir"] == str(tmp_path) and snap["history"]
    tw.Shared.latest, tw.Shared.out_dir = json.dumps(snap, allow_nan=False).encode(), str(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), tw.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "Makawan" in page and "Terry\\u2019s" not in page
        assert json.load(urllib.request.urlopen(base + "/api/state"))["ready"] is True
    finally:
        srv.shutdown()


def test_week_script_always_starts_clean(tmp_path):
    import simulate_week as sw
    out = str(tmp_path / "w")
    sw.run(0.03, 1, False, "pro", None, out, 1000.0, say=lambda *a: None)
    eng, curve = sw.run(0.03, 1, False, "pro", None, out, 1000.0, say=lambda *a: None)
    assert eng.notes == [] and curve[0][1] == 1000.0


def test_all_files_share_one_version():
    import re
    here = os.path.dirname(os.path.abspath(tb.__file__))
    for f in ("makawan_web.py", "makawan_tune.py", "simulate_week.py", "makawan_latency_check.py",
              "makawan_watchdog.py", "makawan_report.py", "makawan_reconcile.py", "makawan_live.py",
              "makawan_lab.py"):
        assert re.search(r'REQUIRES = "%s"' % re.escape(tb.__version__), open(os.path.join(here, f)).read()), f


def test_a_position_that_cannot_be_settled_stays_counted_at_cost(tmp_path):
    class Offline:
        def get(self, path, params=None):
            raise tb.NotFound(path)
    with open(tmp_path / "events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(tb.Recorder.EVENT)
        w.writerow(["2027-01-01T00:03:00Z", "", "5m", "cpc-btc-updown-5m-2027-01-01-0000z", "BUY", "UP", "0.8", "",
                    "", "0.8", "10", "0.11", "", ""])
    cfg = tb.Config(out_dir=str(tmp_path))
    e = tb.Engine(cfg, tb.Api(Offline(), cfg), tb.RealClock(), tb.Recorder(cfg.out_dir), parallel=False)
    e.restore()
    assert e.equity() == pytest.approx(1000.0) and e.cash == pytest.approx(1000.0 - 8.11) and e.warnings


def test_sheet_shows_taker_gross_fee_and_net(tmp_path):
    eng, _ = run_sim(tmp_path, 7300)
    tr = [r for r in rows(tmp_path, "windows.csv") if r["traded_side"]]
    assert tr
    for r in tr:
        assert r["liquidity"] == "taker" and r["net_pnl"] == r["pnl"]
        assert float(r["gross_pnl"]) - float(r["fee"]) == pytest.approx(float(r["net_pnl"]), abs=0.011)
        payout = float(r["shares"]) if r["traded_side"] == r["outcome"] else 0.0
        assert float(r["gross_pnl"]) == pytest.approx(payout - float(r["shares"]) * float(r["avg_px"]), abs=0.011)
    fees = sum(float(r["fee"]) for r in tr)
    assert sum(s.fees for s in eng.stats.values()) == pytest.approx(fees, abs=0.02)
    assert all("taker" in r["note"] for r in rows(tmp_path, "events.csv") if r["event"] == "BUY")
    import makawan_web as tw
    snap = tw.snapshot(eng, _, "simulation")
    for h, st in snap["stats"].items():
        assert st["gross"] == pytest.approx(st["pnl"] + st["fees"], abs=0.01)
    assert any(r["fee"] and r["liquidity"] == "taker" and r["gross"] is not None for r in snap["history"])


def test_old_sheet_gets_the_new_columns_filled_in(tmp_path):
    old_cols = [c for c in tb.Recorder.WIN if c not in ("liquidity", "gross_pnl", "net_pnl")]
    with open(tmp_path / "windows.csv", "w", newline="") as f:
        w = csv.DictWriter(f, old_cols)
        w.writeheader()
        w.writerow({"window_start": "2027-01-01T00:00:00Z", "window_end": "2027-01-01T00:05:00Z", "horizon": "5m",
                    "slug": "a", "outcome": "UP", "traded_side": "UP", "shares": "10", "avg_px": "0.8",
                    "fee": "0.11", "pnl": "1.89"})
        w.writerow({"window_start": "2027-01-01T00:05:00Z", "window_end": "2027-01-01T00:10:00Z", "horizon": "5m",
                    "slug": "b", "outcome": "UP"})
    tb.Recorder(str(tmp_path))
    r = rows(tmp_path, "windows.csv")
    assert (r[0]["liquidity"], r[0]["gross_pnl"], r[0]["net_pnl"]) == ("taker", "2.00", "1.89")
    assert (r[1]["liquidity"], r[1]["gross_pnl"], r[1]["net_pnl"]) == ("", "", "")


# ---- paper maker mode ---------------------------------------------------------------------------------------
def test_maker_price_never_crosses_and_respects_the_edge():
    cfg = tb.Config(exec_mode="maker")
    v = cfg.view("5m")
    sb = tb.Book([(0.70, 100)], [(0.72, 100)])
    px, ceiling = tb.Engine._maker_price(v, 0.01, sb, 0.90)
    assert px == 0.71 and px < sb.ask                                      # one tick above the bid, below the ask
    px, _ = tb.Engine._maker_price(v, 0.01, tb.Book([(0.70, 100)], [(0.71, 100)]), 0.90)
    assert px == 0.70                                                      # no room to improve: join the bid
    px, ceiling = tb.Engine._maker_price(v, 0.01, sb, 0.66)
    assert px <= ceiling and px == 0.65                                    # capped by the edge (no fee: 66% - 1c)


def test_a_maker_bid_fills_only_when_the_ask_reaches_it():
    cfg = tb.Config(exec_mode="maker", out_dir="x")
    class NoApi:
        def get(self, *a, **k):
            raise tb.NotFound("x")
    import tempfile
    cfg.out_dir = tempfile.mkdtemp()
    eng = tb.Engine(cfg, tb.Api(NoApi(), cfg), tb.SimClock(0), tb.Recorder(cfg.out_dir), parallel=False)
    w = tb.WindowState(tb.MarketInfo("cpc-btc-updown-5m-2027-01-01-0000z", "5m", 0, 300, ptb=1.0))
    w.call, w.p_final, w.tau = "UP", 0.85, 60
    w.book = tb.Book([(0.70, 100)], [(0.73, 100)])
    w.order = {"side": "UP", "px": 0.71, "shares": 50.0, "posted": 0}
    w.order_status = "resting"
    eng._work_order(w, 10)
    assert w.order and not w.position and w.order["px"] == pytest.approx(0.71)     # ask 73c: still resting
    w.book = tb.Book([(0.68, 100)], [(0.71, 30)])
    eng._work_order(w, 11)
    assert w.position and w.position.shares == 30.0 and w.position.avg_px == pytest.approx(0.71)
    assert w.order_status == "partial" and w.position.fee == 0.0 and w.order is None


def test_maker_bid_is_cancelled_when_the_call_changes(tmp_path):
    cfg = tb.Config(exec_mode="maker", out_dir=str(tmp_path))
    class NoApi:
        def get(self, *a, **k):
            raise tb.NotFound("x")
    eng = tb.Engine(cfg, tb.Api(NoApi(), cfg), tb.SimClock(0), tb.Recorder(cfg.out_dir), parallel=False)
    w = tb.WindowState(tb.MarketInfo("cpc-btc-updown-5m-2027-01-01-0000z", "5m", 0, 300, ptb=1.0))
    w.call, w.p_final, w.tau = None, 0.5, 60
    w.book = tb.Book([(0.50, 100)], [(0.52, 100)])
    w.order, w.order_status = {"side": "UP", "px": 0.51, "shares": 10.0, "posted": 0}, "resting"
    eng._work_order(w, 5)
    assert w.order is None and w.order_status == "unfilled" and not w.position
    assert any(r["event"] == "CANCEL" for r in rows(tmp_path, "events.csv"))


def test_run_both_in_simulation_runs_only_the_taker_bots(tmp_path):
    import json
    import signal
    import subprocess
    import sys
    import time
    import urllib.request
    here = os.path.dirname(os.path.abspath(tb.__file__))
    p = subprocess.Popen([sys.executable, os.path.join(here, "run_both.py"), "--simulate", "--speed", "60",
                          "--late-port", "8871", "--any-port", "8872", "--late-maker-port", "8873",
                          "--any-maker-port", "8874"], cwd=tmp_path, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    try:
        models, deadline = {}, time.time() + 60
        while time.time() < deadline and len(models) < 2:
            for port in (8871, 8872):
                try:
                    d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2))
                    if d.get("ready"):
                        models[port] = d["model"]
                except OSError:
                    pass
            time.sleep(0.5)
        assert "5m last 2:00" in models[8871] and "5m any time" in models[8872]
        assert all("taker" in m for m in models.values())
        for port in (8873, 8874):                                        # no maker bots in a simulation
            with pytest.raises(OSError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
        assert not os.path.exists(tmp_path / "makawan_out_late_maker_sim")
    finally:
        p.send_signal(signal.SIGINT)
        out, _ = p.communicate(timeout=30)
    assert "only the two taker bots" in out and "All stopped." in out


def test_maker_mode_is_refused_in_simulation(tmp_path):
    import subprocess
    import sys
    here = os.path.dirname(os.path.abspath(tb.__file__))
    for script in ("makawan_bot.py", "makawan_web.py"):
        r = subprocess.run([sys.executable, os.path.join(here, script), "--simulate", "--exec", "maker"],
                           cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert r.returncode != 0 and "cannot be simulated fairly" in (r.stderr + r.stdout), script
    import simulate_week as sw
    eng, _ = sw.run(0.01, 1, False, "pro", None, str(tmp_path / "w"), 1000.0, say=lambda *a: None)
    assert eng.cfg.exec_mode == "taker"


def test_launcher_live_command_includes_the_maker_bots():
    here = os.path.dirname(os.path.abspath(tb.__file__))
    src = open(os.path.join(here, "run_both.py")).read()
    assert '"--exec", "maker"' in src and "makawan_out_late_maker" in src and "makawan_out_any_maker" in src


def test_empty_order_book_is_explained_not_blank(tmp_path):
    import makawan_web as tw
    class Empty(tb.SimTransport):
        def _book(self, slug, now):
            return {"marketData": {"marketSlug": slug, "bids": [], "offers": [], "state": "MARKET_STATE_OPEN"}}
    clock = tb.SimClock(START)
    eng, clock = run_sim(tmp_path, 400, clock, Empty(clock, seed=5))
    w = eng.active["5m"]
    assert w.book is None and w.p_mkt is None and w.p_final == w.p_model     # no quotes: the chance is the model alone
    assert "order book empty" in w.value_note
    snap = {x["horizon"]: x for x in tw.snapshot(eng, clock, "simulation")["windows"]}
    assert snap["5m"]["bid"] is None and snap["5m"]["ask"] is None and "order book empty" in snap["5m"]["value"]
    assert "nobody quoting" in tw.PAGE and "no quotes" in tw.PAGE


def test_a_seller_hitting_the_bid_fills_it():
    import tempfile
    class NoApi:
        def get(self, *a, **k):
            raise tb.NotFound("x")
    cfg = tb.Config(exec_mode="maker", out_dir=tempfile.mkdtemp())
    eng = tb.Engine(cfg, tb.Api(NoApi(), cfg), tb.SimClock(0), tb.Recorder(cfg.out_dir), parallel=False)

    def window(side, px, alone):
        w = tb.WindowState(tb.MarketInfo("cpc-btc-updown-5m-2027-01-01-0000z", "5m", 0, 300, ptb=1.0))
        w.call, w.p_final, w.tau = side, (0.85 if side == "UP" else 0.15), 60
        w.order = {"side": side, "px": px, "shares": 50.0, "posted": 0, "alone": alone}
        w.order_status = "resting"
        return w

    w = window("UP", 0.71, True)                                  # alone at 71c: a trade AT 71c must have hit us
    w.book, w.vol_delta = tb.Book([(0.71, 50)], [(0.73, 100)], last_px=0.71, volume=1000), 20.0
    eng._work_order(w, 5)
    assert w.position and w.position.shares == 20.0 and w.position.avg_px == pytest.approx(0.71)

    w = window("UP", 0.71, False)                                 # joined others at 71c: needs a trade THROUGH 71c
    w.book, w.vol_delta = tb.Book([(0.71, 500)], [(0.73, 100)], last_px=0.71, volume=1000), 20.0
    eng._work_order(w, 5)
    assert w.order and not w.position
    w.book = tb.Book([(0.70, 500)], [(0.73, 100)], last_px=0.70, volume=1030)
    w.vol_delta = 30.0
    eng._work_order(w, 6)
    assert w.position and w.position.shares == 30.0

    w = window("DOWN", 0.30, True)                                # DOWN bid 30c = selling UP at 70c; UP trade at 70c hits it
    w.book, w.vol_delta = tb.Book([(0.68, 100)], [(0.70, 100)], last_px=0.70, volume=500), 15.0
    eng._work_order(w, 5)
    assert w.position and w.position.side == "DOWN" and w.position.shares == 15.0 and w.position.avg_px == pytest.approx(0.30)

    w = window("UP", 0.71, True)                                  # no new volume: no fill, whatever the last price says
    w.book, w.vol_delta = tb.Book([(0.71, 50)], [(0.73, 100)], last_px=0.60, volume=1000), 0.0
    eng._work_order(w, 5)
    assert w.order and not w.position


def test_parse_book_reads_last_trade_and_volume():
    b = tb.parse_book({"marketData": {"bids": [{"px": {"value": "0.55"}, "qty": "10"}],
                                      "offers": [{"px": {"value": "0.57"}, "qty": "10"}],
                                      "stats": {"lastTradePx": {"value": "0.56"}, "sharesTraded": "50000"}}})
    assert b.last_px == 0.56 and b.volume == 50000.0 and b.flipped().last_px == pytest.approx(0.44)


# ---- rules from the first day of paper results: no 1h trades, gap skip, flat $20 -----------------------------
def test_new_rules_in_a_full_run(tmp_path):
    eng, _ = run_sim(tmp_path, 7300)
    ev = rows(tmp_path, "events.csv")
    buys = [r for r in ev if r["event"] == "BUY"]
    assert buys and not any(r["horizon"] == "1h" for r in buys)                 # 1h is tracked, never traded
    assert eng.stats["1h"].windows >= 1 and eng.stats["1h"].trades == 0
    for r in buys:
        cost = float(r["shares"]) * float(r["price"]) + float(r["fee"])
        assert cost <= 20.0 + 0.02                                              # flat stake, fee included
        side = r["side"]
        gap = tb.side_chance(float(r["model"]), side) - tb.side_chance(float(r["market"]), side)
        assert gap <= 0.20 + 0.005
    assert sum(1 for r in buys if float(r["shares"]) * float(r["price"]) > 18.5) > len(buys) * 0.6


def test_gap_and_stake_helpers(tmp_path):
    cfg = tb.Config(out_dir=str(tmp_path))
    class NoApi:
        def get(self, *a, **k):
            raise tb.NotFound("x")
    eng = tb.Engine(cfg, tb.Api(NoApi(), cfg), tb.SimClock(0), tb.Recorder(cfg.out_dir), parallel=False)
    w = tb.WindowState(tb.MarketInfo("s", "5m", 0, 300))
    w.p_model, w.p_mkt = 0.10, 0.36                                             # DOWN: model 90%, market 64%
    assert eng._model_gap(w, "DOWN") == pytest.approx(0.26) and eng._model_gap(w, "UP") == pytest.approx(-0.26)
    w.p_mkt = None
    assert eng._model_gap(w, "DOWN") == 0.0
    v = cfg.view("5m")
    assert eng._stake(v, 0.9, 0.8, 0.0695) == 20.0
    eng.cash = 7.5
    assert eng._stake(v, 0.9, 0.8, 0.0695) == 7.5


def test_rules_can_be_changed_and_video_preset_turns_them_off():
    import argparse
    v = tb.Config(**tb.PRESETS["video"])
    assert v.flat_stake == 0 and v.max_model_gap == 1.0 and v.no_trade_horizons == ()
    a = argparse.Namespace(horizons="5m,15m,1h", bankroll=None, no_trade=False, out="x", stake=35.0, max_gap=0.15,
                           no_trade_on="none")
    cfg = tb.build_config(a)
    assert (cfg.flat_stake, cfg.max_model_gap, cfg.no_trade_horizons, cfg.trade) == (35.0, 0.15, (), True)
    d = tb.describe_model(tb.Config())
    assert "1h none (tracking only)" in d and "stake $20 flat" in d and "20+ pts above market" in d


# ---- live order-book stream ---------------------------------------------------------------------------------
class _WsServer:
    """A stand-in for Polymarket's markets WebSocket, speaking the message shapes in the official SDK."""

    def __init__(self):
        import asyncio
        import threading
        self.headers, self.subscribed, self.unsubscribed, self.conns = [], [], [], []
        self.px = 0.60
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(5)

    def _md(self, slug, rid):
        return json.dumps({"requestId": rid, "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA", "marketData": {
            "marketSlug": slug, "state": "MARKET_STATE_OPEN",
            "bids": [{"px": {"value": f"{self.px:.2f}", "currency": "USD"}, "qty": "100"}],
            "offers": [{"px": {"value": f"{self.px + 0.02:.2f}", "currency": "USD"}, "qty": "80"}],
            "stats": {"lastTradePx": {"value": f"{self.px + 0.01:.2f}"}, "sharesTraded": "1234"}}})

    async def _handler(self, conn):
        self.conns.append(conn)
        self.headers.append({k.lower() for k in conn.request.headers.keys()})
        subs = {}
        await conn.send(json.dumps({"heartbeat": {}}))
        async for raw in conn:
            m = json.loads(raw)
            if "subscribe" in m:
                rid = m["subscribe"]["requestId"]
                for slug in m["subscribe"].get("marketSlugs", []):
                    subs[rid] = slug
                    self.subscribed.append(slug)
                    await conn.send(self._md(slug, rid))
            elif "unsubscribe" in m:
                self.unsubscribed.append(subs.pop(m["unsubscribe"]["requestId"], None))

    def _run(self):
        import asyncio
        from websockets.asyncio.server import serve
        asyncio.set_event_loop(self.loop)

        async def main():
            self.server = await serve(self._handler, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            self.ready.set()
            await asyncio.Future()
        self.loop.run_until_complete(main())

    def call(self, coro):
        import asyncio
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(5)


def _throwaway_key():
    import base64
    from nacl.signing import SigningKey
    sk = SigningKey.generate()                                     # made up here; not a real account
    return "11111111-2222-3333-4444-555555556666", base64.b64encode(bytes(sk) + bytes(sk.verify_key)).decode()


def _wait(cond, secs=6.0):
    import time
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_book_stream_subscribes_updates_unsubscribes_and_reconnects():
    srv = _WsServer()
    kid, sec = _throwaway_key()
    st = tb.BookStream(kid, sec, base_url=f"ws://127.0.0.1:{srv.port}").start()
    try:
        st.want(["slug-a", "slug-b"])
        assert _wait(lambda: st.get("slug-a") is not None and st.get("slug-b") is not None)
        assert {"x-pm-access-key", "x-pm-signature", "x-pm-timestamp"} <= srv.headers[0]      # signed, like the SDK
        b = st.get("slug-a")
        assert (b.bid, b.ask, b.last_px, b.volume) == (0.60, 0.62, 0.61, 1234.0) and "live" in st.status()

        srv.px = 0.70                                                      # a price change is pushed, not polled
        rid = st.subs["slug-a"]
        srv.call(srv.conns[0].send(srv._md("slug-a", rid)))
        assert _wait(lambda: st.get("slug-a").bid == 0.70)

        st.want(["slug-b"])                                                # the window ended: drop it
        assert _wait(lambda: "slug-a" in srv.unsubscribed) and st.get("slug-a") is None

        srv.call(srv.conns[0].close())                                     # connection drops: reconnect + resubscribe
        assert _wait(lambda: len(srv.conns) >= 2 and st.get("slug-b") is not None, 10)
        assert st.reconnects >= 1 and srv.subscribed.count("slug-b") >= 2
    finally:
        st.stop()


def test_book_stream_get_is_none_when_not_trustworthy():
    kid, sec = _throwaway_key()
    st = tb.BookStream(kid, sec, base_url="ws://127.0.0.1:9")            # nothing listens there
    st.books["s"] = (tb.Book([(0.5, 1)], [(0.52, 1)]), 0)
    assert st.get("s") is None                                           # not connected
    st.connected, st.last_msg = True, __import__("time").time()
    assert st.get("s") is not None
    st.last_msg -= 60
    assert st.get("s") is None and "silent" in st.status()               # connected but silent for too long
    st.last_msg, st.disabled = __import__("time").time(), True
    assert st.get("s") is None and "polling" in st.status()


class _FakeStream:
    def __init__(self, transport, wrong=False):
        self.t, self.wrong, self.disabled, self.wanted = transport, wrong, False, []

    def want(self, slugs):
        self.wanted = list(slugs)

    def get(self, slug):
        if self.disabled or slug not in self.t.index:
            return None
        b = tb.parse_book(self.t._book(slug, self.t.clock.time()))
        if self.wrong and b.bids:                                        # a stream that is being read wrongly
            b = tb.Book([(round(b.bids[0][0] - 0.2, 2), 1)], [(round(b.asks[0][0] + 0.2, 2), 1)])
        return b

    def status(self):
        return "off" if self.disabled else "live (test)"


def _run_with_stream(tmp_path, wrong, seconds=900):
    clock = tb.SimClock(START)
    tr = tb.SimTransport(clock, seed=5)
    calls = {"book": 0}
    real_get = tr.get

    def counting_get(path, params=None):
        if path.endswith("/book"):
            calls["book"] += 1
        return real_get(path, params)
    tr.get = counting_get
    cfg = tb.Config(out_dir=str(tmp_path))
    stream = _FakeStream(tr, wrong)
    eng = tb.Engine(cfg, tb.Api(tr, cfg), clock, tb.Recorder(cfg.out_dir), parallel=False, stream=stream)
    eng.bootstrap()
    for _ in range(seconds):
        eng.tick()
        clock.advance(1)
    return eng, stream, calls


def test_engine_uses_the_stream_and_polls_only_to_cross_check(tmp_path):
    eng, stream, calls = _run_with_stream(tmp_path, wrong=False)
    assert eng.errors == 0 and not stream.disabled and not eng.warnings
    assert eng.stream_used > 2000 and eng.stream_used > 10 * eng.polled
    assert calls["book"] < 900 * 3 / 10                                    # about one check per market per 15 s
    assert set(stream.wanted) == {w.info.slug for w in eng.active.values()}
    assert eng.stream_status() == "live (test)"


def test_a_stream_that_keeps_disagreeing_is_switched_off(tmp_path):
    eng, stream, calls = _run_with_stream(tmp_path, wrong=True, seconds=400)
    assert stream.disabled and any("switched" in w for w in eng.warnings)
    assert eng.polled > 500                                                # back on polling afterwards


def test_stream_flag_without_a_key_falls_back_to_polling(monkeypatch):
    monkeypatch.delenv("POLYMARKET_KEY_ID", raising=False)
    monkeypatch.delenv("POLYMARKET_SECRET_KEY", raising=False)
    said = []
    assert tb.make_stream(say=said.append) is None and "API key" in said[0]


# ---- fixes from the first overnight run (2026-11-10) --------------------------------------------------------
def _bare_engine(tmp_path, **cfg_over):
    class NoApi:
        def get(self, *a, **k):
            raise tb.NotFound("x")
    cfg = tb.Config(out_dir=str(tmp_path), **cfg_over)
    return tb.Engine(cfg, tb.Api(NoApi(), cfg), tb.SimClock(1000), tb.Recorder(cfg.out_dir), parallel=False)


def _window(h="15m", left=120.0, **kw):
    dur = tb.HORIZON_SECONDS[h]
    w = tb.WindowState(tb.MarketInfo(f"cpc-btc-updown-{h}-2027-01-01-0000z", h, 1000 + left - dur, 1000 + left, ptb=100.0))
    w.tau = left
    for k, v in kw.items():
        setattr(w, k, v)
    return w


def test_settlement_is_the_closing_print_not_a_60_second_average():
    assert tb.Config().avg_window_s == 1 and tb.Config(**tb.PRESETS["video"]).avg_window_s == 60
    s = 16.33 / math.sqrt(60)
    # the 2:59 AM trade: 18 s left, BTC 15 cents above the line. It is a coin flip, not a sure thing.
    assert tb.chance_up(74772.76, 74772.61, 18, s, None, 1.0, 0.10, 2.0) == pytest.approx(0.51, abs=0.02)
    assert tb.chance_up(74772.76, 74772.61, 18, s, 74781.0, 60.0, 0.10, 2.0) > 0.99      # what the old rule said
    assert "closing index print" in tb.describe_model(tb.Config())
    import makawan_tune as tt
    assert tt.AVG == 1


def test_simulator_settles_on_the_closing_print():
    clock = tb.SimClock(START)
    tr = tb.SimTransport(clock, seed=5)
    ws = (START // 300) * 300
    clock.advance(ws + 300 + 10 - START)
    raw = tr.get(f"/v1/market/slug/cpc-btc-updown-5m-{__import__('datetime').datetime.fromtimestamp(ws, tb.timezone.utc):%Y-%m-%d-%H%M}z")
    t = raw["market"]["assetPriceTerms"]
    assert float(t["priceToBeat"]["value"]) == tr.prices[ws - 1] and float(t["settlementPrice"]["value"]) == tr.prices[ws + 299]


def test_gap_rule_uses_the_ask_when_there_is_no_two_sided_market(tmp_path):
    eng = _bare_engine(tmp_path)
    w = _window(call="UP", p_model=0.9996, p_mkt=None, p_final=0.9996)       # the 2 cent "bargain" at 2:59 AM
    w.book = tb.Book([], [(0.01, 500), (0.03, 5000)])
    eng._maybe_buy(w, 1000)
    assert w.position is None and "above the market" in w.value_note
    assert eng._model_gap(w, "UP", w.book) == pytest.approx(0.9896)
    w2 = _window(call="UP", p_model=0.93, p_mkt=None, p_final=0.93)          # one-sided but the price agrees: allowed
    w2.book = tb.Book([], [(0.88, 500)])
    eng._maybe_buy(w2, 1000)
    assert w2.position is not None


def test_stake_is_a_hard_cap_when_the_order_walks_the_book(tmp_path):
    eng = _bare_engine(tmp_path)
    w = _window(call="UP", p_model=0.95, p_mkt=0.80, p_final=0.875)
    w.book = tb.Book([(0.79, 100)], [(0.80, 5), (0.82, 5), (0.84, 1000)])
    eng._maybe_buy(w, 1000)
    assert w.position is not None and w.position.cost <= 20.005 and w.position.cost > 19.0
    assert w.position.avg_px > 0.80                                           # it did take the deeper levels


def test_maker_bid_keeps_its_dollar_stake_when_repriced(tmp_path):
    eng = _bare_engine(tmp_path, exec_mode="maker")
    w = _window(call="UP", p_model=0.90, p_mkt=0.80, p_final=0.85)
    w.book = tb.Book([(0.50, 100)], [(0.60, 100)])
    eng._maybe_buy(w, 1000)
    assert w.order and w.order["px"] == pytest.approx(0.51) and w.order["shares"] == pytest.approx(39.21, abs=0.02)
    w.book = tb.Book([(0.79, 100)], [(0.82, 100)])                           # the market moves up; the bid follows
    eng._work_order(w, 1001)
    assert w.order["px"] == pytest.approx(0.80) and w.order["shares"] * w.order["px"] <= 20.0 + 1e-9
    w.book = tb.Book([(0.70, 100)], [(0.80, 100)])
    eng._work_order(w, 1002)
    assert w.position is not None and w.position.cost <= 20.0 + 1e-9


def test_a_clock_jump_is_reported_as_sleep_and_delays_new_calls(tmp_path):
    clock = tb.SimClock(START)
    eng, clock = run_sim(tmp_path, 120, clock, tb.SimTransport(clock, seed=5))
    assert eng.sleep_gaps == 0 and eng.sleep_note() is None
    clock.advance(400)
    eng.tick()
    assert eng.sleep_gaps == 1 and "asleep" in eng.sleep_note()
    assert all(w.joined_late and w.joined_at == clock.time() for w in eng.active.values())
    assert any("asleep or paused" in t for _, t in eng.events)
    import makawan_web as tw
    assert any("asleep" in x for x in tw.snapshot(eng, clock, "simulation")["warnings"])


def test_keep_awake_is_a_no_op_off_macos(monkeypatch):
    monkeypatch.setattr(tb.sys, "platform", "linux")
    assert tb.keep_awake(say=lambda *_: None) is None


# ---- risk guard ---------------------------------------------------------------------------------------
def test_guard_halts_at_the_stop_and_stays_halted_after_a_restart(tmp_path):
    eng = _bare_engine(tmp_path, bankroll=100.0, flat_stake=5.0, stop_loss_frac=0.5)
    assert eng.floor() == 50.0
    eng.cash = 52.0                                                    # one more $5 loss would go through the floor
    w = _window(call="UP", p_model=0.93, p_mkt=0.85, p_final=0.89)
    w.book = tb.Book([(0.84, 500)], [(0.86, 500)])
    eng._maybe_buy(w, 1000)
    assert w.position is None and eng.halted and "halted" in w.value_note
    assert tb.read_state(str(tmp_path))["halted"] == eng.halted        # written down, so a restart cannot forget it
    assert any(r["event"] == "HALT" for r in rows(tmp_path, "events.csv"))
    eng2 = _bare_engine(tmp_path, bankroll=100.0, flat_stake=5.0)
    eng2.restore()
    assert eng2.halted and "HALTED" in eng2.guard_note()
    w2 = _window(call="UP", p_model=0.93, p_mkt=0.85, p_final=0.89)
    w2.book = tb.Book([(0.84, 500)], [(0.86, 500)])
    eng2._maybe_buy(w2, 1000)
    assert w2.position is None


def test_guard_allows_trades_above_the_floor_and_resume_clears_a_halt(tmp_path, capsys):
    eng = _bare_engine(tmp_path, bankroll=100.0, flat_stake=5.0, stop_loss_frac=0.5)
    eng.cash = 60.0
    w = _window(call="UP", p_model=0.93, p_mkt=0.85, p_final=0.89)
    w.book = tb.Book([(0.84, 500)], [(0.86, 500)])
    eng._maybe_buy(w, 1000)
    assert w.position is not None and w.position.cost <= 5.005 and not eng.halted
    eng.cash = 10.0                                                    # equity (cash + open cost) is now under $50
    eng.guard_check(1001)
    assert eng.halted and eng.guard_status()["halted"]
    tb.write_state(str(tmp_path), 100.0)                               # an ordinary restart keeps the halt...
    assert tb.read_state(str(tmp_path)).get("halted")
    tb.clear_halt(str(tmp_path), eng.rec, resume=False)
    assert tb.read_state(str(tmp_path)).get("halted") and "HALTED" in capsys.readouterr().out
    tb.clear_halt(str(tmp_path), eng.rec, resume=True)                 # ...and only --resume clears it
    assert not tb.read_state(str(tmp_path)).get("halted")
    assert tb.read_state(str(tmp_path))["bankroll"] == 100.0


def test_guard_counts_open_trades_as_if_they_all_lose(tmp_path):
    eng = _bare_engine(tmp_path, bankroll=100.0, flat_stake=5.0, stop_loss_frac=0.5)
    eng.cash = 58.0

    def win(h):
        w = _window(h, call="UP", p_model=0.93, p_mkt=0.85, p_final=0.89)
        w.book = tb.Book([(0.84, 500)], [(0.86, 500)])
        eng.active[h] = w
        return w
    a, b = win("5m"), win("15m")
    eng._maybe_buy(a, 1000)
    assert a.position is not None                                      # $58 -> worst case $53: allowed
    eng._maybe_buy(b, 1000)
    assert b.position is None and "waiting" in b.value_note            # worst case would be $48: not allowed
    assert not eng.halted                                              # ...but not a halt: the open trade may win
    eng.guard_check(1001)
    assert not eng.halted and eng.cash >= 50.0


def test_guard_halt_cancels_a_resting_maker_bid(tmp_path):
    eng = _bare_engine(tmp_path, exec_mode="maker", bankroll=100.0, flat_stake=5.0)
    w = _window(call="UP", p_model=0.93, p_mkt=0.85, p_final=0.89)
    w.book = tb.Book([(0.84, 500)], [(0.86, 500)])
    eng.active["15m"] = w
    eng._maybe_buy(w, 1000)
    assert w.order is not None
    eng.halt(1001, "test")
    assert w.order is None and eng.halted == "test"


def test_daily_loss_limit_pauses_until_the_next_day(tmp_path):
    tb.set_display_tz("America/New_York")
    eng = _bare_engine(tmp_path, bankroll=1000.0, daily_loss_limit=30.0, stop_loss_frac=0.0)
    now = 1_800_000_000.0
    eng.clock = tb.SimClock(now)
    eng.day_pnl[tb.day_key(now)] = -31.0
    eng.guard_check(now)
    assert eng.guard_blocks(now, 20.0) and not eng.halted and eng.guard_status()["paused_today"]
    assert eng.guard_blocks(now + 86400, 20.0) == ""                   # a new day starts clean
    assert any(r["event"] == "PAUSE" for r in rows(tmp_path, "events.csv"))
    assert eng.floor() is None                                         # stop-loss switched off with 0


def test_guard_settings_are_validated_and_shown():
    with pytest.raises(ValueError):
        tb.validate_config(tb.Config(stop_loss_frac=1.0))
    with pytest.raises(ValueError):
        tb.validate_config(tb.Config(daily_loss_limit=-1))
    text = tb.describe_model(tb.Config(bankroll=100.0, daily_loss_limit=15))
    assert "stop at -50% ($50)" in text and "daily loss limit $15" in text


def test_a_live_size_simulation_never_goes_under_the_floor(tmp_path):
    eng, _ = run_sim(tmp_path, 6000, bankroll=30.0, flat_stake=5.0, stop_loss_frac=0.5, trade_last_s={})
    assert eng.equity() >= 15.0 - 1e-6                                 # $5 trades can never take it below the $15 floor
    for r in rows(tmp_path, "events.csv"):
        if r["event"] == "BUY":
            assert float(r["shares"]) * float(r["price"]) + float(r["fee"] or 0) <= 5.01


def test_snapshot_carries_the_guard_for_the_watchdog(tmp_path):
    import makawan_web as tw
    eng, clock = run_sim(tmp_path, 400)
    snap = tw.snapshot(eng, clock, "simulation")
    assert snap["guard"]["floor"] == 500.0 and snap["guard"]["halted"] == "" and snap["last_tick"] > 0
    assert snap["version"] == tb.__version__
    json.dumps(snap, allow_nan=False)


# ---- watchdog -----------------------------------------------------------------------------------------
def _ok_state(now, **over):
    st = {"ready": True, "mode": "live", "last_tick": now - 1, "print_ts": now - 2, "last_error": "", "equity": 990.0,
          "bankroll": 1000.0, "books": "polling (1 request a second)",
          "guard": {"halted": "", "floor": 500.0, "paused_today": False, "today_pnl": 0.0}}
    st.update(over)
    return st


def test_watchdog_spots_each_kind_of_trouble():
    import makawan_watchdog as wd
    now = 1_800_000_000.0
    assert wd.check_bot("late", _ok_state(now), now) == {}
    assert "late:down" in wd.check_bot("late", None, now)
    assert "late:stuck" in wd.check_bot("late", _ok_state(now, last_tick=now - 200), now)
    assert "late:feed" in wd.check_bot("late", _ok_state(now, print_ts=now - 200), now)
    assert "late:feed" in wd.check_bot("late", _ok_state(now, last_error="HTTP 503"), now)
    g = {"halted": "stop-loss: equity $50.00", "floor": 50.0}
    msg, urgent, instant = wd.check_bot("late", _ok_state(now, guard=g), now)["late:halted"]
    assert "TRADING HALTED" in msg and urgent and instant
    near = wd.check_bot("late", _ok_state(now, equity=580.0), now)
    assert "late:near-stop" in near and "84%" in near["late:near-stop"][0]
    assert "late:stream" in wd.check_bot("late", _ok_state(now, books="off (kept disagreeing with polled prices); polling"), now)
    assert wd.check_bot("late", _ok_state(now, mode="simulation", last_tick=5.0, print_ts=5.0), now) == {}


def test_watchdog_messages_on_start_on_clear_and_hourly():
    import makawan_watchdog as wd
    tr = wd.Tracker(confirm=2, remind_s=3600)
    down = {"late:down": ("late: not answering.", True, False)}
    assert tr.update(down, 0) == ([], [], [])                          # one blip is not a problem yet
    started, cleared, remind = tr.update(down, 30)
    assert started == [("late: not answering.", True)] and not cleared and not remind
    assert tr.update(down, 60) == ([], [], [])                         # no repeat while it lasts...
    assert tr.update(down, 3700)[2]                                    # ...until the hourly reminder
    started, cleared, remind = tr.update({}, 3760)
    assert cleared == ["late: answering again (after 62 min)"]
    halt = {"any:halted": ("any: TRADING HALTED.", True, True)}
    assert tr.update(halt, 4000)[0]                                    # a halt is announced at once
    down_any = {"any:down": ("any: not answering.", True, False)}
    tr.update(down_any, 4030, silent=["any"])
    started, cleared, _ = tr.update(down_any, 4060, silent=["any"])
    assert started and not cleared                                     # a bot that went quiet has not "fixed" its halt
    assert "any:halted" in tr.active


def test_watchdog_reports_sleep_and_queues_phone_messages_when_offline():
    import makawan_watchdog as wd
    import requests
    tb.set_display_tz("America/New_York")
    sent, online = [], [False]

    def post(url, **kw):
        if not online[0]:
            raise requests.ConnectionError("offline")
        sent.append((url, kw["headers"]["Title"], kw["data"].decode()))

        class R:
            status_code = 200
        return R()
    n = wd.Notifier("my-topic", mac=False, post=post, say=lambda *a: None)
    now = 1_800_000_000.0
    dog = wd.Watchdog([("late", 1)], n, fetch=lambda port: _ok_state(dog.last_loop or now))
    dog.step(now)
    assert not sent and not n.queue
    dog.step(now + 5 * 3600)                                           # the Mac slept for five hours
    assert len(n.queue) == 1 and "asleep" in n.queue[0][1] and "5 h 00 min" in n.queue[0][1]
    online[0] = True
    dog.step(now + 5 * 3600 + 30)                                      # back online: the waiting message goes out
    assert not n.queue and sent[0][0] == "https://ntfy.sh/my-topic" and "delayed" not in sent[0][2]
    assert sent[0][1] == "Makawan: Mac was asleep"


def test_watchdog_ignores_the_live_bot_until_it_has_been_started():
    import makawan_watchdog as wd
    n = wd.Notifier("", mac=False, say=lambda *a: None)
    now = 1_800_000_000.0
    up = {"late": True, "LIVE": False}
    dog = wd.Watchdog(wd.parse_bots("late=1,LIVE?=2"), n,
                      fetch=lambda port: _ok_state(dog.last_loop) if up["late" if port == 1 else "LIVE"] else None)
    for i in range(4):
        assert dog.step(now + 30 * i) == {}                            # never started: not a problem
    up["LIVE"] = True
    dog.step(now + 150)
    up["LIVE"] = False
    assert "LIVE:down" in dog.step(now + 180)                          # it ran, then stopped: that is a problem


def test_watchdog_pings_the_heartbeat_only_while_a_bot_answers():
    import makawan_watchdog as wd
    pings = []
    n = wd.Notifier("", mac=False, say=lambda *a: None)
    now = 1_800_000_000.0
    alive = [True]
    dog = wd.Watchdog([("late", 1)], n, heartbeat="https://hc.example/abc", get=lambda url, **kw: pings.append(url),
                      fetch=lambda port: _ok_state(dog.last_loop) if alive[0] else None)
    dog.step(now)
    dog.step(now + 30)
    assert pings == ["https://hc.example/abc"]                         # at most about once a minute
    alive[0] = False
    dog.step(now + 90)
    assert len(pings) == 1                                             # no bot answering: no "all is well" ping


def test_schedule_waits_for_the_next_slot_and_catches_up_after_a_miss(tmp_path):
    import makawan_watchdog as wd
    from datetime import datetime
    tb.set_display_tz("America/New_York")
    t = datetime(2026, 11, 10, 10, 0, tzinfo=tb.DISPLAY_TZ).timestamp()      # a Tuesday, 10 AM
    jobs = {"daily report": {"at": (7, 0), "weekday": None, "cmd": ["x"]},
            "weekly tuner": {"at": (6, 30), "weekday": 6, "cmd": ["y"]}}
    path = str(tmp_path / "state.json")
    s = wd.Schedule(path, jobs, t)
    assert s.due(t) == []                                              # first start: nothing runs straight away
    assert s.due(t + 20 * 3600) == []                                  # 6 AM Wednesday: not yet
    assert s.due(t + 21.5 * 3600) == ["daily report"]                  # 7:30 AM Wednesday

    class P:
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return ("blah\nSUMMARY: late +$5.00 (3 trades)\n", None)
    s.start("daily report", t + 21.5 * 3600, popen=lambda *a, **k: P())
    assert s.due(t + 21.5 * 3600) == []                                # not started twice
    done = s.finished(t + 21.6 * 3600)
    assert done[0][0] == "daily report" and wd.headline(done[0][2], "x") == "late +$5.00 (3 trades)"
    s2 = wd.Schedule(path, jobs, t + 22 * 3600)                        # restart the watchdog: it remembers
    assert s2.due(t + 22 * 3600) == []
    assert s2.due(t + 6 * 86400) == ["daily report", "weekly tuner"]   # Monday, after a missed Sunday slot
    assert wd.parse_hhmm("off") is None and wd.parse_hhmm("07:05") == (7, 5)
    assert wd.parse_bots("late=8765,any maker=8768") == [("late", 8765), ("any maker", 8768)]


# ---- daily report -------------------------------------------------------------------------------------
def _log_folder(root, name, trades, bankroll=1000.0):
    """A small hand-made bot folder. trades: (minutes after T0, horizon, side, shares, px, outcome, market or None)."""
    import makawan_report as rp
    d = root / name
    d.mkdir()
    t0 = 1_800_000_000.0 - 1_800_000_000 % 900
    ev, wn = [tb.Recorder.EVENT], [tb.Recorder.WIN]
    for mins, h, side, shares, px, outcome, mkt in trades:
        ws = t0 + mins * 60
        we = ws + tb.HORIZON_SECONDS[h]
        slug = f"cpc-btc-updown-{h}-" + rp.datetime.fromtimestamp(ws, tb.timezone.utc).strftime("%Y-%m-%d-%H%M") + "z"
        fee = tb.exch_fee(px, shares, 0.0695)
        net = (shares if side == outcome else 0.0) - shares * px - fee
        up = (lambda p: p if side == "UP" else 1 - p)
        ev.append([tb.iso(we - 60), "", h, slug, "BUY", side, "", round(up(min(0.99, px + 0.08)), 4),
                   "" if mkt is None else round(up(mkt), 4), px, shares, fee, "", "taker"])
        row = dict.fromkeys(tb.Recorder.WIN, "")
        row.update(window_start=tb.iso(ws), window_end=tb.iso(we), horizon=h, slug=slug, price_to_beat=100,
                   settlement=101 if outcome == "UP" else 99, outcome=outcome, provisional=0, traded_side=side,
                   shares=shares, avg_px=px, liquidity="taker", gross_pnl=round(net + fee, 2), fee=fee,
                   net_pnl=round(net, 2), pnl=round(net, 2))
        wn.append([row[c] for c in tb.Recorder.WIN])
    for fn, data in (("events.csv", ev), ("windows.csv", wn)):
        with open(d / fn, "w", newline="") as f:
            csv.writer(f).writerows(data)
    tb.write_state(str(d), bankroll)
    return str(d), t0


def test_report_adds_up_and_flags_the_odd_trade(tmp_path):
    import makawan_report as rp
    tb.set_display_tz("America/New_York")
    good = [(i * 5, "5m", "UP", 22, 0.88, "UP", 0.80) for i in range(8)]
    bad = [(45, "15m", "UP", 1871, 0.02, "DOWN", None), (60, "5m", "DOWN", 30, 0.60, "UP", 0.55)]
    late, t0 = _log_folder(tmp_path, "makawan_out_late", good + bad)
    anyf, _ = _log_folder(tmp_path, "makawan_out_any", good[:2])
    assert [rp.label_for(f) for f in rp.find_folders(str(tmp_path))] == ["late", "any"]
    sections, summary, info = rp.build([late, anyf], t0 - 60, t0 + 4 * 3600)
    b = info["bots"][0]
    assert b["sum"]["n"] == 10 and b["sum"]["won"] == 8
    want = sum(r_["net"] for r_ in b["trades"])
    assert b["sum"]["net"] == pytest.approx(want) and b["sum"]["gross"] - b["sum"]["fees"] == pytest.approx(want)
    assert info["total"] == pytest.approx(want + info["bots"][1]["sum"]["net"])
    odd = "\n".join(info["odd"])
    assert "over the $20 stake limit" in odd and "no market price" in odd and "under 5c" in odd
    assert "late " in summary[0] and "any " in summary[0] and "thing(s) to look at" in summary[1]
    text, page = rp.as_text("T", sections), rp.as_html("T", sections)
    assert "Profit and loss by bot" in text and "BY PRICE PAID" in text and "under 25c" in text
    assert "<table>" in page and "prefers-color-scheme" in page
    quiet, _, info2 = rp.build([anyf], t0 - 60, t0 + 4 * 3600)
    assert not any("stake limit" in o for o in info2["odd"])


def test_report_measures_running_time_from_the_tick_log(tmp_path):
    import makawan_report as rp
    tb.set_display_tz("America/New_York")
    d, t0 = _log_folder(tmp_path, "makawan_out_late", [(0, "5m", "UP", 22, 0.88, "UP", 0.80)])
    with open(os.path.join(d, "ticks.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(tb.Recorder.TICK)
        for t in list(range(0, 3600, 5)) + list(range(3 * 3600, 4 * 3600, 5)):      # one hour on, two off, one on
            w.writerow([tb.iso(t0 + t), "5m", "x", 1] + [""] * 9)
    share, gaps, exact = rp.running(d, [], t0, t0 + 4 * 3600)
    assert exact and len(gaps) == 1 and gaps[0][1] - gaps[0][0] == pytest.approx(2 * 3600 + 5)
    assert share == pytest.approx(0.5, abs=0.01)
    assert len(rp.tick_times(os.path.join(d, "ticks.csv"), t0 + 3 * 3600, t0 + 4 * 3600)) == 720
    sections, _, info = rp.build([d], t0, t0 + 4 * 3600)
    assert any("Not running for 2 h 00 min" in o for o in info["odd"])


# ---- reconciler ---------------------------------------------------------------------------------------
class _FakeAccount:
    """Stands in for the Polymarket library: same method names and answer shapes, nothing else."""

    def __init__(self, fills, balance=95.0):
        self.calls = []
        self.account, self.portfolio = self, self
        self._fills, self._balance = fills, balance

    def balances(self):
        self.calls.append("balances")
        return {"balances": [{"currency": "USD", "currentBalance": self._balance, "buyingPower": self._balance}]}

    def positions(self, params=None):
        self.calls.append("positions")
        return {"positions": {}}

    def activities(self, params=None):
        self.calls.append("activities")
        acts = [{"type": "ACTIVITY_TYPE_TRADE", "trade": {
            "marketSlug": s, "createTime": tb.iso(t), "price": {"value": str(px), "currency": "USD"},
            "qtyDecimal": str(q), "isAggressor": True}} for s, t, px, q in self._fills]
        return {"activities": acts, "eof": True}


def test_reconciler_finds_slippage_missed_and_unknown_fills(tmp_path):
    import makawan_reconcile as rc
    tb.set_display_tz("America/New_York")
    now = 1_800_000_000.0
    a, b, c, d = (f"cpc-btc-updown-5m-2027-01-15-08{m:02d}z" for m in (0, 5, 10, 15))
    with open(tmp_path / "events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(tb.Recorder.EVENT)
        w.writerow([tb.iso(now - 600), "", "5m", a, "BUY", "UP", "", "", "", 0.80, 6, 0.07, "", ""])     # paid 82c really
        w.writerow([tb.iso(now - 500), "", "5m", b, "BUY", "DOWN", "", "", "", 0.70, 7, 0.10, "", ""])   # quoted as 30c UP
        w.writerow([tb.iso(now - 400), "", "5m", c, "BUY", "UP", "", "", "", 0.90, 5, 0.03, "", ""])     # never filled
    tb.write_state(str(tmp_path), 100.0)
    fake = _FakeAccount([(a, now - 599, 0.82, 6), (b, now - 499, 0.30, 7), (d, now - 300, 0.55, 4),
                         ("some-other-market", now - 200, 0.5, 9), (a, now - 90000, 0.5, 9)])
    bot, info = rc.bot_record(str(tmp_path), now - 3600, now)
    acct = rc.fetch_account(fake, now - 3600)
    assert len(acct["fills"]) == 3 and set(fake.calls) == {"balances", "positions", "activities"}   # reads only
    got = {r["slug"]: r for r in rc.reconcile(bot, acct["fills"])}
    assert got[a]["status"] == "paid more than recorded" and got[a]["price_diff_cents"] == pytest.approx(2.0)
    assert got[a]["slippage_dollars"] == pytest.approx(0.12)
    assert got[b]["status"] == "matched" and got[b]["real_price"] == pytest.approx(0.70)
    assert got[c]["status"].startswith("MISSED") and got[d]["status"].startswith("UNKNOWN")
    lines = rc.summarize(list(got.values()), acct, info)
    assert "2 in both records, 1 missed, 1 unknown" in lines[0] and "$+0.12" in lines[1] and "$95.00" in lines[2]
    path = rc.save(str(tmp_path), list(got.values()), now)
    saved = rows(tmp_path, "reconcile.csv")
    assert len(saved) == 4 and saved[0]["window_end_et"] == "2027-01-15 03:05:00" and os.path.basename(path) == "reconcile.csv"
    assert not info["live"]


def test_reconciler_and_checks_never_call_anything_that_trades():
    here = os.path.dirname(os.path.abspath(tb.__file__))
    for f in ("makawan_reconcile.py", "makawan_watchdog.py", "makawan_report.py", "makawan_connect_check.py",
              "makawan_latency_check.py", "makawan_tune.py", "simulate_week.py", "run_both.py", "makawan_bot.py",
              "makawan_web.py"):
        src = open(os.path.join(here, f)).read()
        for word in ("orders.create", "orders.cancel", "orders.modify", "close_position", ".withdraw", ".transfer"):
            assert word not in src, (f, word)


# ---- tuner: propose only ------------------------------------------------------------------------------
def test_tuner_proposal_changes_nothing_until_applied(tmp_path):
    import argparse
    import makawan_tune as mt
    out = str(tmp_path)
    said = []
    changed = mt.write_proposal(out, {"5m": {"vol_scale": 1.15, "tail_w": 0.10}}, "test", said.append)
    assert changed == {"5m": {"vol_scale": 1.15}}                      # tail_w already has that value: not a change
    assert os.path.exists(os.path.join(out, "params_proposed.json")) and not os.path.exists(os.path.join(out, "params.json"))
    assert "Nothing has been changed" in open(os.path.join(out, "tune_proposal.txt")).read()
    assert any(s.startswith("SUMMARY: Tuner: proposes changes for 5m") for s in said)
    mt.run_apply(argparse.Namespace(out=out), said.append)             # the user's decision
    cfg = tb.Config()
    tb.load_params(cfg, os.path.join(out, "params.json"))
    assert cfg.view("5m").vol_scale == 1.15 and cfg.view("15m").vol_scale == tb.Config().vol_scale
    assert not os.path.exists(os.path.join(out, "params_proposed.json"))
    assert mt.write_proposal(out, {"5m": {"vol_scale": 1.15}}, "again", said.append) == {}     # already in use
    mt.run_revert(argparse.Namespace(out=out), said.append)
    assert not os.path.exists(os.path.join(out, "params.json"))
    with pytest.raises(SystemExit):
        mt.run_apply(argparse.Namespace(out=out), said.append)


def test_launcher_passes_limits_through_and_starts_the_watchdog():
    src = open(os.path.join(os.path.dirname(os.path.abspath(tb.__file__)), "run_both.py")).read()
    for word in ("--stake", "--stop-loss", "--daily-loss", "--resume", "makawan_watchdog.py", "--no-watchdog"):
        assert word in src, word


# ---- live orders (against a stand-in exchange; nothing here can reach Polymarket) ---------------------
class _FakeExchange:
    """Stands in for Polymarket's library. `mode` decides how the next order is answered."""

    def __init__(self, mode="fill", fill_long_px=None):
        self.orders = self
        self.mode, self.fill_long_px = mode, fill_long_px
        self.created, self.previewed, self.cancelled, self.lookups = [], [], [], 0

    def _order(self, req, state, cum, avg):
        return {"id": "ord-1", "marketSlug": req["marketSlug"], "state": state, "cumQuantity": cum,
                "avgPx": {"value": str(avg), "currency": "USD"}, "quantity": req["quantity"]}

    def create(self, req):
        self.created.append(req)
        px = self.fill_long_px if self.fill_long_px is not None else float(req["price"]["value"])
        q = req["quantity"]
        if self.mode == "boom":
            raise ConnectionError("network dropped")
        if self.mode == "fill":
            return {"id": "ord-1", "executions": [{"type": "EXECUTION_TYPE_FILL", "lastShares": str(q),
                    "lastPx": {"value": str(px)}, "order": self._order(req, "ORDER_STATE_FILLED", q, px)}]}
        if self.mode == "partial":
            return {"id": "ord-1", "executions": [
                {"type": "EXECUTION_TYPE_PARTIAL_FILL", "lastShares": "2", "lastPx": {"value": str(px)},
                 "order": self._order(req, "ORDER_STATE_PARTIALLY_FILLED", 2, px)},
                {"type": "EXECUTION_TYPE_CANCELED", "order": self._order(req, "ORDER_STATE_CANCELED", 2, px)}]}
        if self.mode == "miss":
            return {"id": "ord-1", "executions": [{"type": "EXECUTION_TYPE_CANCELED",
                                                   "order": self._order(req, "ORDER_STATE_CANCELED", 0, 0)}]}
        if self.mode == "reject":
            return {"id": "ord-1", "executions": [{"type": "EXECUTION_TYPE_REJECTED",
                                                   "orderRejectReason": "ORD_REJECT_REASON_PRICE_OUT_OF_BOUNDS"}]}
        return {"id": "ord-1"}                         # "slow" and "stuck": no word yet

    def retrieve(self, oid):
        self.lookups += 1
        req = self.created[-1]
        if self.mode == "slow" and self.lookups >= 2:
            return {"order": self._order(req, "ORDER_STATE_FILLED", req["quantity"], float(req["price"]["value"]))}
        return {"order": self._order(req, "ORDER_STATE_NEW", 0, 0)}

    def cancel(self, oid, params):
        self.cancelled.append(oid)

    def preview(self, params):
        self.previewed.append(params)
        return {"order": {"state": "ORDER_STATE_NEW"}}


def _live_engine(tmp_path, ex, rehearse=False, **over):
    import makawan_live as ml
    kw = dict(bankroll=100.0, flat_stake=5.0, stop_loss_frac=0.5)
    kw.update(over)
    eng = _bare_engine(tmp_path, **kw)
    eng.broker = ml.LiveBroker(ex, str(tmp_path), rehearse=rehearse, sleep=lambda s: None)
    eng.last_print_ts = 1000
    return eng


def _live_window(side="UP", bid=0.84, ask=0.86, h="15m", p=None):
    p = p if p is not None else (0.93 if side == "UP" else 0.07)
    m = (bid + ask) / 2
    w = _window(h, call=side, p_model=p, p_mkt=m, p_final=(p + m) / 2)
    w.book = tb.Book([(bid, 500)], [(ask, 500)])
    return w


def test_live_up_order_is_capped_and_recorded_from_the_real_fill(tmp_path):
    ex = _FakeExchange("fill", fill_long_px=0.86)
    eng = _live_engine(tmp_path, ex)
    w = _live_window("UP")
    eng._maybe_buy(w, 1000)
    req = ex.created[0]
    assert req["intent"] == "ORDER_INTENT_BUY_LONG" and req["type"] == "ORDER_TYPE_LIMIT"
    assert req["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" and req["synchronousExecution"] is True
    assert req["price"] == {"value": "0.86", "currency": "USD"}       # never above the price that keeps an edge
    assert req["quantity"] == 5 and 5 * 0.86 + tb.exch_fee(0.86, 5, 0.095) <= 5.0    # worst case fits the $5 stake
    assert w.position.shares == 5 and w.position.avg_px == pytest.approx(0.86)   # what was really paid, not the limit
    assert eng.cash == pytest.approx(100 - w.position.cost) and w.position.cost <= 5.0
    evs = [r["event"] for r in rows(tmp_path, "events.csv")]
    assert evs == ["SEND", "BUY"] and "LIVE order ord-1" in rows(tmp_path, "events.csv")[1]["note"]
    eng._maybe_buy(w, 1001)
    assert len(ex.created) == 1                                        # one order per window, ever
    log = [json.loads(x) for x in open(tmp_path / "live_orders.jsonl")]
    assert [x["kind"] for x in log] == ["request", "answer"]


def test_live_down_order_uses_the_long_side_price(tmp_path):
    ex = _FakeExchange("fill", fill_long_px=0.70)                      # DOWN bought at 30c = long price 70c
    eng = _live_engine(tmp_path, ex)
    w = _live_window("DOWN", bid=0.70, ask=0.72, p=0.58)                       # UP book 70/72 -> DOWN costs 30c
    eng._maybe_buy(w, 1000)
    req = ex.created[0]
    assert req["intent"] == "ORDER_INTENT_BUY_SHORT" and req["price"]["value"] == "0.7"    # 1 - (30c + 0c slip)
    assert w.position.side == "DOWN" and w.position.avg_px == pytest.approx(0.30) and w.position.shares == 15
    assert w.position.cost <= 5.0 and not eng.halted


def test_live_partial_fill_and_no_fill(tmp_path):
    ex = _FakeExchange("partial", fill_long_px=0.86)
    eng = _live_engine(tmp_path, ex)
    w = _live_window("UP")
    eng._maybe_buy(w, 1000)
    assert w.position.shares == 2 and eng.cash == pytest.approx(100 - w.position.cost)
    ex2 = _FakeExchange("miss")
    eng2 = _live_engine(tmp_path / "b", ex2)
    w2 = _live_window("UP")
    eng2._maybe_buy(w2, 1000)
    assert w2.position is None and eng2.cash == 100.0 and not eng2.halted
    assert [r["event"] for r in rows(tmp_path / "b", "events.csv")] == ["SEND", "NOFILL"]
    eng2._maybe_buy(w2, 1001)
    assert len(ex2.created) == 1                                       # no second try in the same window


def test_live_slow_answer_is_looked_up_and_a_stuck_order_is_cancelled_and_halts(tmp_path):
    ex = _FakeExchange("slow")
    eng = _live_engine(tmp_path, ex)
    w = _live_window("UP")
    eng._maybe_buy(w, 1000)
    assert w.position is not None and w.position.shares == 5 and not eng.halted and ex.lookups == 2
    ex2 = _FakeExchange("stuck")
    eng2 = _live_engine(tmp_path / "b", ex2)
    w2 = _live_window("UP")
    eng2._maybe_buy(w2, 1000)
    assert ex2.cancelled == ["ord-1"]                                  # it must not be left resting
    assert w2.position is None and "could not be confirmed" in eng2.halted


def test_live_halts_when_an_order_fails_in_transit_or_fills_beyond_its_limit(tmp_path):
    ex = _FakeExchange("boom")
    eng = _live_engine(tmp_path, ex)
    eng._maybe_buy(_live_window("UP"), 1000)
    assert "result is unknown" in eng.halted
    ex2 = _FakeExchange("fill", fill_long_px=0.95)                     # filled 8c above the 87c limit
    eng2 = _live_engine(tmp_path / "b", ex2)
    w = _live_window("UP")
    eng2._maybe_buy(w, 1000)
    assert w.position is not None and "worse than its limit" in eng2.halted
    w3 = _live_window("UP", h="5m")
    eng2._maybe_buy(w3, 1001)
    assert len(ex2.created) == 1                                       # halted: nothing more is sent


def test_live_three_rejections_in_a_row_halt(tmp_path):
    ex = _FakeExchange("reject")
    eng = _live_engine(tmp_path, ex)
    for i, h in enumerate(("5m", "15m")):
        eng._maybe_buy(_live_window("UP", h=h), 1000 + i)
    assert not eng.halted and len(ex.created) == 2
    w = _live_window("UP")
    w.info.slug = "third"
    eng._maybe_buy(w, 1003)
    assert "rejected three real orders" in eng.halted and eng.cash == 100.0


def test_live_respects_the_stop_loss_the_trade_limit_and_stale_prices(tmp_path):
    ex = _FakeExchange("fill")
    eng = _live_engine(tmp_path, ex)
    eng.cash = 52.0                                                    # one more $5 loss would cross the $50 floor
    eng._maybe_buy(_live_window("UP"), 1000)
    assert not ex.created and "stop-loss" in eng.halted
    ex2 = _FakeExchange("fill")
    eng2 = _live_engine(tmp_path / "b", ex2, live_max_trades=1)
    eng2._maybe_buy(_live_window("UP"), 1000)
    assert len(ex2.created) == 1 and "limit of 1 real trade" in eng2.halted
    ex3 = _FakeExchange("fill")
    eng3 = _live_engine(tmp_path / "c", ex3)
    eng3.last_print_ts = 900                                           # the index price is 100 s old
    w = _live_window("UP")
    eng3._maybe_buy(w, 1000)
    assert not ex3.created and "too old" in w.value_note


def test_live_restart_never_resends_and_halts_on_an_order_with_no_recorded_result(tmp_path):
    ex = _FakeExchange("fill")
    eng = _live_engine(tmp_path, ex)
    w = _live_window("UP")
    eng._maybe_buy(w, 1000)
    eng2 = _live_engine(tmp_path, _FakeExchange("fill"))
    eng2.restore()
    assert w.info.slug in eng2.sent and eng2.live_fills == 1 and not eng2.halted
    with open(tmp_path / "events.csv", "a", newline="") as f:          # the bot died between sending and the answer
        csv.writer(f).writerow([tb.iso(1005), "", "5m", "lost-one", "SEND", "UP", "", "", "", 0.87, 5, "", "", "LIVE"])
    eng3 = _live_engine(tmp_path, _FakeExchange("fill"))
    eng3.restore()
    assert "no recorded result" in eng3.halted and "lost-one" in eng3.halted


def test_rehearsal_previews_but_never_creates_an_order(tmp_path):
    ex = _FakeExchange("fill")
    eng = _live_engine(tmp_path, ex, rehearse=True)
    w = _live_window("DOWN", bid=0.70, ask=0.72, p=0.58)
    eng._maybe_buy(w, 1000)
    assert ex.created == [] and len(ex.previewed) == 1
    assert ex.previewed[0]["request"]["intent"] == "ORDER_INTENT_BUY_SHORT"
    assert w.position is not None                                      # the fill shown in a rehearsal is paper
    assert "SEND" not in [r["event"] for r in rows(tmp_path, "events.csv")]


def test_live_order_format_and_answer_reading():
    import makawan_live as ml
    r = ml.order_request("slug", "DOWN", 0.255, 12.5, tick=0.005)
    assert r["price"]["value"] == "0.745" and r["quantity"] == 12.5 and r["maxBlockTime"] == "2"
    with pytest.raises(ValueError):
        ml.order_request("slug", "UP", 0.995, 1)
    assert ml.side_price(0.70, "DOWN", 0.31) == pytest.approx(0.30)
    assert ml.side_price(0.21, "DOWN", 0.80) == pytest.approx(0.79)
    assert ml.side_price(0.51, "DOWN", 0.52) == pytest.approx(0.49)    # Nov 11 02:32: was wrongly read as 51c
    assert ml.side_price(0.69, "DOWN", 0.55) == pytest.approx(0.31)    # Nov 11 03:02: was wrongly read as 69c
    res = ml.read_result({"id": "x", "executions": [{"type": "EXECUTION_TYPE_FILL", "lastShares": "5",
                          "lastPx": {"value": "0.86"}, "commissionNotionalCollected": {"value": "0.04"}}]}, "UP", 0.87)
    assert res["final"] and res["filled"] == 5 and res["price"] == 0.86 and res["fee"] == 0.04
    assert not ml.read_result({"id": "x"}, "UP", 0.87)["final"]
    src = open(os.path.join(os.path.dirname(os.path.abspath(tb.__file__)), "makawan_live.py")).read()
    assert src.count("orders.create(") == 3 and "input(" in src        # buy now, hybrid resting order, sell (--cut) and "POLYMARKET_SECRET_KEY" in src
    for bad in ("close_position", "ORDER_TYPE_MARKET", "GOOD_TILL_CANCEL", ".withdraw", "create_many"):
        assert bad not in src, bad


def test_no_buying_on_an_old_call_right_after_waking_up(tmp_path):
    eng, clock = run_sim(tmp_path, 200, trade_last_s={})
    bought = []
    eng._maybe_buy = lambda w, now: bought.append(w.info.slug)
    w = eng.active["1h"]                                               # a window that outlives the nap
    w.call, w.entered, w.position, w.order = "UP", False, None, None
    clock.advance(45)                                                  # the Mac slept for 45 seconds
    eng.tick()
    assert eng.sleep_gaps == 1 and eng.active["1h"] is w
    assert w.info.slug not in bought and w.value_note == "waiting after a gap before trading"
    for _ in range(10):
        clock.advance(1)
        eng.tick()
    assert w.info.slug not in bought                                   # still inside the 20-second wait


def test_reads_the_first_real_polymarket_answers_correctly():
    """The two answers Polymarket really sent on Nov 10, 2026 (trimmed to the fields that matter)."""
    import makawan_live as ml

    def ex(kind, state, cum, avg, shares="0.0000", px="0.0000", fee="0.0000", total="0.0000"):
        return {"type": kind, "lastShares": shares, "lastPx": {"value": px}, "orderRejectReason": "ORD_REJECT_REASON_EXCHANGE_OPTION",
                "commissionNotionalCollected": {"value": fee},
                "order": {"id": "DN2", "state": state, "cumQuantity": cum, "avgPx": {"value": avg},
                          "commissionNotionalTotalCollected": {"value": total}}}
    filled = {"id": "DN2", "executions": [
        ex("EXECUTION_TYPE_NEW", "ORDER_STATE_NEW", 0, "0.0000"),
        ex("EXECUTION_TYPE_PARTIAL_FILL", "ORDER_STATE_PARTIALLY_FILLED", 0.01, "0.4100", "0.0100", "0.4100"),
        ex("EXECUTION_TYPE_PARTIAL_FILL", "ORDER_STATE_PARTIALLY_FILLED", 3.01, "0.4100", "3.0000", "0.4100", "0.0700", "0.0700"),
        ex("EXECUTION_TYPE_FILL", "ORDER_STATE_FILLED", 11.44, "0.4200", "8.4300", "0.4200", "0.1900", "0.2600")]}
    r = ml.read_result(filled, "UP", 0.42)
    assert r["final"] and r["filled"] == pytest.approx(11.44) and r["fee"] == pytest.approx(0.26)
    assert r["price"] == pytest.approx((3.01 * 0.41 + 8.43 * 0.42) / 11.44)        # 41.7c, not the rounded 42c
    assert 0.090 < r["fee"] / (r["filled"] * r["price"] * (1 - r["price"])) < 0.097   # the real fee rate
    missed = {"id": "DN1", "executions": [ex("EXECUTION_TYPE_NEW", "ORDER_STATE_NEW", 0, "0.0000"),
                                          ex("EXECUTION_TYPE_EXPIRED", "ORDER_STATE_EXPIRED", 0, "0.0000")]}
    r = ml.read_result(missed, "UP", 0.81)
    assert r["final"] and r["filled"] == 0 and not r["rejected"] and r["status"].startswith("expired")
    assert tb.Config().fee_theta == 0.095


def test_live_retries_a_miss_three_times_then_skips_the_window(tmp_path):
    ex = _FakeExchange("miss")
    eng = _live_engine(tmp_path, ex, live_retries=3)
    w = _live_window("UP")
    eng.active["15m"] = w
    eng._maybe_buy(w, 1000)
    assert len(ex.created) == 1 and not w.entered and "retry 1 of 3" in w.value_note
    eng._maybe_buy(w, 1001)
    assert len(ex.created) == 1                                        # waits 3 seconds between tries
    for t in (1003, 1006, 1009):
        eng.last_print_ts = t                                          # fresh index prices keep arriving
        eng._maybe_buy(w, t)
    assert len(ex.created) == 4 and w.entered and w.position is None   # first try + 3 retries
    eng.last_print_ts = 1020
    eng._maybe_buy(w, 1020)
    assert len(ex.created) == 4 and eng.cash == 100.0 and not eng.halted
    notes = [r["note"] for r in rows(tmp_path, "events.csv") if r["event"] == "SEND"]
    assert "retry" not in notes[0] and notes[3].endswith("retry 3 of 3")
    eng2 = _live_engine(tmp_path, _FakeExchange("miss"), live_retries=3)               # a restart remembers the tries
    eng2.restore()
    assert eng2.sent[w.info.slug] == 4 and not eng2.halted


def test_live_retry_stops_at_a_fill_and_rechecks_the_price(tmp_path):
    ex = _FakeExchange("miss")
    eng = _live_engine(tmp_path, ex, live_retries=3)
    w = _live_window("UP")
    eng._maybe_buy(w, 1000)
    ex.mode = "fill"
    eng._maybe_buy(w, 1003)
    assert len(ex.created) == 2 and w.position is not None and w.entered
    eng._maybe_buy(w, 1010)
    assert len(ex.created) == 2                                        # one position per window
    eng2 = _live_engine(tmp_path, _FakeExchange("fill"), live_retries=3)
    eng2.restore()
    assert not eng2.halted and w.info.slug in eng2.done                # a filled window is closed after a restart
    ex3 = _FakeExchange("miss")
    eng3 = _live_engine(tmp_path / "c", ex3, live_retries=3)
    w3 = _live_window("UP")
    eng3._maybe_buy(w3, 1000)
    w3.book = tb.Book([(0.95, 500)], [(0.97, 500)])                    # the price ran away: no edge left
    eng3._maybe_buy(w3, 1003)
    assert len(ex3.created) == 1                                       # a retry never pays more than allowed


def test_live_no_retry_after_a_rejection_or_an_unknown_result(tmp_path):
    ex = _FakeExchange("reject")
    eng = _live_engine(tmp_path, ex, live_retries=3)
    w = _live_window("UP")
    eng._maybe_buy(w, 1000)
    eng._maybe_buy(w, 1004)
    assert len(ex.created) == 1 and w.entered
    ex2 = _FakeExchange("stuck")
    eng2 = _live_engine(tmp_path / "b", ex2, live_retries=3)
    w2 = _live_window("UP")
    eng2._maybe_buy(w2, 1000)
    eng2._maybe_buy(w2, 1004)
    assert len(ex2.created) == 1 and eng2.halted
    eng3 = _live_engine(tmp_path / "c", _FakeExchange("miss"), live_retries=0)
    w4 = _live_window("UP")
    eng3._maybe_buy(w4, 1000)
    eng3._maybe_buy(w4, 1004)
    assert eng3.broker.client.created.__len__() == 1                   # --retries 0 = one try, as before


def test_live_default_is_one_taker_order_per_window(tmp_path):
    assert tb.Config().live_retries == 0
    ex = _FakeExchange("miss")
    eng = _live_engine(tmp_path, ex)
    w = _live_window("UP")
    eng._maybe_buy(w, 1000)
    eng.last_print_ts = 1010
    eng._maybe_buy(w, 1010)
    assert len(ex.created) == 1 and w.entered and w.position is None   # not filled: the window is left alone



# ---- hybrid: a real resting limit order after a missed fill-now order ---------------------------------
class _RestingExchange(_FakeExchange):
    """The fill-now order misses; the resting order then fills as `script` says, one entry per lookup."""

    def __init__(self, script=(), cancel_fills=0.0):
        super().__init__("miss")
        self.script, self.cancel_fills, self.state = list(script), cancel_fills, {}

    def create(self, req):
        if req["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL":
            return super().create(req)
        self.created.append(req)
        self.state = {"cum": 0.0, "px": float(req["price"]["value"]), "st": "ORDER_STATE_NEW", "q": req["quantity"]}
        return {"id": "R1"}

    def retrieve(self, oid):
        self.lookups += 1
        st = self.state
        if self.script and st["st"] == "ORDER_STATE_NEW":
            st["cum"] = max(st["cum"], self.script.pop(0))
            if st["cum"] >= st["q"]:
                st["st"] = "ORDER_STATE_FILLED"
        return {"order": {"id": oid, "state": st["st"], "cumQuantity": st["cum"], "avgPx": {"value": str(st["px"])},
                          "commissionNotionalTotalCollected": {"value": "0"}}}

    def cancel(self, oid, params):
        self.cancelled.append(oid)
        st = self.state
        st["cum"] = max(st["cum"], self.cancel_fills)                   # a fill can slip in before the cancel lands
        st["st"] = "ORDER_STATE_FILLED" if st["cum"] >= st["q"] else "ORDER_STATE_CANCELED"


def _hybrid(tmp_path, ex, **over):
    eng = _live_engine(tmp_path, ex, live_rest=True, **dict(dict(live_rest_max_s=60.0), **over))
    w = _live_window("UP")
    eng.active["15m"] = w
    return eng, w


def test_hybrid_rests_a_limit_order_after_a_miss_and_books_its_fills(tmp_path):
    ex = _RestingExchange(script=[0, 2, 5])
    eng, w = _hybrid(tmp_path, ex)
    eng._maybe_buy(w, 1000)
    ioc, rest = ex.created
    assert ioc["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" and rest["tif"] == "TIME_IN_FORCE_GOOD_TILL_DATE"
    assert rest["price"] == ioc["price"] and rest["quantity"] == ioc["quantity"]       # same price, same size
    assert rest["goodTillTime"] == tb.iso(int(w.info.we - 20)) and "synchronousExecution" not in rest
    assert w.live_order and w.live_order["id"] == "R1" and w.entered
    for t in (1001, 1002, 1004, 1006):
        eng._work_live_order(w, t, eng.cfg)
    assert w.position.shares == 5 and w.live_order is None and w.trade["liquidity"] == "maker"
    assert w.position.cost == pytest.approx(5 * 0.86) and eng.cash == pytest.approx(100 - 5 * 0.86)
    buys = [r for r in rows(tmp_path, "events.csv") if r["event"] == "BUY"]
    assert [float(b["shares"]) for b in buys] == [2, 3]                # booked piece by piece
    assert [r["event"] for r in rows(tmp_path, "events.csv")] == ["SEND", "NOFILL", "REST", "BUY", "BUY"]
    eng._maybe_buy(w, 1010)
    assert len(ex.created) == 2                                        # one position per window
    eng2 = _live_engine(tmp_path, _RestingExchange(), live_rest=True)  # a restart rebuilds the whole position
    eng2.restore()
    assert eng2.cash == pytest.approx(100 - 5 * 0.86) and not eng2.halted     # both pieces counted once each


def test_hybrid_cancels_when_the_call_changes_or_the_window_closes_and_books_late_fills(tmp_path):
    ex = _RestingExchange(script=[0], cancel_fills=1.0)
    eng, w = _hybrid(tmp_path, ex)
    eng._maybe_buy(w, 1000)
    w.call = "DOWN"
    eng._work_live_order(w, 1003, eng.cfg)
    assert ex.cancelled == ["R1"] and w.live_order is None
    assert w.position.shares == 1                                      # the fill that beat the cancel is counted
    assert "the call changed" in rows(tmp_path, "events.csv")[-1]["note"]
    ex2 = _RestingExchange()
    eng2, w2 = _hybrid(tmp_path / "b", ex2)
    eng2._maybe_buy(w2, 1000)
    w2.tau = 15
    eng2._work_live_order(w2, 1003, eng2.cfg)
    assert ex2.cancelled == ["R1"] and w2.position is None and w2.order_status == "unfilled"
    ex3 = _RestingExchange()
    eng3, w3 = _hybrid(tmp_path / "c", ex3)
    eng3._maybe_buy(w3, 1000)
    eng3.halt(1002, "test")
    assert ex3.cancelled == ["R1"] and w3.live_order is None           # a halt cancels resting orders


def test_hybrid_cancels_when_the_edge_is_gone_and_counts_the_order_against_the_stop(tmp_path):
    ex = _RestingExchange()
    eng, w = _hybrid(tmp_path, ex)
    eng._maybe_buy(w, 1000)
    assert eng.resting() == pytest.approx(5 * 0.86)                    # money a resting order may still use
    w.p_final = 0.80                                                   # model + market now below the 86c limit
    eng._work_live_order(w, 1003, eng.cfg)
    assert ex.cancelled == ["R1"] and "edge gone" in rows(tmp_path, "events.csv")[-1]["note"]


def test_hybrid_halts_if_a_cancel_cannot_be_confirmed_and_on_restart_with_an_orphan(tmp_path):
    class Stuck(_RestingExchange):
        def retrieve(self, oid):
            raise ConnectionError("offline")
    ex = Stuck()
    eng, w = _hybrid(tmp_path, ex)
    eng._maybe_buy(w, 1000)
    w.call = None
    eng._work_live_order(w, 1003, eng.cfg)
    assert w.live_order.get("cancelling") and not eng.halted          # followed up, not a halt yet
    eng._work_live_order(w, w.live_order["expires"] + 11, eng.cfg)    # still open after its own expiry
    assert "still shows as open" in eng.halted
    ex2 = _RestingExchange()
    eng2, w2 = _hybrid(tmp_path / "b", ex2)
    eng2._maybe_buy(w2, 1000)                                          # the bot is stopped with the order resting
    eng3 = _live_engine(tmp_path / "b", _RestingExchange(), live_rest=True)
    eng3.restore()
    assert "was left when the bot stopped" in eng3.halted


def test_hybrid_is_off_unless_asked_for(tmp_path):
    assert tb.Config().live_rest is False
    ex = _RestingExchange()
    eng = _live_engine(tmp_path, ex)
    eng._maybe_buy(_live_window("UP"), 1000)
    assert len(ex.created) == 1                                        # plain taker: a miss is left alone


# ---- Coinbase live price ------------------------------------------------------------------------------
def _cb(price, product="BTC-USD", kind="update"):
    return json.dumps({"channel": "ticker", "timestamp": "2026-11-10T20:30:37.1Z", "sequence_num": 1,
                       "events": [{"type": kind, "tickers": [{"type": "ticker", "product_id": product,
                                                              "price": str(price)}]}]})


def test_spot_feed_reads_coinbase_ticker_messages():
    f = tb.SpotFeed()
    f.on_message(json.dumps({"channel": "subscriptions", "events": []}), now=100.0)
    f.on_message(json.dumps({"channel": "heartbeats", "events": [{"current_time": "x"}]}), now=100.5)
    f.on_message(_cb(73640.10, kind="snapshot"), now=101.0)
    f.on_message(_cb(99999, product="ETH-USD"), now=101.5)                 # other products are ignored
    f.on_message(_cb(73645.50), now=102.0)
    f.on_message("not json", now=102.5)
    assert f.updates == 2 and f.price_at(101.4) == 73640.10 and f.price_at(102.0) == 73645.50
    assert f.price_at(99.0) is None                                          # nothing that old
    f.on_message(json.dumps({"type": "error", "message": "bad"}), now=103)
    assert f.error == "bad"


def _spot_engine(tmp_path, **over):
    eng, clock = run_sim(tmp_path, 200, **over)
    S = eng.S
    real = int(time.time()) - 2                                        # line the index up with the wall clock
    eng.prints[real], eng.last_print_ts = S, real
    return eng, clock, S, real


def test_engine_brings_the_index_up_to_date_with_coinbase(tmp_path):
    eng, clock, S, real = _spot_engine(tmp_path)
    f = tb.SpotFeed()
    with f.lock:
        f.ticks.extend([(real + 0.2, S + 60), (real + 0.5, S + 62), (time.time() - 0.1, S + 70)])
    eng.spot = f
    est, px = eng.spot_estimate(clock.time())
    assert px == S + 70 and est == pytest.approx(S + 8)               # the index + Coinbase's move since its print
    with f.lock:
        f.ticks.append((time.time(), (S + 62) * 1.01))                 # a 1% jump: not trusted
    assert eng.spot_estimate(clock.time())[0] is None
    f2 = tb.SpotFeed()
    with f2.lock:
        f2.ticks.extend([(real - 10, S), (time.time() - 5, S + 3)])    # last Coinbase price is 5 s old
    eng.spot = f2
    assert eng.spot_estimate(clock.time())[0] is None
    eng.spot = None
    assert eng.spot_estimate(clock.time()) == (None, None) and eng.spot_status() == "off"


def test_watch_logs_both_models_and_use_trades_on_the_coinbase_one(tmp_path):
    for mode in ("watch", "use"):
        eng, clock, S, real = _spot_engine(tmp_path / mode, spot_use=(mode == "use"))
        f = tb.SpotFeed()
        with f.lock:
            f.ticks.extend([(real + 0.5, S), (time.time() - 0.1, S + 25.0)])
        eng.spot = f
        w = next(iter(eng.active.values()))
        eng._eval(w, clock.time(), w.book)
        assert w.p_model_spot is not None and w.p_model_index is not None and w.spot_est == pytest.approx(S + 25.0)
        if mode == "use":
            assert w.p_model == w.p_model_spot and "Coinbase" in tb.describe_model(eng.cfg)
        else:
            assert w.p_model == w.p_model_index


def test_spot_feed_talks_to_a_websocket_server(tmp_path):
    import asyncio
    import threading as th
    websockets = pytest.importorskip("websockets")
    got, ready = [], th.Event()

    async def handler(ws, *args):
        for _ in range(2):
            got.append(json.loads(await ws.recv()))
        for p in (73650.25, 73651.75):
            await ws.send(_cb(p))
            await asyncio.sleep(0.05)
        await asyncio.sleep(2)

    def serve():
        async def main():
            async with websockets.serve(handler, "127.0.0.1", 8823):
                ready.set()
                await asyncio.sleep(4)
        asyncio.run(main())
    t = th.Thread(target=serve, daemon=True)
    t.start()
    ready.wait(5)
    f = tb.SpotFeed(url="ws://127.0.0.1:8823").start()
    for _ in range(40):
        if f.updates >= 2:
            break
        time.sleep(0.1)
    f.stop()
    assert [g["channel"] for g in got] == ["heartbeats", "ticker"] and got[1]["product_ids"] == ["BTC-USD"]
    assert f.latest()[0] == 73651.75 and "Coinbase: $73,651.75" in f.status()


def test_report_compares_the_two_models(tmp_path):
    import makawan_report as rp
    d, t0 = _log_folder(tmp_path, "makawan_out_any", [(0, "5m", "UP", 22, 0.88, "UP", 0.80)])
    slug = rows(tmp_path / "makawan_out_any", "windows.csv")[0]["slug"]
    with open(os.path.join(d, "ticks.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(tb.Recorder.TICK)
        for i in range(60):
            left = 300 - i * 5
            w.writerow([tb.iso(t0 + i * 5), "5m", slug, left, 1, 1, 1, 0.6, 0.6, 0.6, "", "", "UP", 1, 1, 0.60, 0.70])
    sec = rp.spot_vs_index([d], t0 - 1, t0 + 400)
    assert sec and sec[2][-1][0] == "all" and sec[2][-1][5] == "Coinbase"   # 0.70 was closer to UP than 0.60


def test_real_down_fill_better_than_its_limit_is_read_right_and_does_not_halt(tmp_path):
    """Nov 11, 2026 03:02 UTC: BUY_SHORT at long 0.45 (DOWN up to 55c) filled at long 0.69 = DOWN at 31c."""
    import makawan_live as ml
    ans = {"id": "DN5", "executions": [{"type": "EXECUTION_TYPE_FILL", "lastShares": "8.6600",
           "lastPx": {"value": "0.6900"}, "commissionNotionalCollected": {"value": "0.1800"},
           "commissionSpreadPx": {"value": "0.3100"},
           "order": {"id": "DN5", "state": "ORDER_STATE_FILLED", "cumQuantity": 8.66, "avgPx": {"value": "0.6900"},
                     "intent": "ORDER_INTENT_BUY_SHORT", "outcomeSide": "OUTCOME_SIDE_NO",
                     "commissionNotionalTotalCollected": {"value": "0.1800"}}}]}
    r = ml.read_result(ans, "DOWN", 0.55)
    assert r["price"] == pytest.approx(0.31) and r["filled"] == pytest.approx(8.66) and r["fee"] == pytest.approx(0.18)
    ex = _FakeExchange("fill", fill_long_px=0.69)
    eng = _live_engine(tmp_path, ex)
    w = _live_window("DOWN", bid=0.45, ask=0.47, p=0.30)
    eng._maybe_buy(w, 1000)
    assert w.position.avg_px == pytest.approx(0.31) and not eng.halted


def test_secure_websockets_use_certifi_certificates():
    import ssl
    ctx = tb.ssl_context()
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert ctx.cert_store_stats()["x509_ca"] > 50                         # a real list of trusted authorities
    assert os.environ.get("SSL_CERT_FILE")


def test_empty_and_failed_books_are_logged_and_told_apart(tmp_path):
    eng, clock = run_sim(tmp_path, 200)
    w = next(iter(eng.active.values()))
    eng._note_empty_book(w, clock.time(), tb.Book([], []), "poll")
    eng._note_empty_book(w, clock.time() + 5, None, "poll")              # still empty: one line a minute at most
    w.book_error = "order book request failed (429 Too Many Requests)"
    eng._note_empty_book(w, clock.time() + 70, None, "poll")
    lines = [json.loads(x) for x in open(tmp_path / "book_debug.jsonl")]
    assert len(lines) == 2 and lines[0]["book_was_empty"] and lines[0]["source"] == "poll"
    assert "429" in lines[1]["error"]
    eng._eval(w, clock.time() + 70, None)
    assert "429" in w.value_note or w.note
    eng._note_empty_book(w, clock.time() + 75, tb.Book([(0.5, 10)], [(0.52, 10)]), "poll")
    assert w.empty_since is None


def test_an_empty_streamed_book_falls_back_to_polling(tmp_path):
    eng, clock = run_sim(tmp_path, 200)

    class EmptyStream:
        disabled = False

        def want(self, slugs):
            pass

        def get(self, slug):
            return tb.Book([], [])

        def status(self):
            return "live"
    eng.stream = EmptyStream()
    clock.advance(1)
    eng.tick()
    assert any(w.book is not None for w in eng.active.values())       # the polled book was used instead


def test_a_cached_not_found_order_book_is_asked_again_past_the_cache():
    class Cached:
        """Answers 'not found' for the plain address (a stale cache), and the real book with a one-off extra."""
        def __init__(self):
            self.calls = []

        def get(self, path, params=None, headers=None):
            self.calls.append((path, params, headers))
            if path.endswith("/book") and not params:
                e = tb.NotFound(path)
                e.headers = {"Age": "212", "X-Cache": "Error from cloudfront"}
                raise e
            return {"marketData": {"bids": [{"px": {"value": "0.55"}, "qty": "10"}],
                                   "offers": [{"px": {"value": "0.57"}, "qty": "10"}]}}
    tr = Cached()
    api = tb.Api(tr, tb.Config())
    b = api.book("cpc-btc-updown-5m-2026-11-11-0325z")
    assert b.bid == 0.55 and b.ask == 0.57
    assert tr.calls[1][1]["_"] and tr.calls[1][2] == {"Cache-Control": "no-cache"}
    assert api.last_404["headers"]["Age"] == "212"
    api.book("cpc-btc-updown-5m-2026-11-11-0325z")
    assert len(tr.calls) == 3 and tr.calls[2][1]                      # goes straight past the cache from now on

    class Gone(Cached):
        def get(self, path, params=None, headers=None):
            raise tb.NotFound(path)
    with pytest.raises(tb.NotFound):
        tb.Api(Gone(), tb.Config()).book("x")                          # a market with no book still says so


def test_resume_after_an_orphan_resting_order_does_not_halt_again(tmp_path):
    ex = _RestingExchange()
    eng, w = _hybrid(tmp_path, ex)
    eng._maybe_buy(w, 1000)                                            # stopped with an order resting
    eng2 = _live_engine(tmp_path, _RestingExchange(), live_rest=True)
    eng2.restore()
    assert "was left when the bot stopped" in eng2.halted
    tb.clear_halt(str(tmp_path), eng2.rec, resume=True)                # --resume
    eng3 = _live_engine(tmp_path, _RestingExchange(), live_rest=True)
    eng3.restore()
    assert not eng3.halted                                             # it used to halt again, every time


def test_an_orphan_resting_order_is_looked_up_on_restart(tmp_path):
    ex = _RestingExchange()
    eng, w = _hybrid(tmp_path, ex)
    eng._maybe_buy(w, 1000)
    ex2 = _RestingExchange()
    ex2.state = dict(ex.state)                                         # the same order, seen from the new run
    eng2 = _live_engine(tmp_path, ex2, live_rest=True)
    assert eng2.resolve_orphans() == [("R1", "ended unfilled")] and ex2.cancelled == ["R1"]
    eng2.restore()
    assert not eng2.halted
    ex3 = _RestingExchange(cancel_fills=5)
    eng3, w3 = _hybrid(tmp_path / "b", ex3)
    eng3._maybe_buy(w3, 1000)
    ex4 = _RestingExchange(cancel_fills=5)
    ex4.state = dict(ex3.state)
    eng4 = _live_engine(tmp_path / "b", ex4, live_rest=True)
    got = eng4.resolve_orphans()
    assert "added to the record" in got[0][1]
    eng4.restore()
    assert not eng4.halted and eng4.cash == pytest.approx(100 - 5 * 0.86)     # the fill is on the books


def test_orphan_lookup_still_happens_when_restarting_with_resume(tmp_path):
    ex = _RestingExchange()
    eng, w = _hybrid(tmp_path, ex)
    eng._maybe_buy(w, 1000)
    tb.clear_halt(str(tmp_path), eng.rec, resume=True)                 # (no halt yet, but a RESUME row is written)
    tb.write_state(str(tmp_path), halted="x")
    tb.clear_halt(str(tmp_path), eng.rec, resume=True)
    ex2 = _RestingExchange()
    ex2.state = dict(ex.state)
    eng2 = _live_engine(tmp_path, ex2, live_rest=True)
    assert eng2.resolve_orphans() == [("R1", "ended unfilled")]
    assert eng2.resolve_orphans() == []                                # written down: not asked twice


def test_coinbase_move_is_not_zeroed_by_matching_a_moment_that_has_not_happened(tmp_path):
    eng, clock, S, real = _spot_engine(tmp_path)
    f = tb.SpotFeed()
    now = time.time()
    eng.last_print_ts = int(now - 0.7)                                 # how prints really arrive: stamped ~0.7 s ago
    eng.prints[eng.last_print_ts] = S
    with f.lock:
        f.ticks.extend([(eng.last_print_ts + 0.4, S + 100), (now - 0.05, S + 109)])
    eng.spot = f
    est, _ = eng.spot_estimate(clock.time())
    assert est == pytest.approx(S + 9) or eng.last_print_ts + 0.5 > time.time()



# ---- cutting losses -----------------------------------------------------------------------------------
def _holding(tmp_path, side="UP", px=0.60, shares=8.0, ex=None, **over):
    kw = dict(cut_on_flip=True, cut_on_value=True, cut_only_losing=False)
    kw.update(over)
    eng = _live_engine(tmp_path, ex, **kw) if ex is not None else _bare_engine(tmp_path, bankroll=100.0, **kw)
    eng.last_print_ts = 1000
    w = _live_window(side)
    cost = shares * px + 0.11
    eng.cash -= cost
    w.position = tb.Position(side, shares, px, 0.11, cost, 990)
    w.trade = {"side": side, "shares": shares, "avg": px, "fee": 0.11, "exit": "", "liquidity": "taker"}
    w.entered = True
    eng.active[w.info.horizon] = w
    return eng, w


def test_paper_cut_when_the_call_turns(tmp_path):
    eng, w = _holding(tmp_path, "UP")
    w.call, w.p_model = "DOWN", 0.40
    w.book = tb.Book([(0.38, 3), (0.37, 50)], [(0.40, 50)])
    eng._eval_holding = None
    why = eng._cut_reason(w, 1000, eng.cfg)
    assert "turned DOWN" in why
    eng._cut(w, 1000, eng.cfg, why)
    assert w.position is None and w.trade["exit"] == "cut"                  # sold 3 @ 38c and 5 @ 37c
    sale = 3 * 0.38 + 5 * 0.37
    fee = round(3 * tb.fee_per_share(0.38, 0.095) + 5 * tb.fee_per_share(0.37, 0.095), 2)
    assert eng.cash == pytest.approx(100 - (8 * 0.60 + 0.11) + sale - fee)
    assert w.cut_pnl == pytest.approx(sale - fee - (8 * 0.60 + 0.11))
    eng._finalize(w, "UP", 1300)                                       # it would have won; the sale is the result
    win = rows(tmp_path, "windows.csv")[-1]
    assert float(win["net_pnl"]) == pytest.approx(w.cut_pnl, abs=0.01) and win["exit"] == "cut"
    assert eng.cash == pytest.approx(100 - (8 * 0.60 + 0.11) + sale - fee)  # no payout on top


def test_cut_when_the_bid_is_worth_more_than_the_maths_for_down_and_up(tmp_path):
    eng, w = _holding(tmp_path, "DOWN", px=0.55)
    w.call = "DOWN"                                                   # the call has not turned...
    w.p_model = 0.50                                                  # ...but the maths says DOWN 50%
    w.book = tb.Book([(0.38, 50)], [(0.40, 50)])                      # and buyers pay 60c for DOWN
    why = eng._cut_reason(w, 1000, eng.cfg)
    assert "worth more than the 50% chance" in why
    w.p_model = 0.45                                                  # maths DOWN 55%: 60c - fee 2.3c = 57.7c > 56c
    assert eng._cut_reason(w, 1000, eng.cfg)
    w.p_model = 0.40                                                  # maths DOWN 60%: keep it
    assert eng._cut_reason(w, 1000, eng.cfg) == ""
    eng2, w2 = _holding(tmp_path / "b", "UP", px=0.60)
    w2.call, w2.p_model = "UP", 0.55                                  # maths UP 55%, bid 70c
    w2.book = tb.Book([(0.70, 50)], [(0.72, 50)])
    assert eng2._cut_reason(w2, 1000, eng2.cfg)


def test_no_cut_when_off_too_late_or_without_buyers(tmp_path):
    eng, w = _holding(tmp_path, "UP", cut_on_flip=False, cut_on_value=False)
    w.call, w.p_model = "DOWN", 0.2
    w.book = tb.Book([(0.30, 50)], [(0.32, 50)])
    assert eng._cut_reason(w, 1000, eng.cfg) == ""
    eng2, w2 = _holding(tmp_path / "b", "UP")
    w2.call, w2.book, w2.tau = "DOWN", tb.Book([(0.30, 50)], [(0.32, 50)]), 8
    assert eng2._cut_reason(w2, 1000, eng2.cfg) == ""                 # last 10 seconds
    w2.tau, w2.book = 100, tb.Book([], [(0.32, 50)])
    assert eng2._cut_reason(w2, 1000, eng2.cfg) == ""                 # nobody buying UP


def test_live_cut_sends_a_capped_sell_and_books_the_real_fill(tmp_path):
    ex = _FakeExchange("fill", fill_long_px=0.62)                     # DOWN sold at 38c = long price 62c
    eng, w = _holding(tmp_path, "DOWN", px=0.55, ex=ex)
    w.call, w.p_model = "UP", 0.70
    w.book = tb.Book([(0.60, 50)], [(0.62, 50)])                      # DOWN bid = 1 - 0.62 = 38c
    eng._cut(w, 1000, eng.cfg, eng._cut_reason(w, 1000, eng.cfg))
    req = ex.created[0]
    assert req["intent"] == "ORDER_INTENT_SELL_SHORT" and req["tif"] == "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
    assert req["price"]["value"] == "0.63" and req["quantity"] == 8   # DOWN no lower than 37c = long at most 63c
    assert w.position is None and [r["event"] for r in rows(tmp_path, "events.csv")][-2:] == ["CUT", "SELL"]
    sell = rows(tmp_path, "events.csv")[-1]
    assert float(sell["price"]) == pytest.approx(0.38) and not eng.halted
    ex2 = _FakeExchange("fill", fill_long_px=0.40)
    eng2, w2 = _holding(tmp_path / "b", "UP", px=0.60, ex=ex2)
    w2.call, w2.book = "DOWN", tb.Book([(0.40, 50)], [(0.42, 50)])
    eng2._cut(w2, 1000, eng2.cfg, "test")
    assert ex2.created[0]["intent"] == "ORDER_INTENT_SELL_LONG" and ex2.created[0]["price"]["value"] == "0.39"


def test_live_cut_not_filled_keeps_the_position_and_tries_a_few_times(tmp_path):
    ex = _FakeExchange("miss")
    eng, w = _holding(tmp_path, "UP", ex=ex)
    w.call, w.book = "DOWN", tb.Book([(0.40, 50)], [(0.42, 50)])
    for t in range(5):
        why = eng._cut_reason(w, 1000 + t, eng.cfg)
        if why:
            eng._cut(w, 1000 + t, eng.cfg, why)
    assert len(ex.created) == 3 and w.position.shares == 8            # at most 3 sell orders per window
    ex2 = _FakeExchange("boom")
    eng2, w2 = _holding(tmp_path / "b", "UP", ex=ex2)
    w2.call, w2.book = "DOWN", tb.Book([(0.40, 50)], [(0.42, 50)])
    eng2._cut(w2, 1000, eng2.cfg, "test")
    assert "SELL" in eng2.halted and "unknown" in eng2.halted


def test_a_part_sold_position_survives_a_restart(tmp_path):
    eng, w = _holding(tmp_path, "UP")
    with open(tmp_path / "events.csv", "a", newline="") as f:          # the original buy, as the bot writes it
        csv.writer(f).writerow([tb.iso(990), "", w.info.horizon, w.info.slug, "BUY", "UP", "", "", "", 0.60, 8, 0.11, "", "taker"])
    w.call, w.book = "DOWN", tb.Book([(0.40, 3)], [(0.42, 50)])
    eng._cut(w, 1000, eng.cfg, "test")
    assert w.position.shares == 5 and w.trade["exit"] == "part cut"
    cash_after = eng.cash
    eng2 = _bare_engine(tmp_path, bankroll=100.0)
    eng2.restore()
    assert eng2.cash == pytest.approx(cash_after, abs=0.01)            # open cost of 5 shares, sale already in cash


def test_cut_flag_sells_on_value_only_not_on_a_turned_call():
    import argparse
    a = argparse.Namespace(cut=True)
    cfg = tb.Config()
    if getattr(a, "cut", False):
        cfg.cut_on_value = True
    ap_src = open(os.path.join(os.path.dirname(os.path.abspath(tb.__file__)), "makawan_bot.py")).read()
    assert "cfg.cut_on_value = True" in ap_src and "cfg.cut_on_flip = cfg.cut_on_value = True" not in ap_src


def test_users_example_down_60_market_45_holds_market_75_sells(tmp_path):
    eng, w = _holding(tmp_path, "DOWN", px=0.50, cut_on_flip=False)
    w.call, w.p_model = "DOWN", 0.40                                  # maths: DOWN 60%
    w.book = tb.Book([(0.54, 50)], [(0.56, 50)])                      # market DOWN ~45%: buyers pay 44c
    assert eng._cut_reason(w, 1000, eng.cfg) == ""
    w.book = tb.Book([(0.24, 50)], [(0.26, 50)])                      # market DOWN 75%: buyers pay 74c
    assert "worth more than the 60% chance" in eng._cut_reason(w, 1000, eng.cfg)
    w.call = "UP"                                                     # a turned call alone does nothing
    w.book = tb.Book([(0.54, 50)], [(0.56, 50)])
    assert eng._cut_reason(w, 1000, eng.cfg) == ""


def test_cut_sells_only_losing_positions_by_default(tmp_path):
    assert tb.Config().cut_only_losing is True
    eng, w = _holding(tmp_path, "DOWN", px=0.50, cut_on_flip=False, cut_only_losing=True)
    w.call, w.p_model = "DOWN", 0.40                                  # maths: DOWN 60%
    w.book = tb.Book([(0.24, 50)], [(0.26, 50)])                      # buyers pay 74c for DOWN, bought at ~51c: winning
    assert eng._cut_reason(w, 1000, eng.cfg) == ""                    # held to settlement
    w.p_model = 0.70                                                  # maths: DOWN 30%
    w.book = tb.Book([(0.58, 50)], [(0.60, 50)])                      # buyers pay 40c: losing, and above the maths
    why = eng._cut_reason(w, 1000, eng.cfg)
    assert "worth more than the 30% chance" in why and "losing" in why
    w.book = tb.Book([(0.74, 50)], [(0.76, 50)])                      # buyers pay 24c: losing but below the maths
    assert eng._cut_reason(w, 1000, eng.cfg) == ""


def test_a_resting_order_is_cancelled_after_five_seconds(tmp_path):
    assert tb.Config().live_rest_max_s == 5.0
    ex = _RestingExchange()
    eng, w = _hybrid(tmp_path, ex, live_rest_max_s=5.0)
    eng._maybe_buy(w, 1000)
    for t in (1001, 1002, 1003, 1004):
        eng._work_live_order(w, t, eng.cfg)
    assert w.live_order is not None and not ex.cancelled
    eng._work_live_order(w, 1005, eng.cfg)
    assert ex.cancelled == ["R1"] and w.live_order is None and w.position is None
    assert "not filled within 5s" in rows(tmp_path, "events.csv")[-1]["note"]
    eng._maybe_buy(w, 1010)
    assert len(ex.created) == 2                                        # the window is done: no new order


def test_a_slow_cancel_is_followed_up_instead_of_halting(tmp_path):
    class SlowCancel(_RestingExchange):
        """Polymarket finishes a cancel a moment later: the order shows open for two more lookups."""
        def cancel(self, oid, params):
            self.cancelled.append(oid)
            self.pending = 2

        def retrieve(self, oid):
            st = self.state
            if getattr(self, "pending", None) is not None:
                if self.pending <= 0:
                    st["st"] = "ORDER_STATE_CANCELED"
                self.pending -= 1
            return super().retrieve(oid)
    ex = SlowCancel()
    eng, w = _hybrid(tmp_path, ex, live_rest_max_s=5.0)
    eng._maybe_buy(w, 1000)
    eng._work_live_order(w, 1005, eng.cfg)                             # 5 s: cancel sent, still showing open
    assert w.live_order and w.live_order.get("cancelling") and not eng.halted
    for t in (1006, 1007, 1008):
        eng._work_live_order(w, t, eng.cfg)
    assert w.live_order is None and not eng.halted
    assert rows(tmp_path, "events.csv")[-1]["event"] == "CANCEL"
    eng._maybe_buy(w, 1010)
    assert len([r for r in ex.created if r["tif"].endswith("GOOD_TILL_DATE")]) == 1


def test_an_unconfirmed_cancel_is_looked_up_on_the_next_restart(tmp_path):
    class NeverCancels(_RestingExchange):
        def cancel(self, oid, params):
            self.cancelled.append(oid)
    ex = NeverCancels()
    eng, w = _hybrid(tmp_path, ex)
    eng._maybe_buy(w, 1000)
    tb.time.sleep = lambda s: None
    try:
        eng.halt(1002, "test")                                         # waits, then gives up: an ERROR row
    finally:
        import importlib
        tb.time.sleep = __import__("time").sleep
    assert any(r["event"] == "ERROR" and "not confirmed" in r["note"] for r in rows(tmp_path, "events.csv"))
    ex2 = _RestingExchange()
    ex2.state = dict(ex.state)
    eng2 = _live_engine(tmp_path, ex2, live_rest=True)
    assert eng2.resolve_orphans() == [("R1", "ended unfilled")]       # asked again instead of forgotten


def test_live_page_shows_the_real_polymarket_balance(tmp_path):
    import makawan_web as tw
    eng, clock = run_sim(tmp_path, 300)

    class B:
        rehearse = False
        calls = 0

        def balance(self):
            B.calls += 1
            return {"balance": 71.25, "buying_power": 70.0, "open_orders": 1.25}
    eng.broker = B()
    eng._refresh_real_balance(clock.time())
    eng._refresh_real_balance(clock.time() + 5)
    assert B.calls == 1                                                # every 15 s, not every tick
    a = eng.real_status()
    open_cost = eng.equity() - eng.cash
    assert a["balance"] == 71.25 and a["equity"] == pytest.approx(71.25 + open_cost, abs=0.01)
    assert a["diff"] == pytest.approx(a["equity"] - eng.equity(), abs=0.01)
    snap = tw.snapshot(eng, clock, "live")
    assert snap["account"]["balance"] == 71.25
    json.dumps(snap, allow_nan=False)
    eng2, clock2 = run_sim(tmp_path / "paper", 100)
    assert eng2.real_status() is None and tw.snapshot(eng2, clock2, "simulation")["account"] is None


def test_a_one_dollar_stake_still_sends_orders(tmp_path):
    ex = _FakeExchange("fill")
    eng = _live_engine(tmp_path, ex, flat_stake=1.0, min_trade_usd=0.5)
    w = _live_window("UP")
    eng._maybe_buy(w, 1000)
    assert len(ex.created) == 1 and w.position is not None and w.position.cost <= 1.0
    src = open(os.path.join(os.path.dirname(os.path.abspath(tb.__file__)), "makawan_live.py")).read()
    assert '"min_trade_usd": min(1.0, a.stake * 0.5)' in src


# ---- strategy lab -------------------------------------------------------------------------------------
def test_trend_and_fade_shift_the_maths_in_opposite_directions(tmp_path):
    eng, clock = run_sim(tmp_path, 700)
    w = next(iter(eng.active.values()))
    S, t = eng.S, eng.last_print_ts
    eng.prints[t - 600] = S - 50.0                                     # Bitcoin rose $50 over the last 10 minutes
    eng.prints[t - 60] = S - 20.0                                      # ...$20 of it in the last minute
    w.tau = 300.0
    flat = eng._drift(eng.cfg, w, S)
    trend = eng._drift(tb.Config(drift_lookback_s=600, drift_beta=0.3), w, S)
    fade = eng._drift(tb.Config(drift_lookback_s=60, drift_beta=-0.3), w, S)
    assert flat == 0.0
    assert trend == pytest.approx(0.3 * 50 * 300 / 600)                # 30% of the move, for half a look-back
    assert fade == pytest.approx(-0.3 * 20)                            # 30% of the last minute's move undone
    w.tau = 0.0
    assert eng._drift(tb.Config(drift_lookback_s=600, drift_beta=0.3), w, S) == 0.0      # no time left, no drift
    eng.cfg.drift_lookback_s, eng.cfg.drift_beta = 600, 0.3
    w.tau = 300.0
    eng._eval(w, clock.time(), w.book)
    assert w.drift_adj > 0 and "trend" in tb.describe_model(eng.cfg)


def test_price_cap_blocks_expensive_sides(tmp_path):
    eng = _bare_engine(tmp_path, max_buy_cap=0.85)
    w = _window(call="UP", p_model=0.95, p_mkt=0.88, p_final=0.92)
    w.book = tb.Book([(0.87, 500)], [(0.89, 500)])
    eng._maybe_buy(w, 1000)
    assert w.position is None and "price cap" in w.value_note


def _paper(tmp_path, **over):
    eng = _bare_engine(tmp_path, paper_delay_s=1.0, flat_stake=20.0, **over)
    w = _window(call="UP", p_model=0.93, p_mkt=0.85, p_final=0.89)
    w.book = tb.Book([(0.84, 500)], [(0.86, 500)])
    eng.active["15m"] = w
    return eng, w


def test_honest_paper_fill_needs_the_price_to_still_be_there(tmp_path):
    eng, w = _paper(tmp_path)
    eng._maybe_buy(w, 1000)
    assert w.position is None and w.paper_order and w.entered         # sent, not filled yet
    assert w.paper_order["limit"] == pytest.approx(0.86)               # capped by the edge, as a live order is
    eng._paper_arrive(w, 1000.5, eng.cfg)
    assert w.paper_order is not None                                   # not arrived yet
    w.book = tb.Book([(0.83, 500)], [(0.85, 500)])                     # a second later the ask is 85c
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is not None and w.position.avg_px == pytest.approx(0.85) and w.position.cost <= 20.0
    assert eng.paper_tries == 1 and eng.paper_fills == 1
    assert [r["event"] for r in rows(tmp_path, "events.csv")] == ["TRY", "BUY"]


def test_honest_paper_order_misses_when_the_price_moved_and_skips_the_window(tmp_path):
    eng, w = _paper(tmp_path)
    eng._maybe_buy(w, 1000)
    w.book = tb.Book([(0.88, 500)], [(0.90, 500)])                     # the ask ran away
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is None and w.paper_order is None and w.entered and eng.cash == 1000.0
    assert [r["event"] for r in rows(tmp_path, "events.csv")] == ["TRY", "NOFILL"]
    w.book = tb.Book([(0.84, 500)], [(0.86, 500)])
    w.entered = False
    eng._maybe_buy(w, 1005)
    assert eng.paper_tries == 1                                        # one order per window, as live
    eng2 = _bare_engine(tmp_path, paper_delay_s=1.0)
    eng2.restore()
    assert eng2.paper_tries == 1 and eng2.paper_fills == 0             # counts survive a restart


def test_honest_paper_fill_takes_only_what_is_on_offer_under_the_limit(tmp_path):
    eng, w = _paper(tmp_path)
    eng._maybe_buy(w, 1000)
    qty = w.paper_order["qty"]
    w.book = tb.Book([(0.84, 500)], [(0.86, 5), (0.87, 500)])          # only 5 shares (<50% of qty): rejected
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is None and rows(tmp_path, "events.csv")[-1]["event"] == "NOFILL"
    assert "partial fill" in rows(tmp_path, "events.csv")[-1]["note"]
    eng2, w2 = _paper(tmp_path / "half")
    eng2._maybe_buy(w2, 1000)
    qty2 = w2.paper_order["qty"]
    w2.book = tb.Book([(0.84, 500)], [(0.86, 15), (0.87, 500)])        # 15 shares (>=50% of qty): fills partially
    eng2._paper_arrive(w2, 1001, eng2.cfg)
    assert w2.position is not None and w2.position.shares == 15 and qty2 > 15


def test_paper_arrive_revalidates_entry_filters(tmp_path):
    # 1) price leaks under min_buy_px during flight -> rejected
    eng, w = _paper(tmp_path / "floor", min_buy_px={"15m": 0.85})
    eng._maybe_buy(w, 1000)
    w.book = tb.Book([(0.80, 500)], [(0.82, 500)])
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is None and "under min 0.85" in rows(tmp_path / "floor", "events.csv")[-1]["note"]
    # 2) edge disappears by arrival -> rejected
    eng, w = _paper(tmp_path / "edge")
    eng._maybe_buy(w, 1000)
    w.p_final = 0.865                                                  # edge < 1c at 0.86 after fee
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is None and "edge" in rows(tmp_path / "edge", "events.csv")[-1]["note"]
    # 3) call dropped or flipped -> rejected
    eng, w = _paper(tmp_path / "call")
    eng._maybe_buy(w, 1000)
    w.call = None
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is None and "call changed" in rows(tmp_path / "call", "events.csv")[-1]["note"]
    # 4) model gap widens past max_model_gap -> rejected
    eng, w = _paper(tmp_path / "gap")
    eng._maybe_buy(w, 1000)
    w.p_model, w.p_mkt = 0.99, 0.70                                    # 29-pt gap > 20-pt cap
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is None and "model gap" in rows(tmp_path / "gap", "events.csv")[-1]["note"]
    # 5) too little time left -> rejected
    eng, w = _paper(tmp_path / "tau")
    eng._maybe_buy(w, 1000)
    w.tau = 3.0
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is None and "left" in rows(tmp_path / "tau", "events.csv")[-1]["note"]


def test_shared_feed_asks_polymarket_once_for_the_same_question():
    import makawan_lab as ml

    class Inner:
        def __init__(self):
            self.calls = []
            self.backoff_until = 0.0

        def get(self, path, params=None, headers=None):
            self.calls.append((path, params))
            if "missing" in path:
                raise tb.NotFound(path)
            if "broken" in path:
                raise tb.ApiError("500")
            return {"n": len(self.calls)}
    inner = Inner()
    feed = ml.CachedTransport(inner, ttl=5)
    a = feed.get("/v1/markets/x/book")
    b = feed.get("/v1/markets/x/book")
    assert a is b and len(inner.calls) == 1
    c = feed.get("/v1/markets/x/book", {"_": "123"}, {"Cache-Control": "no-cache"})   # 'past the cache' always asks
    assert len(inner.calls) == 2 and c is not a
    assert feed.get("/v1/markets/x/book") is c                         # and its fresh answer is shared
    feed.get("/v1/asset-prices/history", {"from": 10})
    feed.get("/v1/asset-prices/history", {"from": 11})
    assert len(inner.calls) == 4                                       # different questions are asked
    for _ in range(3):
        with pytest.raises(tb.NotFound):
            feed.get("/v1/markets/missing/book")
    assert len(inner.calls) == 5                                       # a 'not found' is remembered too
    for _ in range(2):
        with pytest.raises(tb.ApiError):
            feed.get("/v1/broken")
    assert len(inner.calls) == 7                                       # a failure is not: it is tried again
    assert feed.backoff_until == 0.0
    quick = ml.CachedTransport(inner, ttl=0.0)
    quick.get("/v1/markets/x/book")
    quick.get("/v1/markets/x/book")
    assert len(inner.calls) == 9                                       # nothing is kept past its time


def test_lab_feed_shares_answers_and_failures_within_a_round():
    import makawan_lab as ml

    class Inner:
        def __init__(self):
            self.calls = []
            self.backoff_until = 0.0
            self.fail = False

        def get(self, path, params=None, headers=None):
            self.calls.append((path, params))
            if self.fail:
                raise TimeoutError("read timed out")
            if "missing" in path:
                raise tb.NotFound(path)
            return {"n": len(self.calls)}
    inner = Inner()
    feed = ml.CachedTransport(inner, ttl=0.0)                         # timer alone would share nothing
    feed.new_round()
    a = feed.get("/v1/markets/x/book")
    assert feed.get("/v1/markets/x/book") is a and len(inner.calls) == 1             # ten strategies, one request
    for i in range(3):                                                 # 'past the cache' asked once a round, not 3x
        with pytest.raises(tb.NotFound):
            feed.get("/v1/markets/missing/book", {"_": str(i)}, {"Cache-Control": "no-cache"})
    assert len(inner.calls) == 2
    inner.fail = True
    for _ in range(10):                                                # a timeout is waited out once, not ten times
        with pytest.raises(tb.ApiError):
            feed.get("/v1/asset-prices/history", {"from": 5})
    assert len(inner.calls) == 3
    feed.end_round()
    feed.new_round()                                                   # next round: asked again
    inner.fail = False
    assert feed.get("/v1/asset-prices/history", {"from": 5}) == {"n": 4}
    b = feed.get("/v1/markets/x/book")
    assert b is not a and len(inner.calls) == 5                        # a new round gets new prices
    feed.end_round()


def test_lab_runs_every_strategy_on_one_feed_and_scores_them(tmp_path, monkeypatch):
    import argparse
    import makawan_lab as ml
    monkeypatch.chdir(tmp_path)
    a = argparse.Namespace(only=",".join(ml.NAMES), simulate=True, seed=5, fresh=False, bankroll=1000.0, stake=20.0,
                           delay=1.0, spot="off", speed=0)
    bots, clock = ml.build(a, say=lambda *x: None)
    assert [b.name for b in bots] == ml.NAMES and len({id(b.eng.api) for b in bots}) == 1     # one shared feed
    ml.Lab.bots, ml.Lab.by_name = bots, {b.name: b for b in bots}
    ml.run_loop(bots, clock, a, max_ticks=1500)
    board = json.loads(ml.Lab.board)
    assert {r["name"] for r in board["rows"]} == set(ml.NAMES)
    by = {r["name"]: r for r in board["rows"]}
    assert sum(r["trades"] for r in board["rows"]) > 0
    assert by["baseline"]["tries"] >= by["baseline"]["fills"] >= 0
    for name in ml.NAMES:
        assert os.path.isfile(os.path.join(f"makawan_out_lab_{name}", "windows.csv"))
        assert tb.read_state(f"makawan_out_lab_{name}")["lab"] is True
    cfgs = {b.name: b.cfg for b in bots}
    assert cfgs["trend"].drift_beta > 0 > cfgs["fade"].drift_beta and cfgs["maker"].exec_mode == "maker"
    assert cfgs["sniper"].min_buy_px["5m"] == 0.90 and cfgs["cautious"].w_market == 0.65
    assert all(c.paper_delay_s == 1.0 for n, c in cfgs.items() if n != "maker")
    assert all(c.flat_stake == 20.0 for c in cfgs.values())
    assert all(c.no_trade_horizons == ("5m", "1h") if n == "base15m" else c.no_trade_horizons == ("1h",)
               for n, c in cfgs.items())                               # 1h disabled everywhere; base15m trades 15m only
    assert cfgs["base15m"].trade_last_s == {} and cfgs["base15m"].live_slip == 0.0
    v15 = cfgs["base15m"].view("15m")
    assert (v15.vol_short_weight, v15.vol_scale, v15.tail_w) == (0.0, 1.1, 0.05)
    assert cfgs["terry"].no_trade_horizons == ("1h",) and isinstance(by_name_eng(bots, "terry"), ml.TerryEngine)
    assert isinstance(by_name_eng(bots, "rising"), ml.RisingEngine) and cfgs["rising"].rising_min == 0.03
    assert cfgs["favs"].min_buy_px == {"5m": 0.80, "15m": 0.80} and type(by_name_eng(bots, "favs")) is tb.Engine
    assert "up 3c+" in by["rising"]["settings"] and len(ml.NAMES) == 11
    for r in rows(tmp_path / "makawan_out_lab_favs", "events.csv"):
        if r["event"] in ("TRY", "BUY"):
            assert float(r["price"]) >= 0.80 - 1e-9                    # 80c+ enforced at both TRY and arrival
    for r in rows(tmp_path / "makawan_out_lab_sniper", "events.csv"):
        if r["event"] == "BUY":
            assert 0.90 - 1e-9 <= float(r["price"]) <= 0.97 + 1e-9     # no <90c arrival leaks
    with pytest.raises(SystemExit):
        ml.build(argparse.Namespace(**dict(vars(a), only="nope")), say=lambda *x: None)
    src = open(os.path.join(os.path.dirname(os.path.abspath(tb.__file__)), "makawan_lab.py")).read()
    for word in ("orders.create", "POLYMARKET_SECRET_KEY", "PolymarketUS("):
        assert word not in src                                         # paper only: no key, no orders


def _rising(tmp_path, **over):
    import makawan_lab as ml

    class NoApi:
        def get(self, *a, **k):
            raise tb.NotFound("x")
    cfg = tb.Config(out_dir=str(tmp_path), paper_delay_s=1.0, flat_stake=20.0, **over)
    cfg.rising_look_s, cfg.rising_min = 30.0, 0.03
    eng = ml.RisingEngine(cfg, tb.Api(NoApi(), cfg), tb.SimClock(1000), tb.Recorder(cfg.out_dir), parallel=False)
    w = _window(call="UP", p_model=0.93, p_mkt=0.85, p_final=0.89)
    w.book = tb.Book([(0.84, 500)], [(0.86, 500)])
    eng.active["15m"] = w
    return eng, w


def test_rising_only_buys_while_the_side_is_moving_its_way(tmp_path):
    eng, w = _rising(tmp_path)
    eng._maybe_buy(w, 1000)
    assert w.paper_order is None and "not enough price history" in w.value_note        # nothing seen yet: wait
    eng.trail[w.info.slug] = [(968.0, 0.84), (969.0, 0.84), (999.0, 0.85), (1000.0, 0.85)]
    assert eng.side_move(w, 1000, "UP") == pytest.approx(0.01) and eng.side_move(w, 1000, "DOWN") == pytest.approx(-0.01)
    eng._maybe_buy(w, 1000)
    assert w.paper_order is None and "+1c" in w.value_note and not w.entered            # up 1c is not enough
    eng.trail[w.info.slug] = [(968.0, 0.80), (969.0, 0.81), (999.0, 0.85), (1000.0, 0.85)]
    assert eng.side_move(w, 1000, "UP") == pytest.approx(0.04)                         # against ~30 s ago, not 32 s
    eng._maybe_buy(w, 1000)
    assert w.paper_order and w.paper_order["side"] == "UP" and w.entered                # up 4c: baseline's buy goes out
    eng2, w2 = _rising(tmp_path / "falling")
    eng2.trail[w2.info.slug] = [(969.0, 0.90), (1000.0, 0.85)]
    eng2._maybe_buy(w2, 1000)
    assert w2.paper_order is None and "-5c" in w2.value_note                           # falling: baseline would buy, this waits
    eng3, w3 = _rising(tmp_path / "down")
    w3.call, w3.p_model, w3.p_mkt, w3.p_final = "DOWN", 0.07, 0.15, 0.11
    w3.book = tb.Book([(0.14, 500)], [(0.16, 500)])
    eng3.trail[w3.info.slug] = [(969.0, 0.20), (1000.0, 0.15)]                          # UP fell 5c = DOWN rose 5c
    eng3._maybe_buy(w3, 1000)
    assert w3.paper_order and w3.paper_order["side"] == "DOWN"
    eng4, w4 = _rising(tmp_path / "gap")
    eng4.trail[w4.info.slug] = [(969.0, 0.80), (990.0, 0.85)]                           # last reading 10 s old: cannot tell
    assert eng4.side_move(w4, 1000, "UP") is None


def test_rising_keeps_its_own_price_trail_from_the_order_book(tmp_path):
    eng, w = _rising(tmp_path)
    for i in range(80):
        eng._eval(w, 1000 + i, tb.Book([(0.50 + i / 1000, 500)], [(0.52 + i / 1000, 500)]))
    t = eng.trail[w.info.slug]
    assert t[-1] == (1079, pytest.approx(0.589)) and 1079 - t[0][0] <= 51 and len(t) <= 52    # only what it needs
    eng._eval(w, 1080, None)                                                             # no book: nothing added
    assert eng.trail[w.info.slug][-1][0] == 1079


def test_favs_only_buys_sides_priced_80c_or_more(tmp_path):
    import argparse
    import makawan_lab as ml
    over = dict(next(s for s in ml.STRATEGIES if s[0] == "favs")[4])
    assert over == {"min_buy_px": {"5m": 0.80, "15m": 0.80}}
    a = argparse.Namespace(bankroll=1000.0, stake=20.0, delay=1.0)
    cfg = ml.make_config("favs", over, a)
    assert cfg.no_trade_horizons == ("1h",) and cfg.live_slip == 0.0
    cfg.out_dir = str(tmp_path)
    eng = tb.Engine(cfg, None, tb.SimClock(1000), tb.Recorder(cfg.out_dir), parallel=False)
    w = _window(call="UP", p_model=0.80, p_mkt=0.70, p_final=0.75)
    w.book = tb.Book([(0.69, 500)], [(0.71, 500)])
    eng._maybe_buy(w, 1000)
    assert w.paper_order is None and "under 80%" in w.value_note                        # 71c: baseline buys, favs does not
    w.p_model, w.p_mkt, w.p_final = 0.93, 0.85, 0.89
    w.book = tb.Book([(0.84, 500)], [(0.86, 500)])
    eng._maybe_buy(w, 1000)
    assert w.paper_order and w.paper_order["limit"] == pytest.approx(0.86)
    w.book = tb.Book([(0.76, 500)], [(0.78, 500)])                                      # leaks below 80c by arrival
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is None and "under min 0.80" in rows(tmp_path, "events.csv")[-1]["note"]


def test_lab_runs_only_the_five_kept_strategies_unless_asked(tmp_path, monkeypatch):
    import argparse
    import makawan_lab as ml
    monkeypatch.chdir(tmp_path)
    assert ml.DEFAULT == ["base15m", "sniper", "favs", "terry"]
    assert set(ml.RETIRED) == {"baseline", "cut", "cautious", "trend", "fade", "rising", "maker"} and set(ml.RETIRED) < set(ml.NAMES)
    a = argparse.Namespace(only="", simulate=True, seed=5, fresh=False, bankroll=1000.0, stake=20.0, delay=1.0,
                           spot="off", speed=0)
    bots, _ = ml.build(a, say=lambda *x: None)
    assert [b.name for b in bots] == ml.DEFAULT
    a.only = "trend"
    bots, _ = ml.build(a, say=lambda *x: None)
    assert [b.name for b in bots] == ["trend"]                        # a switched-off one still runs when asked


def by_name_eng(bots, name):
    return next(b.eng for b in bots if b.name == name)


# ---- lab strategy "terry": follow Terry Calls' first call ---------------------------------------------
def _terry(tmp_path, call=None, chance_now=None, age_ok=True, **over):
    import makawan_lab as ml
    cfg = tb.Config(out_dir=str(tmp_path), paper_delay_s=1.0, flat_stake=20.0, no_trade_horizons=("1h",),
                    call_up=0.999, keep_up=0.998, keep_down=0.002, call_down=0.001, **over)
    cfg.terry_max_age_s, cfg.terry_max_chase = 15.0, 0.02

    class NoApi:
        def get(self, *a, **k):
            raise tb.NotFound("x")
    eng = ml.TerryEngine(cfg, tb.Api(NoApi(), cfg), tb.SimClock(1000), tb.Recorder(cfg.out_dir), parallel=False)
    w = _window(h="5m", left=150.0, p_mkt=0.72)
    w.book = tb.Book([(0.70, 500)], [(0.72, 500)])
    eng.active["5m"] = w
    market = {"horizon": "5m", "market": w.info.slug, "window_end": tb.iso(w.info.we), "call": call,
              "chance_now": chance_now or {"up": 0.76, "down": 0.24}}
    feed = ml.TerryFeed("", fetch=lambda: {"markets": [market], "stale": False}, clock=lambda: 1000.0)
    feed.poll_once()
    eng.terry = feed
    return eng, w, feed, market


def test_terry_buys_his_fresh_first_call_with_honest_paper_fills(tmp_path):
    eng, w, feed, m = _terry(tmp_path, call={"side": "up", "at": tb.iso(995), "chance": 0.76, "price": 0.72})
    eng._maybe_buy(w, 1000)
    assert w.paper_order and w.paper_order["side"] == "UP"
    assert w.paper_order["limit"] == pytest.approx(0.72)               # live_slip=0: no +1c chase
    assert w.first_call == "UP" and w.first_call_p == pytest.approx(0.76)
    eng._paper_arrive(w, 1001, eng.cfg)
    assert w.position is not None and w.position.avg_px == pytest.approx(0.72) and w.position.cost <= 20.0
    assert w.position.fee == pytest.approx(tb.exch_fee(0.72, w.position.shares, 0.095))
    eng._maybe_buy(w, 1002)
    assert eng.paper_tries == 1                                         # one trade per window


def test_terry_skips_an_old_call_a_run_up_price_locks_and_no_call(tmp_path):
    eng, w, _, _ = _terry(tmp_path, call=None)
    eng._maybe_buy(w, 1000)
    assert w.paper_order is None and "waiting for Terry" in w.value_note
    eng, w, _, _ = _terry(tmp_path / "old", call={"side": "up", "at": tb.iso(970), "chance": 0.76, "price": 0.72})
    eng._maybe_buy(w, 1000)
    assert w.paper_order is None and w.entered and "30s old" in w.value_note
    eng, w, _, _ = _terry(tmp_path / "ran", call={"side": "up", "at": tb.iso(995), "chance": 0.76, "price": 0.68})
    eng._maybe_buy(w, 1000)                                             # ask 72c > 68c + 2c
    assert w.paper_order is None and not w.entered and "price ran" in w.value_note
    eng, w, _, m = _terry(tmp_path / "lock", call=None)
    m["lock"] = {"side": "up", "at": tb.iso(998), "chance": 0.96}       # a lock alone is never followed
    eng._maybe_buy(w, 1000)
    assert w.paper_order is None


def test_terry_down_call_buys_the_down_side_and_skips_1h_by_default(tmp_path):
    eng, w, _, _ = _terry(tmp_path, call={"side": "down", "at": tb.iso(996), "chance": 0.76, "price": 0.72},
                          chance_now={"up": 0.24, "down": 0.76})
    w.book, w.p_mkt = tb.Book([(0.28, 500)], [(0.30, 500)]), 0.29         # UP 28/30, so DOWN costs 1 - 28c = 72c
    eng._maybe_buy(w, 1000)
    assert w.paper_order["side"] == "DOWN" and w.paper_order["ask"] == pytest.approx(0.72)
    assert "1h" in eng.cfg.no_trade_horizons


def test_terry_requires_min_edge_at_try_and_arrival(tmp_path):
    # 73% chance at 72c ask -> negative edge after 1.9c fee -> skipped at TRY
    eng, w, _, _ = _terry(tmp_path / "try_edge", call={"side": "up", "at": tb.iso(995), "chance": 0.73, "price": 0.72},
                          chance_now={"up": 0.73, "down": 0.27})
    eng._maybe_buy(w, 1000)
    assert w.paper_order is None and "Terry edge" in w.value_note
    # Passes at TRY (76% @ 72c), then chance_now falls to 73% before arrival -> rejected as NOFILL
    eng2, w2, feed2, m2 = _terry(tmp_path / "arr_edge", call={"side": "up", "at": tb.iso(995), "chance": 0.76, "price": 0.72},
                                 chance_now={"up": 0.76, "down": 0.24})
    eng2._maybe_buy(w2, 1000)
    assert w2.paper_order is not None
    m2["chance_now"] = {"up": 0.73, "down": 0.27}
    eng2._paper_arrive(w2, 1001, eng2.cfg)
    assert w2.position is None and "Terry edge" in rows(tmp_path / "arr_edge", "events.csv")[-1]["note"]


def test_terry_obeys_the_shared_gap_rule_and_needs_fresh_data(tmp_path):
    eng, w, feed, _ = _terry(tmp_path, call={"side": "up", "at": tb.iso(995), "chance": 0.76, "price": 0.72},
                             chance_now={"up": 0.95, "down": 0.05})
    eng._maybe_buy(w, 1000)
    assert w.paper_order is None and "pts above the market" in w.value_note
    eng, w, feed, _ = _terry(tmp_path / "stale", call={"side": "up", "at": tb.iso(995), "chance": 0.76, "price": 0.72})
    feed.clock = lambda: 1100.0                                         # last answer 100 s old
    eng._maybe_buy(w, 1000)
    assert w.paper_order is None and "old" in w.value_note


def test_terry_key_is_found_in_env_or_a_dot_env_file_and_only_that_key(tmp_path):
    import makawan_lab as ml
    assert ml.find_terry_key({"TERRY_API_KEY": "tc_1"}, paths=[]) == ("tc_1", "environment")
    f = tmp_path / ".env"
    f.write_text("POLYMARKET_KEY_ID=abc\nTERRY_API_KEY='tc_2'\n")
    assert ml.find_terry_key({}, paths=[str(tmp_path / "missing"), str(f)]) == ("tc_2", str(f))
    assert ml.find_terry_key({}, paths=[]) == ("", "")
    feed = ml.TerryFeed("")
    assert "no TERRY_API_KEY" in feed.status()


def _terry_day(horizons, budget, seed=1, keys=""):
    """A simulated UTC day of Terry markets; returns (feed, requests used, seconds each call waited to be seen)."""
    import random
    import makawan_lab as ml
    rnd, day0, plan, t, seen = random.Random(seed), 1794700800.0, {}, [1794700800.0], {}
    rate = {"5m": 0.42, "15m": 0.53, "1h": 0.98}                        # share of windows that get a call

    def fetch():
        out = []
        for h, (L, start, last) in ml.CALL_WINDOWS.items():
            n = int(t[0] // L)
            if (h, n) not in plan:
                plan[(h, n)] = rnd.uniform(start, L - last) if rnd.random() < rate[h] else None
            ca, el = plan[(h, n)], t[0] - n * L
            call = {"side": "up", "at": tb.iso(n * L + ca), "chance": .8, "price": .8} if ca is not None and el >= ca else None
            out.append({"horizon": h, "market": f"{h}-{n}", "window_end": tb.iso(n * L + L), "seconds_left": int(L - el),
                        "call": call})
        return {"markets": out}
    feed = ml.TerryFeed(keys, fetch=fetch, clock=lambda: t[0], horizons=horizons, budget=budget)
    while t[0] < day0 + 86400:
        feed.poll_once(wall=t[0])
        for m in feed.by_slug.values():
            if m["call"] and m["market"] not in seen and m["horizon"] in horizons:
                seen[m["market"]] = t[0] - tb.parse_ts(m["call"]["at"])
        t[0] += feed.next_interval(t[0])
    return feed, feed.used, sorted(seen.values())


def test_terry_feed_checks_fast_only_while_a_first_call_is_due():
    import makawan_lab as ml
    t = [1000.0]
    data = {"markets": [{"horizon": "5m", "market": "a", "window_end": tb.iso(1210), "seconds_left": 210, "call": None}]}
    feed = ml.TerryFeed("", fetch=lambda: data, clock=lambda: t[0], horizons=("5m",), budget=0)
    assert feed.call_due()                                             # nothing heard yet: look quickly
    feed.poll_once(wall=t[0])
    assert feed.call_due() and feed.next_interval(t[0]) == pytest.approx(1.0)        # 1:30 in, no call yet
    data["markets"][0]["seconds_left"] = 250                          # only 0:50 in: a 5m call cannot come yet
    feed.poll_once(wall=t[0])
    assert not feed.call_due() and feed.next_interval(t[0]) == pytest.approx(15.0)
    data["markets"][0].update(seconds_left=150, call={"side": "up", "at": tb.iso(990), "chance": .8, "price": .8})
    feed.poll_once(wall=t[0])
    assert not feed.call_due()                                         # called already: no hurry
    data["markets"][0].update(seconds_left=10, call=None)
    feed.poll_once(wall=t[0])
    assert not feed.call_due()                                         # never in the last 0:20
    data["markets"][0].update(horizon="1h", seconds_left=2000)
    feed.poll_once(wall=t[0])
    assert not feed.call_due()                                         # not a length this feed watches


def test_terry_feed_stays_inside_the_free_daily_cap_and_goes_1s_without_one():
    feed, used, seen = _terry_day(("5m", "15m", "1h"), budget=19000)
    assert 17000 < used <= 19000                                       # uses the allowance, never more
    assert seen[len(seen) // 2] < 3.5
    feed, used, seen = _terry_day(("5m", "15m", "1h"), budget=0)
    assert used > 40000 and seen[int(len(seen) * .9)] <= 1.5          # Unlimited plan: calls seen within ~1 s


def test_terry_finds_every_key_in_env_or_dot_env(tmp_path):
    import makawan_lab as ml
    env = {"TERRY_API_KEY_2": "tc_b", "TERRY_API_KEY": "tc_a", "PATH": "x"}
    assert ml.find_terry_keys(env, paths=[]) == (["tc_a", "tc_b"], "environment")
    f = tmp_path / ".env"
    f.write_text("TERRY_API_KEY=tc_a\n# TERRY_API_KEY_3=old\nexport TERRY_API_KEY_2=\"tc_b\"\nTERRY_API_KEY_4=tc_a\n")
    assert ml.find_terry_keys({}, paths=[str(f)]) == (["tc_a", "tc_b"], str(f))      # comments and repeats skipped
    assert ml.find_terry_key({}, paths=[str(f)]) == ("tc_a", str(f))


def test_terry_feed_takes_turns_with_two_keys_and_respects_each_keys_allowance(tmp_path):
    import makawan_lab as ml

    class Feed(ml.TerryFeed):
        sent = []

        def _http(self, key=None):
            self.sent.append(key)
            return {"markets": []}
    feed = Feed(["tc_a", "tc_b"], budget=3, state_path=str(tmp_path / "c.json"))
    assert feed.budget == 6 and feed.every == pytest.approx(1.0)
    for i in range(8):
        feed.poll_once(wall=1794700800.0 + i)
    assert Feed.sent == ["tc_a", "tc_b", "tc_a", "tc_b", "tc_a", "tc_b"]         # 3 each, then both used up
    assert feed.used == 6 and sorted(feed.used_by.values()) == [3, 3]
    feed._save_count()
    assert "tc_a" not in (tmp_path / "c.json").read_text()                  # saved under a short name, not the key
    Feed.sent = []
    feed2 = Feed(["tc_a", "tc_b"], budget=19000)
    feed2.key_backoff["tc_a"] = 1794700800.0 + 60                          # tc_a got a 429: rest it
    feed2.poll_once(wall=1794700800.0)
    feed2.poll_once(wall=1794700801.0)
    assert Feed.sent == ["tc_b", "tc_b"]


def test_two_keys_double_the_day_and_speed_up_the_checks():
    _, used1, seen1 = _terry_day(("5m", "15m", "1h"), budget=19000)
    _, used2, seen2 = _terry_day(("5m", "15m", "1h"), budget=19000, keys=["tc_a", "tc_b"])
    assert 34000 < used2 <= 38000
    assert seen2[len(seen2) // 2] < seen1[len(seen1) // 2]


def test_terry_feed_remembers_todays_count_across_a_restart(tmp_path):
    import makawan_lab as ml
    path = str(tmp_path / "terry_feed.json")
    f1 = ml.TerryFeed("", fetch=lambda: {"markets": []}, state_path=path)
    for _ in range(50):
        f1.poll_once()
    f2 = ml.TerryFeed("", fetch=lambda: {"markets": []}, state_path=path)
    assert f2.used == 50


def test_lab_feed_lets_a_cached_not_found_book_be_asked_past_the_cache():
    import makawan_lab as ml

    class Cdn:
        """Polymarket's cache: the plain address keeps answering 'not found'; the one-off address gets the book."""
        backoff_until = 0.0

        def get(self, path, params=None, headers=None):
            if not params:
                raise tb.NotFound(path)
            return {"marketData": {"bids": [{"px": {"value": "0.55"}, "qty": "10"}],
                                   "offers": [{"px": {"value": "0.57"}, "qty": "10"}]}}
    api = tb.Api(ml.CachedTransport(Cdn()), tb.Config())
    b = api.book("cpc-btc-updown-5m-2026-11-13-2025z")
    assert b is not None and b.bid == 0.55 and b.ask == 0.57          # it used to stay 'not found' in the lab
    b2 = tb.Api(api.t, tb.Config()).book("cpc-btc-updown-5m-2026-11-13-2025z")   # another strategy, same feed
    assert b2.ask == 0.57


def test_watchdog_sends_an_hourly_status_message():
    import makawan_watchdog as wd
    sent = []
    n = wd.Notifier("", mac=False, say=lambda *a: None)
    n.send = lambda title, body, urgent=False: sent.append((title, body))
    now = 1_800_000_000.0
    lab = {"rows": [{"name": "fade", "net": 12.5, "trades": 30, "ret": 0.021, "halted": ""},
                    {"name": "baseline", "net": -4.0, "trades": 28, "ret": -0.007, "halted": ""}]}
    dog = wd.Watchdog(wd.parse_bots("lab=8780,LIVE?=8770"), n, status_every=3600,
                      fetch=lambda port: _ok_state(dog.last_loop or now) if port == 8780 else None,
                      fetch_lab=lambda port: lab if port == 8780 else None)
    dog.step(now)
    assert len(sent) == 1 and sent[0][0].startswith("Makawan: status")
    body = sent[0][1]
    assert "fade: +$12.50 (30 trades, +2.1c per $1)" in body and "baseline: -$4.00" in body
    assert "LIVE" not in body and "All running normally." in body       # a live bot never started is not mentioned
    for k in range(1, 120):
        dog.step(now + 30 * k)
    assert len(sent) == 1                                              # not again within the hour
    dog.step(now + 3600)
    assert len(sent) == 2
    st = _ok_state(now)
    st["stats"] = {"5m": {"trades": 3, "pnl": -1.5}, "15m": {"trades": 1, "pnl": 0.5}}
    st["account"] = {"equity": 74.2}
    dog2 = wd.Watchdog([("live", 1)], n, status_every=3600, fetch=lambda port: st, fetch_lab=lambda port: None)
    dog2.step(now)
    assert "live: Polymarket $74.20, -$1.00 on 4 trades" in sent[-1][1]
    dog3 = wd.Watchdog([("live", 1)], n, status_every=0, fetch=lambda port: st, fetch_lab=lambda port: None)
    dog3.step(now)
    assert len(sent) == 3                                              # 0 = off


def test_watchdog_holds_phone_messages_in_quiet_hours_and_sends_one_summary():
    import makawan_watchdog as wd
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = [_dt(2026, 11, 22, 0, 30, tzinfo=et).timestamp()]                # 12:30 AM Eastern
    sent = []
    post = lambda url, **kw: sent.append((kw["headers"]["Title"], kw["data"].decode())) or type("R", (), {"status_code": 200})()
    n = wd.Notifier("topic", post=post, mac=False, say=lambda *a: None, quiet=wd.parse_quiet("00:00-07:00"),
                    clock=lambda: now[0])
    assert wd.parse_quiet("off") is None and wd.parse_quiet("23:30-6") == (1410, 360)
    n.send("Makawan: status 12:30 AM", "all fine")
    n.send("Makawan: PROBLEM", "lab: stuck", urgent=True)
    now[0] += 600
    n.send("Makawan: OK again", "lab: updating again")
    n.release()
    assert sent == [] and len(n.held) == 3                                # nothing to the phone after midnight
    now[0] = _dt(2026, 11, 22, 7, 0, 5, tzinfo=et).timestamp()            # 7:00 AM: one summary
    n.release()
    assert len(sent) == 1 and sent[0][0] == "Makawan: overnight summary"
    assert "12:30 AM PROBLEM: lab: stuck" in sent[0][1] and "OK again" in sent[0][1] and "1 hourly status" in sent[0][1]
    n.send("Makawan: status 8:00 AM", "all fine")                         # daytime: sent straight away
    assert len(sent) == 2 and not n.held
    night = wd.Notifier("topic", post=post, mac=False, say=lambda *a: None, quiet=(1380, 360), clock=lambda: now[0])
    now[0] = _dt(2026, 11, 22, 23, 15, tzinfo=et).timestamp()
    assert night.is_quiet() and not wd.Notifier("t", quiet=None).is_quiet()   # a span across midnight works too


def test_watchdog_heartbeat_stops_when_the_lab_is_stuck_down_or_has_no_prices():
    import makawan_watchdog as wd
    pings = []
    n = wd.Notifier("", mac=False, say=lambda *a: None)
    st = {"ready": True, "mode": "live", "last_tick": 1000.0, "print_ts": 998.0, "guard": {}}
    states = {"ok": st, "stuck": dict(st, last_tick=900.0), "feed": dict(st, print_ts=900.0), "down": None}
    now = [1000.0]
    for case, beats in (("ok", 1), ("stuck", 0), ("feed", 0), ("down", 0)):
        pings.clear()
        dog = wd.Watchdog([("lab", 1)], n, heartbeat="https://hc-ping.com/x", status_every=0,
                          get=lambda url, timeout=0: pings.append(url), fetch=lambda port, c=case: states[c],
                          fetch_lab=lambda port: None)
        dog.step(now[0])
        assert len(pings) == beats, case
        assert dog.healthy == (case == "ok")
