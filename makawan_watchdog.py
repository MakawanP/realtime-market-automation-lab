#!/usr/bin/env python3
"""
makawan_watchdog.py - watches the bots and messages your phone when something breaks
====================================================================================
run_both.py starts this for you. By hand:

    python3 makawan_watchdog.py --setup      # one time: make a private phone channel and send a test message
    python3 makawan_watchdog.py --test       # send a test message
    python3 makawan_watchdog.py              # watch the four bots (8765-8768) until Ctrl+C
    python3 makawan_watchdog.py --once       # check once, print what it sees, and stop

What it checks, every 30 seconds:
  * each bot answers                         (a bot crashed or was closed)
  * each bot is still ticking                (a bot is stuck)
  * the Bitcoin price feed is fresh          (wifi or Polymarket trouble)
  * the risk guard has not halted trading    (stop-loss or daily limit hit), and warns when a bot is close to its stop
  * this Mac did not sleep                   (it tells you when it wakes up and how long it was out)
It messages you when a problem STARTS and when it CLEARS, and reminds you once an hour while it lasts.
Quiet hours (default midnight to 9:00 AM Eastern, --quiet): nothing goes to your phone; what happened is logged and
sent as one "overnight summary" at 9:00 (with the 7:00 daily report in it), without the hourly status
messages.

It also runs two jobs on a schedule and sends you the headline:
  * the daily report  (makawan_report.py) at 07:00
  * the weekly tuner  (makawan_tune.py backtest --propose) on Sunday at 06:30. It only PROPOSES; nothing is applied.

Phone messages use ntfy (https://ntfy.sh), a free push service: install the "ntfy" app, subscribe to your channel
name, done. The messages (bot names, problems, profit and loss figures) pass through ntfy's servers, and anyone who
knows the channel name can read them, so --setup makes a long random name. No API key is ever sent.

A computer that is asleep, switched off or offline cannot send anything. To be told about THAT, give the watchdog a
"heartbeat" address from a free service such as healthchecks.io (--set-heartbeat URL): the watchdog pings it every
minute while the bots answer, are not stuck and get fresh prices, and the service messages you (from its own
servers) when the pings stop: internet down, computer off or asleep, lab crashed or frozen, or price feed dead.

It never places, changes or cancels an order. It only reads the bots' own status pages on this computer.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timedelta

import requests

import makawan_bot as tb

REQUIRES = "2026.11.13"
if getattr(tb, "__version__", None) != REQUIRES:
    sys.exit("{} and makawan_bot.py come from different downloads. Put ALL the files from the latest download in one "
             "folder.".format(os.path.basename(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = "makawan_watchdog"                       # settings, state and log live here
DEFAULT_BOTS = "late=8765,any=8766,late maker=8767,any maker=8768,LIVE?=8770"   # ? = only watched once it has run
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def clock(ts):
    return datetime.fromtimestamp(ts, tb.DISPLAY_TZ).strftime("%I:%M %p").lstrip("0")


def span(seconds):
    m = int(round(seconds / 60.0))
    return f"{m} min" if m < 90 else f"{m // 60} h {m % 60:02d} min"


def ascii_only(s):
    return s.encode("ascii", "replace").decode("ascii")


def parse_bots(text):
    out = []
    for part in text.split(","):
        if not part.strip():
            continue
        name, _, port = part.rpartition("=")
        out.append((name.strip() or f"bot {port}", int(port)))    # a name ending in ? marks an optional bot
    return out


def parse_hhmm(text):
    if not text or text.lower() in ("off", "none", "no"):
        return None
    h, _, m = text.partition(":")
    h, m = int(h), int(m or 0)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"'{text}' is not a time of day like 07:00")
    return h, m


# --------------------------------------------------------------------------
# sending messages
# --------------------------------------------------------------------------
def parse_quiet(text):
    """'00:00-07:00' -> (0, 420) minutes of the day; 'off' or '' -> None."""
    text = (text or "").strip().lower()
    if text in ("", "off", "none"):
        return None
    try:
        a, b = text.split("-")
        mins = [int(x.split(":")[0]) * 60 + (int(x.split(":")[1]) if ":" in x else 0) for x in (a, b)]
    except (ValueError, IndexError):
        sys.exit(f"--quiet must look like 00:00-07:00 (got {text!r})")
    if not all(0 <= m <= 1440 for m in mins) or mins[0] == mins[1]:
        sys.exit(f"--quiet must look like 00:00-07:00 (got {text!r})")
    return tuple(mins)


class Notifier:
    """Phone push (ntfy) + a notification on this Mac + the log file. Unsent phone messages are retried."""

    def __init__(self, topic="", server="https://ntfy.sh", mac=True, log_path=None, post=None, say=print,
                 quiet=None, tz="America/New_York", clock=time.time):
        self.topic, self.server, self.mac = (topic or "").strip(), server.rstrip("/"), mac
        self.log_path, self.say = log_path, say
        self.post = post or (lambda url, **kw: requests.post(url, **kw))
        self.queue = []                               # phone messages that could not be sent yet
        # quiet hours: (start minute, end minute) of the day in `tz`, e.g. (0, 420) = midnight to 7:00 AM. Phone
        # messages written then are held (still logged) and sent as ONE overnight summary when the quiet hours end.
        self.quiet, self.clock, self.held = quiet, clock, []
        try:
            from zoneinfo import ZoneInfo
            self.tz = ZoneInfo(tz)
        except Exception:
            self.tz = None

    def is_quiet(self, t=None):
        if not self.quiet:
            return False
        t = self.clock() if t is None else t
        d = datetime.fromtimestamp(t, self.tz) if self.tz else datetime.fromtimestamp(t)
        m = d.hour * 60 + d.minute
        a, b = self.quiet
        return a <= m < b if a <= b else (m >= a or m < b)

    def release(self):
        """After the quiet hours: send what was held overnight as one message (hourly status messages are left out,
        since the next one comes within the hour)."""
        if not self.held or self.is_quiet():
            return
        held, self.held = self.held, []
        real = [(t, title, body) for t, title, body in held if not title.startswith("Makawan: status")]
        skipped = len(held) - len(real)
        if not real:
            return
        tf = lambda t: (datetime.fromtimestamp(t, self.tz) if self.tz else datetime.fromtimestamp(t)).strftime("%I:%M %p").lstrip("0")
        lines = [f"{tf(t)} {title.replace('Makawan: ', '')}: {body}" for t, title, body in real]
        text = "\n".join(lines)
        if len(text) > 3500:
            text = text[:3500] + " ..."
        note = f"\n({skipped} hourly status message(s) not sent.)" if skipped else ""
        self.queue.append(("Makawan: overnight summary", text + note,
                           any("PROBLEM" in title or "still a problem" in title for _, title, _ in real), self.clock()))
        self.flush()

    def send(self, title, body, urgent=False):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.say(f"[{stamp}] {title}: {body}")
        if self.log_path:
            try:
                os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(f"{stamp}\t{title}\t{body}\n".replace("\n", " ").rstrip() + "\n")
            except OSError:
                pass
        if self.mac and sys.platform == "darwin":
            try:                                      # the text goes in as arguments, never into the script itself
                subprocess.run(["osascript", "-e", "on run argv", "-e",
                                "display notification (item 2 of argv) with title (item 1 of argv)", "-e", "end run",
                                title, body[:240]], timeout=5, capture_output=True)
            except (OSError, subprocess.SubprocessError):
                pass
        if self.topic:
            if self.is_quiet():
                self.held.append((self.clock(), title, body))      # quiet hours: sent in the morning summary
                return
            self.queue.append((title, body, urgent, time.time()))
            self.flush()

    def flush(self):
        """Try to deliver waiting phone messages. Returns how many are still waiting."""
        keep = []
        for title, body, urgent, t in self.queue[-20:]:
            late = f" (delayed {span(time.time() - t)})" if time.time() - t > 120 else ""
            try:
                r = self.post(f"{self.server}/{self.topic}", data=(body + late).encode("utf-8"),
                              headers={"Title": ascii_only(title), "Priority": "high" if urgent else "default",
                                       "Tags": "warning" if urgent else "robot"}, timeout=10)
                if getattr(r, "status_code", 200) >= 400:
                    raise requests.RequestException(f"ntfy answered {r.status_code}")
            except (requests.RequestException, OSError):
                keep.append((title, body, urgent, t))
        self.queue = keep
        return len(keep)


# --------------------------------------------------------------------------
# what counts as a problem
# --------------------------------------------------------------------------
def check_bot(name, st, now, stale_s=45.0):
    """st: the bot's /api/state (dict) or None if it did not answer. Returns {key: (message, urgent, instant)}."""
    if st is None:
        return {f"{name}:down": (f"{name}: not answering. It has crashed or been closed.", True, False)}
    out = {}
    if not st.get("ready"):
        out[f"{name}:starting"] = (f"{name}: still starting up (no data yet).", False, False)
        return out
    live = st.get("mode") == "live"
    if live and st.get("last_tick") and now - st["last_tick"] > stale_s:
        out[f"{name}:stuck"] = (f"{name}: stuck. It has not updated for {span(now - st['last_tick'])}.", True, False)
    elif live and (st.get("last_error") or (st.get("print_ts") and now - st["print_ts"] > stale_s)):
        why = st.get("last_error") or f"last Bitcoin price is {span(now - st['print_ts'])} old"
        out[f"{name}:feed"] = (f"{name}: price feed trouble ({str(why)[:120]}). No new trades are made on stale prices.",
                               True, False)
    g = st.get("guard") or {}
    eq, bank = st.get("equity"), st.get("bankroll")
    if g.get("halted"):
        out[f"{name}:halted"] = (f"{name}: TRADING HALTED. {g['halted']}. Restart with --resume to trade again.",
                                 True, True)
    elif g.get("paused_today"):
        out[f"{name}:paused"] = (f"{name}: paused until tomorrow, daily loss limit reached "
                                 f"(today ${g.get('today_pnl', 0):+,.2f}).", True, True)
    elif g.get("floor") is not None and eq is not None and bank and bank > g["floor"]:
        used = (bank - eq) / (bank - g["floor"])
        if used >= 0.8:
            out[f"{name}:near-stop"] = (f"{name}: {used:.0%} of the way to its stop-loss (${eq:,.2f} now, "
                                        f"stops at ${g['floor']:,.2f}).", False, True)
    books = str(st.get("books") or "")
    if books.startswith("off"):
        out[f"{name}:stream"] = (f"{name}: live order-book stream switched off; using slower polling.", False, False)
    return out


