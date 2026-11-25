#!/usr/bin/env python3
"""
run_both.py - run the "late-only" and "any time" bots side by side, each as a taker and as a (paper) maker
==========================================================================================
    python3 run_both.py            # starts all four; Ctrl+C stops them all
    python3 run_both.py --open     # also opens the four pages in your browser
    python3 run_both.py --no-maker # only the two taker bots
    (with --simulate only the two taker bots run: maker mode is tested on live paper data only)

  late-only : http://localhost:8765   trades only in the last 2 min of a 5m and the last 5 min of a 15m
  any time  : http://localhost:8766   trades at any time in every window
  late maker: http://localhost:8767   late-only, but posts paper limit bids instead of buying at the ask
  any maker : http://localhost:8768   any time, with paper limit bids
Each keeps its own logs (makawan_out_late/ and makawan_out_any/). Both read the same live prices, so the comparison is fair.
After a week or two, compare makawan_out_late/windows.csv with makawan_out_any/windows.csv.
"""
import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))


def pump(proc, tag):
    for line in proc.stdout:
        print(f"[{tag}] {line.rstrip()}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Run the late-only and any-time bots side by side, each as taker and as maker")
    ap.add_argument("--late-port", type=int, default=8765)
    ap.add_argument("--any-port", type=int, default=8766)
    ap.add_argument("--late-maker-port", type=int, default=8767)
    ap.add_argument("--any-maker-port", type=int, default=8768)
    ap.add_argument("--no-maker", action="store_true", help="run only the two taker bots")
    ap.add_argument("--open", action="store_true", help="open the pages in your browser")
    ap.add_argument("--simulate", action="store_true", help="fake market, no network")
    ap.add_argument("--speed", type=float, default=30.0, help="simulation speed")
    ap.add_argument("--bankroll", type=float, default=None)
    ap.add_argument("--stake", type=float, default=None, help="flat dollars per trade (default 20)")
    ap.add_argument("--stop-loss", type=float, default=None,
                    help="halt a bot once it has lost this share of its starting bankroll (default 0.5; 0 = off)")
    ap.add_argument("--daily-loss", type=float, default=None,
                    help="pause a bot until tomorrow after it loses this many dollars in a day (default off)")
    ap.add_argument("--resume", action="store_true", help="clear a stop-loss halt on every bot")
    ap.add_argument("--params", default=None,
                    help="tuned settings file (default: makawan_tune/params.json if you applied a proposal; 'none' = built-in)")
    ap.add_argument("--no-watchdog", action="store_true", help="do not start the watchdog alongside the bots")
    ap.add_argument("--report-at", default=None, help="time of the daily report, e.g. 07:00 (watchdog default 07:00)")
    ap.add_argument("--fresh", action="store_true", help="archive every bot's history and start from scratch")
    ap.add_argument("--allow-sleep", action="store_true", help="do not keep the Mac awake while the bots run")
    ap.add_argument("--spot", choices=("off", "watch", "use"), default="watch",
                    help="Coinbase live price for every bot: watch = log only (default), use = trade on it, off")
    ap.add_argument("--stream", action="store_true",
                    help="live order-book stream instead of polling (needs your API key in this Terminal window)")
    a = ap.parse_args()
    base = [sys.executable, os.path.join(HERE, "makawan_web.py")]
    if a.bankroll is not None:
        base += ["--bankroll", str(a.bankroll)]
    for flag, val in (("--stake", a.stake), ("--stop-loss", a.stop_loss), ("--daily-loss", a.daily_loss)):
        if val is not None:
            base += [flag, str(val)]
    if a.resume:
        base += ["--resume"]
    params = a.params or os.path.join("makawan_tune", "params.json")
    if params != "none" and os.path.exists(params):
        base += ["--params", params]
        print(f"Using the tuned settings you applied: {params}   (--params none ignores them)")
    elif a.params and a.params != "none":
        sys.exit(f"Settings file not found: {a.params}")
    if a.fresh:
        base += ["--fresh"]
    if a.stream and not a.simulate:
        base += ["--stream"]
    base += ["--spot", a.spot]
    if a.simulate:
        base += ["--simulate", "--speed", str(a.speed)]
    sfx = "_sim" if a.simulate else ""                # keep simulated history apart from the live folders
    jobs = [("late      ", a.late_port, ["--out", "makawan_out_late" + sfx]),
            ("any       ", a.any_port, ["--trade-last", "none", "--out", "makawan_out_any" + sfx])]
    if a.simulate and not a.no_maker:
        print("Simulation: running only the two taker bots (maker mode needs live trades to be tested fairly).")
    if not a.no_maker and not a.simulate:
        jobs += [("late maker", a.late_maker_port, ["--exec", "maker", "--out", "makawan_out_late_maker" + sfx]),
                 ("any maker ", a.any_maker_port, ["--exec", "maker", "--trade-last", "none",
                                                   "--out", "makawan_out_any_maker" + sfx])]
    jobs = [(t, base + ["--port", str(port)] + extra) for t, port, extra in jobs]
    ports = [int(c[c.index("--port") + 1]) for _, c in jobs]
    def stop(*_):                      # Ctrl+C (SIGINT) or `kill` (SIGTERM): stop both bots
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if not a.simulate and not a.allow_sleep and sys.platform == "darwin":
        import shutil
        exe = shutil.which("caffeinate")
        if exe:                                        # macOS's own tool; ends when this launcher ends
            try:
                subprocess.Popen([exe, "-i", "-s", "-w", str(os.getpid())])
                os.environ["MAKAWAN_AWAKE"] = "1"
                print("Keeping this Mac awake while the bots run (closing the lid still puts it to sleep; "
                      "--allow-sleep turns this off).")
            except OSError:
                pass
    procs, code, dog = [], 0, None
    try:
        for tag, cmd in jobs:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=os.getcwd())
            procs.append((tag, p))
            threading.Thread(target=pump, args=(p, tag), daemon=True).start()
        time.sleep(2.0)
        if not a.no_watchdog and not a.simulate:       # the watchdog is separate: if it stops, the bots keep going
            wcmd = [sys.executable, os.path.join(HERE, "makawan_watchdog.py"), "--bots",
                    ",".join(f"{t.strip()}={port}" for (t, _), port in zip(jobs, ports)) + ",LIVE?=8770"]
            if a.report_at:
                wcmd += ["--report-at", a.report_at]
            dog = subprocess.Popen(wcmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=os.getcwd())
            threading.Thread(target=pump, args=(dog, "watchdog  "), daemon=True).start()
        for tag, p in procs:
            if p.poll() is not None:
                print(f"[{tag}] stopped right away (see the message above). Is the port already in use? "
                      f"Change it with --late-port / --any-port / --late-maker-port / --any-maker-port.")
                code = 1
        if code == 0:
            print("")
            for (tag, _), port in zip(jobs, ports):
                print(f"  {tag.strip():11s} http://localhost:{port}", flush=True)
            print("  (Ctrl+C stops them all)\n", flush=True)
            if a.open:
                for port in ports:
                    webbrowser.open(f"http://localhost:{port}")
            while all(p.poll() is None for _, p in procs):
                time.sleep(1.0)
            for tag, p in procs:
                if p.poll() not in (None, 0):
                    print(f"[{tag}] exited with code {p.returncode}")
    except KeyboardInterrupt:
        pass
    finally:                           # always runs, even for a Ctrl+C during start-up
        procs = procs + ([("watchdog", dog)] if dog else [])
        for _, p in procs:
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("All stopped.", flush=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