CLEARED = {"down": "answering again", "stuck": "updating again", "feed": "price feed is back",
           "halted": "trading halt cleared", "paused": "daily pause is over",
           "near-stop": "no longer close to its stop-loss", "stream": "live order-book stream is back"}


class Tracker:
    """Turns the current set of problems into messages: when one starts, when it clears, and hourly reminders."""

    def __init__(self, confirm=2, remind_s=3600.0):
        self.confirm, self.remind_s = confirm, remind_s
        self.seen = {}          # key -> consecutive checks it has been present
        self.active = {}        # key -> {"since", "last", "msg", "urgent"}

    def update(self, problems, now, silent=()):
        """Returns (started, cleared, reminders): lists of message strings. `silent` names bots that did not
        answer: what was known about them (a halt, say) is kept as it was instead of being called fixed."""
        started, cleared, remind = [], [], []
        for key, (msg, urgent, instant) in problems.items():
            self.seen[key] = self.seen.get(key, 0) + 1
            if key in self.active:
                a = self.active[key]
                a["msg"] = msg
                if now - a["last"] >= self.remind_s:
                    a["last"] = now
                    remind.append(f"still: {msg} (for {span(now - a['since'])})")
            elif instant or self.seen[key] >= self.confirm:
                self.active[key] = {"since": now, "last": now, "msg": msg, "urgent": urgent}
                started.append((msg, urgent))
        for key in [k for k in self.seen if k not in problems]:
            name, _, kind = key.rpartition(":")
            if name in silent and kind != "down":
                continue
            del self.seen[key]
            if key in self.active:
                a = self.active.pop(key)
                if kind in CLEARED:
                    cleared.append(f"{name}: {CLEARED[kind]} (after {span(now - a['since'])})")
        return started, cleared, remind


# --------------------------------------------------------------------------
# scheduled jobs (daily report, weekly tuner)
# --------------------------------------------------------------------------
def last_slot(now, at, weekday=None):
    """The most recent scheduled moment at or before `now` (a datetime in the display time zone)."""
    d = datetime.fromtimestamp(now, tb.DISPLAY_TZ)
    slot = d.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    if weekday is not None:
        slot -= timedelta(days=(slot.weekday() - weekday) % 7)
    if slot > d:
        slot -= timedelta(days=7 if weekday is not None else 1)
    return slot


class Schedule:
    """Runs each job once per slot. A slot missed while the Mac was off or asleep runs when it is back."""

    def __init__(self, state_path, jobs, now):
        self.path, self.jobs = state_path, jobs       # jobs: {name: {"at": (h, m), "weekday": int|None, "cmd": [...]}}
        self.state = load_json(state_path)
        self.running = {}                             # name -> (Popen, slot key, started)
        changed = False
        for name, j in jobs.items():                  # first time: wait for the next slot instead of running at once
            if name not in self.state:
                self.state[name] = last_slot(now, j["at"], j.get("weekday")).isoformat()
                changed = True
        if changed:
            save_json(self.path, self.state)

    def due(self, now):
        return [n for n, j in self.jobs.items() if n not in self.running
                and last_slot(now, j["at"], j.get("weekday")).isoformat() != self.state.get(n)]

    def start(self, name, now, popen=subprocess.Popen):
        j = self.jobs[name]
        p = popen(j["cmd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=os.getcwd())
        self.running[name] = (p, last_slot(now, j["at"], j.get("weekday")).isoformat(), now)

    def finished(self, now):
        """[(name, return code, output)] for jobs that have ended (or run too long and were stopped)."""
        done = []
        for name, (p, slot, t0) in list(self.running.items()):
            limit = self.jobs[name].get("timeout", 1800)
            if p.poll() is None and now - t0 < limit:
                continue
            if p.poll() is None:
                p.kill()
            try:
                out = p.communicate(timeout=10)[0] or ""
            except (subprocess.SubprocessError, OSError, ValueError):
                out = ""
            del self.running[name]
            self.state[name] = slot
            save_json(self.path, self.state)
            done.append((name, p.returncode, out))
        return done


def headline(output, fallback):
    lines = [x.split("SUMMARY:", 1)[1].strip() for x in output.splitlines() if "SUMMARY:" in x]
    return "\n".join(lines)[:900] if lines else fallback


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------
def fetch_json(port, path, get=None, timeout=5):
    try:
        r = (get or requests.get)(f"http://127.0.0.1:{port}{path}", timeout=timeout)
        if getattr(r, "status_code", 200) != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError, OSError):
        return None


def fetch_state(port, get=None, timeout=5):
    try:
        r = (get or requests.get)(f"http://127.0.0.1:{port}/api/state", timeout=timeout)
        return r.json()
    except (requests.RequestException, ValueError, OSError):
        return None


class Watchdog:
    def __init__(self, bots, notifier, schedule=None, every=30.0, heartbeat="", get=None, fetch=None,
                 status_every=0.0, fetch_lab=None):
        self.bots, self.n, self.schedule, self.every = bots, notifier, schedule, every
        self.status_every = status_every              # seconds between status messages (0 = off)
        self.fetch_lab = fetch_lab or (lambda port: fetch_json(port, "/api/lab", self.get))
        self.last_status = None
        self.heartbeat, self.get = (heartbeat or "").strip(), get or requests.get
        self.fetch = fetch or (lambda port: fetch_state(port, self.get))
        self.tracker = Tracker()
        self.optional = {n.rstrip("?").strip() for n, _ in bots if n.endswith("?")}
        self.bots = [(n.rstrip("?").strip(), p) for n, p in bots]
        self.met = set()                              # optional bots that have answered at least once
        self.last_loop = None
        self.last_beat = 0.0
        self.healthy = False

    def step(self, now):
        # 1. did this computer sleep since the last look?
        if self.last_loop is not None and now - self.last_loop > max(90.0, 3 * self.every):
            self.n.send("Makawan: Mac was asleep",
                        f"This Mac was asleep or paused from {clock(self.last_loop)} to {clock(now)} "
                        f"({span(now - self.last_loop)}). The bots missed that time.", urgent=True)
            self.tracker.seen.clear()                 # give everything a moment to catch up before judging it
            self.last_loop = now
            return {}
        self.last_loop = now
        # 2. the bots
        problems, answered, silent = {}, 0, []
        for name, port in self.bots:
            st = self.fetch(port)
            answered += st is not None
            if st is not None:
                self.met.add(name)
            elif name in self.optional and name not in self.met:
                continue                              # an optional bot (the live one) that has never been started
            if st is None:
                silent.append(name)
            problems.update(check_bot(name, st, now))
        started, cleared, remind = self.tracker.update(problems, now, silent)
        if started:
            self.n.send("Makawan: PROBLEM", "\n".join(m for m, _ in started), urgent=any(u for _, u in started))
        if cleared:
            self.n.send("Makawan: OK again", "\n".join(cleared))
        if remind:
            self.n.send("Makawan: still a problem", "\n".join(remind))
        if hasattr(self.n, "release"):
            self.n.release()                          # quiet hours just ended: send the overnight summary
        if self.n.queue:
            self.n.flush()
        # 3. the hourly status message
        if self.status_every and (self.last_status is None or now - self.last_status >= self.status_every):
            self.last_status = now
            self.n.send("Makawan: status " + clock(now), self.status_text(now, problems))
        # 4. heartbeat: "this computer is awake and online, the bots answer, are not stuck and get fresh prices".
        # It goes out only while all that is true, so the heartbeat service raises the alarm (from ITS servers, which
        # still works when this computer has no internet) when the internet drops, the computer sleeps or switches
        # off, the lab crashes or freezes, or the price feed stops.
        broken = [k for k in problems if k.endswith((":down", ":stuck", ":feed", ":starting"))]
        self.healthy = bool(answered) and not broken
        if self.heartbeat and self.healthy and now - self.last_beat >= 55:
            try:
                self.get(self.heartbeat, timeout=8)
                self.last_beat = now
            except (requests.RequestException, OSError):
                pass
        # 4. scheduled jobs
        if self.schedule:
            for name, code, out in self.schedule.finished(now):
                if code == 0:
                    self.n.send(f"Makawan: {name}", headline(out, f"The {name} finished."))
                else:
                    tail = " ".join(out.strip().splitlines()[-2:])[:300]
                    self.n.send(f"Makawan: {name} failed", f"The {name} did not finish. {tail}")
            for name in self.schedule.due(now):
                try:
                    self.schedule.start(name, now)
                except OSError as e:
                    self.n.send(f"Makawan: {name} failed", f"Could not start the {name}: {e}")
        return problems

    def status_text(self, now, problems):
        """One short message: each bot or lab strategy's result so far, and anything wrong."""
        lines = []
        for name, port in self.bots:
            lab = self.fetch_lab(port)
            if lab and lab.get("rows"):                   # the strategy lab: one line per strategy
                for r in lab["rows"]:
                    per = f", {r['ret'] * 100:+.1f}c per $1" if r.get("trades") and r.get("ret") is not None else ""
                    lines.append(f"{r['name']}: {'-' if r['net'] < 0 else '+'}${abs(r['net']):,.2f} "
                                 f"({r['trades']} trades{per})" + (" HALTED" if r.get("halted") else ""))
                continue
            st = self.fetch(port)
            if st is None:
                if name not in self.optional or name in self.met:
                    lines.append(f"{name}: NOT ANSWERING")
                continue
            if not st.get("ready"):
                lines.append(f"{name}: starting")
                continue
            stats = st.get("stats") or {}
            trades = sum(int(v.get("trades") or 0) for v in stats.values())
            pnl = sum(float(v.get("pnl") or 0.0) for v in stats.values())
            acct = st.get("account") or {}
            money = (f"Polymarket ${acct['equity']:,.2f}" if acct.get("equity") is not None
                     else f"equity ${float(st.get('equity') or 0):,.2f}")
            halted = (st.get("guard") or {}).get("halted")
            lines.append(f"{name}: {money}, {'-' if pnl < 0 else '+'}${abs(pnl):,.2f} on {trades} trades"
                         + (" HALTED" if halted else ""))
        trouble = [m for m, _, _ in problems.values()]
        lines.append("All running normally." if not trouble else f"{len(trouble)} problem(s): " + "; ".join(trouble)[:300])
        return "\n".join(lines)[:1500]

    def run(self):
        while True:
            t0 = time.time()
            try:
                self.step(t0)
            except Exception as e:                    # the watchdog must outlive its own mistakes
                print(f"watchdog check failed ({type(e).__name__}: {e}); carrying on", flush=True)
            time.sleep(max(1.0, self.every - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser(description="Watch the Makawan bots and message your phone when something breaks")
    ap.add_argument("--bots", default=DEFAULT_BOTS, help="name=port list (default: the four run_both.py bots)")
    ap.add_argument("--every", type=float, default=30.0, help="seconds between checks (default 30)")
    ap.add_argument("--status-every", type=float, default=60.0,
                    help="minutes between status messages to your phone (default 60; 0 = off)")
    ap.add_argument("--ntfy", default=None, help="phone channel name (or use --setup once, which saves one)")
    ap.add_argument("--ntfy-server", default="https://ntfy.sh")
    ap.add_argument("--no-mac-notify", action="store_true", help="no pop-up notifications on this Mac")
    ap.add_argument("--report-at", default="07:00", help="time of the daily report ('off' = never)")
    ap.add_argument("--tune-on", default="sun", help="day of the weekly tuner run: mon..sun ('off' = never)")
    ap.add_argument("--tune-at", default="06:30")
    ap.add_argument("--tune-days", type=float, default=14, help="days of price history the weekly tuner studies")
    ap.add_argument("--setup", action="store_true", help="make a private phone channel, save it, and send a test")
    ap.add_argument("--set-heartbeat", default=None, metavar="URL",
                    help="save a heartbeat address (e.g. from healthchecks.io); 'off' removes it")
    ap.add_argument("--test", action="store_true", help="send a test message and stop")
    ap.add_argument("--once", action="store_true", help="check once, print the result and stop")
    ap.add_argument("--quiet", default="00:00-09:00",
                    help="no phone messages between these times (Eastern unless --tz); what happens then is sent as "
                         "one summary when they end. 'off' = message any time (default 00:00-09:00)")
    ap.add_argument("--tz", default="America/New_York")
    a = ap.parse_args()
    tb.set_display_tz(a.tz)
    settings_path = os.path.join(DIR, "settings.json")
    settings = load_json(settings_path)

    if a.set_heartbeat is not None:
        if a.set_heartbeat.lower() in ("off", "none"):
            settings.pop("heartbeat", None)
            print("Heartbeat removed.")
        elif not a.set_heartbeat.startswith("https://"):
            sys.exit("The heartbeat address must start with https://")
        else:
            settings["heartbeat"] = a.set_heartbeat
            print("Heartbeat saved. The watchdog will ping it about once a minute while the bots run.")
        save_json(settings_path, settings)
        return
    if a.setup:
        if not settings.get("ntfy_topic"):
            settings["ntfy_topic"] = "makawan-" + secrets.token_urlsafe(12).replace("_", "x").replace("-", "y")
            save_json(settings_path, settings)
        t = settings["ntfy_topic"]
        print(f"""
Your private phone channel is:   {t}

On your phone:
  1. Install the free "ntfy" app (App Store or Google Play).
  2. Open it, tap +  (Subscribe to topic), type the channel name above exactly, and subscribe.
  3. Allow notifications when it asks.

Keep the channel name to yourself: anyone who knows it can read the messages.
It is saved in {settings_path} on this Mac, so you only do this once.
Sending a test message now...""")

    topic = a.ntfy or os.environ.get("MAKAWAN_NTFY_TOPIC") or settings.get("ntfy_topic", "")
    heartbeat = os.environ.get("MAKAWAN_HEARTBEAT_URL") or settings.get("heartbeat", "")
    n = Notifier(topic, a.ntfy_server, mac=not a.no_mac_notify, log_path=os.path.join(DIR, "watchdog.log"),
                 quiet=None if (a.setup or a.test) else parse_quiet(a.quiet), tz=a.tz)
    if a.setup or a.test:
        n.send("Makawan: test", "This is a test from the Makawan watchdog. Phone messages are working.")
        if not topic:
            print("No phone channel is set up yet, so this only appeared on the Mac. Run:  "
                  "python3 makawan_watchdog.py --setup")
        elif n.queue:
            print("The message could NOT be sent to ntfy (no internet, or ntfy is blocked on this network).")
        else:
            print("Sent. If nothing arrives on your phone within a minute, check the channel name in the app.")
        return

    bots = parse_bots(a.bots)
    if a.once:
        tr = Tracker(confirm=1)
        probs = {}
        for name, port in bots:
            st = fetch_state(port)
            if st is None and name.endswith("?"):
                continue
            name = name.rstrip("?").strip()
            probs.update(check_bot(name, st, time.time()))
            if st and st.get("ready"):
                word = "HALTED" if (st.get("guard") or {}).get("halted") else "running"
                print(f"  {name:11s} {word:8s} equity ${st.get('equity', 0):,.2f}   books: {st.get('books', '-')}")
        for msg, _ in tr.update(probs, time.time())[0]:
            print("  PROBLEM  " + msg)
        print("All good." if not probs else f"{len(probs)} problem(s).")
        sys.exit(1 if probs else 0)

    try:
        report_at, tune_at = parse_hhmm(a.report_at), parse_hhmm(a.tune_at)
    except ValueError as e:
        sys.exit(str(e))
    jobs = {}
    if report_at:
        jobs["daily report"] = {"at": report_at, "weekday": None, "timeout": 600,
                                "cmd": [sys.executable, os.path.join(HERE, "makawan_report.py"), "--tz", a.tz]}
    if tune_at and a.tune_on.lower() not in ("off", "none", "no"):
        if a.tune_on.lower()[:3] not in DAYS:
            sys.exit("--tune-on must be one of mon tue wed thu fri sat sun, or off")
        jobs["weekly tuner"] = {"at": tune_at, "weekday": DAYS.index(a.tune_on.lower()[:3]), "timeout": 3600,
                                "cmd": [sys.executable, os.path.join(HERE, "makawan_tune.py"), "backtest", "--propose",
                                        "--days", str(a.tune_days), "--pace", "0.5"]}
    sched = Schedule(os.path.join(DIR, "state.json"), jobs, time.time()) if jobs else None
    print("Watchdog on: " + ", ".join(f"{nm.rstrip('?')} ({p}{', once it is started' if nm.endswith('?') else ''})"
                                      for nm, p in bots))
    print("  phone messages: " + ("on" if topic else "OFF. Run  python3 makawan_watchdog.py --setup  to turn them on"))
    print("  status message: " + (f"every {a.status_every:g} min" if a.status_every > 0 else "off"))
    print("  sleep/off alarm (heartbeat): " + ("on" if heartbeat else "off (see --set-heartbeat in the guide)"))
    if jobs:
        print("  schedule: " + "; ".join(
            f"{k} {'every ' + DAYS[j['weekday']].title() if j['weekday'] is not None else 'every day'} "
            f"{j['at'][0]:02d}:{j['at'][1]:02d}" for k, j in jobs.items()), flush=True)
    try:
        Watchdog(bots, n, sched, a.every, heartbeat, status_every=max(0.0, a.status_every) * 60).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
