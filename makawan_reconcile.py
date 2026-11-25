#!/usr/bin/env python3
"""
makawan_reconcile.py - compare the bot's own record with what really happened at Polymarket (READ-ONLY, live only)
=================================================================================================================
    python3 makawan_reconcile.py                          # list your real fills and balance for the last 24 hours
    python3 makawan_reconcile.py --out makawan_out_live   # compare that bot folder's record with the real fills
    python3 makawan_reconcile.py --hours 72               # look further back

For each market it lines up what the bot wrote down (shares and price) against Polymarket's own record of your
fills, and reports:
  * slippage: you really paid more (or less) per share than the bot recorded
  * missed fills: the bot thinks it traded but Polymarket has no fill
  * unknown fills: Polymarket has a fill the bot never recorded
  * balance: the bot's idea of the account against the real balance
The result is saved as reconcile.csv in the bot's folder, one row per market.

This only means something for a bot that sends REAL orders. Every bot folder you have today is paper, so pointed
at one of them it will (correctly) report that none of the bot's trades exist at Polymarket.

It signs in with your key from POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY (set in the Terminal window, never in a
file) and only READS: balance, positions and activity. It never places, changes or cancels an order.

Not yet checked against the real service: it was built from Polymarket's published library and tested against
stand-in data. Polymarket's fill record does not say which side a fill was on, so prices are compared both ways
round (price and 100c minus price) and the closer one is used. Check its first real run by eye against the
Polymarket website.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone

import makawan_bot as tb

REQUIRES = "2026.11.13"
if getattr(tb, "__version__", None) != REQUIRES:
    sys.exit("{} and makawan_bot.py come from different downloads. Put ALL the files from the latest download in one "
             "folder.".format(os.path.basename(__file__)))

COLS = ["checked_at_et", "slug", "window_end_et", "side", "bot_shares", "bot_price", "real_shares", "real_price",
        "price_diff_cents", "slippage_dollars", "real_fills", "real_taker_fills", "status"]
PRICE_TOL, QTY_TOL = 0.005, 0.01            # half a cent a share; 1% of the size


def amount(x):
    """Polymarket money values come as {"value": "0.55", "currency": "USD"}, plain text or numbers."""
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# the two records
# --------------------------------------------------------------------------
def bot_record(folder, t0, t1):
    """{slug: {...}} from the bot's events.csv (BUY rows) between t0 and t1, plus its all-time net and bankroll."""
    out = {}
    path = os.path.join(folder, "events.csv")
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for e in csv.DictReader(f):
                t = tb.parse_ts(e.get("time_utc"))
                if e.get("event") != "BUY" or t is None or not (t0 <= t <= t1):
                    continue
                sh, px = amount(e.get("shares")), amount(e.get("price"))
                if not sh or px is None:
                    continue
                d = out.setdefault(e["slug"], {"side": e.get("side", ""), "shares": 0.0, "cost": 0.0, "at": t})
                d["shares"] += sh
                d["cost"] += sh * px
    for d in out.values():
        d["price"] = d["cost"] / d["shares"]
    net = 0.0
    wpath = os.path.join(folder, "windows.csv")
    if os.path.exists(wpath):
        with open(wpath, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("traded_side"):
                    net += amount(r.get("net_pnl") if r.get("net_pnl", "") != "" else r.get("pnl")) or 0.0
    state = tb.read_state(folder)
    return out, {"bankroll": state.get("bankroll"), "net": net, "live": bool(state.get("live"))}


def fetch_account(client, t0, prefix="cpc-btc-updown", max_pages=50):
    """Read-only pull of balance, open positions and trade activity back to t0. Returns a plain dict."""
    bal = client.account.balances()
    rows = bal.get("balances", []) if isinstance(bal, dict) else []
    usd = next((b for b in rows if b.get("currency", "USD") == "USD"), rows[0] if rows else {})
    pos = client.portfolio.positions()
    positions = pos.get("positions", {}) if isinstance(pos, dict) else {}
    fills, moved, cursor = [], 0.0, None
    for _ in range(max_pages):
        params = {"limit": 100, "sortOrder": "SORT_ORDER_DESCENDING"}
        if cursor:
            params["cursor"] = cursor
        page = client.portfolio.activities(params)
        acts = page.get("activities", []) if isinstance(page, dict) else []
        oldest = None
        for a in acts:
            tr = a.get("trade")
            if a.get("type") == "ACTIVITY_TYPE_TRADE" and tr:
                t = tb.parse_ts(tr.get("createTime") or tr.get("updateTime"))
                oldest = t if oldest is None or (t is not None and t < oldest) else oldest
                slug = tr.get("marketSlug", "")
                qty = amount(tr.get("qtyDecimal")) or amount(tr.get("qty"))
                px = amount(tr.get("price"))
                if t is None or t < t0 or qty is None or px is None or (prefix and not slug.startswith(prefix)):
                    continue
                fills.append({"slug": slug, "t": t, "qty": abs(qty), "px": px, "taker": bool(tr.get("isAggressor")),
                              "state": tr.get("state", "")})
            elif a.get("type", "").startswith("ACTIVITY_TYPE_ACCOUNT") and a.get("accountBalanceChange"):
                for x in a["accountBalanceChange"].get("transactions", []):
                    t = tb.parse_ts(x.get("createTime") or x.get("updateTime"))
                    if t is not None and t >= t0:
                        sign = -1.0 if "WITHDRAWAL" in a["type"] else 1.0
                        moved += sign * abs(amount(x.get("amount")) or 0.0)
        cursor = page.get("nextCursor") if isinstance(page, dict) else None
        if not acts or page.get("eof") or not cursor or (oldest is not None and oldest < t0):
            break
    return {"balance": usd.get("currentBalance"), "buying_power": usd.get("buyingPower"),
            "open_orders": usd.get("openOrders"), "positions": positions, "fills": fills, "moved": moved}


# --------------------------------------------------------------------------
# lining them up
# --------------------------------------------------------------------------
def reconcile(bot, fills):
    """bot: {slug: {side, shares, price}}; fills: [{slug, qty, px, taker}]. Returns one row (dict) per market."""
    real = {}
    for f in fills:
        d = real.setdefault(f["slug"], {"qty": 0.0, "cost": 0.0, "n": 0, "taker": 0})
        d["qty"] += f["qty"]
        d["cost"] += f["qty"] * f["px"]
        d["n"] += 1
        d["taker"] += int(f["taker"])
    rows = []
    for slug in sorted(set(bot) | set(real)):
        b, r = bot.get(slug), real.get(slug)
        row = {"slug": slug, "side": b["side"] if b else "", "bot_shares": round(b["shares"], 2) if b else "",
               "bot_price": round(b["price"], 4) if b else "", "real_shares": "", "real_price": "",
               "price_diff_cents": "", "slippage_dollars": "", "real_fills": r["n"] if r else 0,
               "real_taker_fills": r["taker"] if r else 0}
        if r:
            rpx = r["cost"] / r["qty"]
            if b and abs((1 - rpx) - b["price"]) < abs(rpx - b["price"]):
                rpx = 1 - rpx                      # the fill is quoted from the other side of the market
            row["real_shares"], row["real_price"] = round(r["qty"], 2), round(rpx, 4)
        if b and r:
            diff = rpx - b["price"]
            row["price_diff_cents"] = round(diff * 100, 2)
            row["slippage_dollars"] = round(diff * r["qty"], 2)
            size_ok = abs(r["qty"] - b["shares"]) <= QTY_TOL * max(b["shares"], 1.0)
            if not size_ok:
                row["status"] = "size differs (partly filled?)" if r["qty"] < b["shares"] else "size differs"
            elif abs(diff) > PRICE_TOL:
                row["status"] = "paid more than recorded" if diff > 0 else "paid less than recorded"
            else:
                row["status"] = "matched"
        elif b:
            row["status"] = "MISSED: the bot recorded a trade, Polymarket has no fill"
        else:
            row["status"] = "UNKNOWN FILL: Polymarket has a fill the bot did not record"
        rows.append(row)
    return rows


def summarize(rows, acct, info):
    both = [r for r in rows if r["bot_shares"] != "" and r["real_shares"] != ""]
    missed = [r for r in rows if r["status"].startswith("MISSED")]
    unknown = [r for r in rows if r["status"].startswith("UNKNOWN")]
    lines = [f"{len(rows)} market(s): {len(both)} in both records, {len(missed)} missed, {len(unknown)} unknown."]
    if both:
        slip = sum(r["slippage_dollars"] for r in both)
        shares = sum(r["real_shares"] for r in both)
        lines.append(f"Slippage on matched trades: ${slip:+,.2f} in all, {slip / shares * 100:+.2f}c a share "
                     f"(+ means you really paid more than the bot recorded).")
    if acct.get("balance") is not None:
        line = f"Polymarket balance ${acct['balance']:,.2f} (buying power ${acct.get('buying_power') or 0:,.2f})"
        if info and info.get("bankroll") is not None:
            want = info["bankroll"] + info["net"]
            line += (f"; the bot's record says ${want:,.2f}; difference ${acct['balance'] - want:+,.2f}"
                     + (f" (deposits/withdrawals in this period: ${acct['moved']:+,.2f})" if acct.get("moved") else ""))
        lines.append(line + ".")
    if acct.get("positions"):
        lines.append(f"Open positions at Polymarket right now: {len(acct['positions'])}.")
    return lines


def window_end(slug):
    """Close time of a BTC Up/Down market from its name (the name carries the window's start, in UTC)."""
    m = re.match(r"cpc-\w+-updown-(\w+)-(\d{4}-\d\d-\d\d)-(\d{4})z", slug or "")
    if not m or m.group(1) not in tb.HORIZON_SECONDS:
        return None
    start = datetime.strptime(m.group(2) + m.group(3), "%Y-%m-%d%H%M").replace(tzinfo=timezone.utc).timestamp()
    return start + tb.HORIZON_SECONDS[m.group(1)]


def save(folder, rows, now):
    """Merge this run into <folder>/reconcile.csv (one row per market, newest check wins)."""
    path = os.path.join(folder, "reconcile.csv")
    old = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            old = {r["slug"]: r for r in csv.DictReader(f)}
    for r in rows:
        we = window_end(r["slug"])
        old[r["slug"]] = dict(r, checked_at_et=tb.local_iso(now), window_end_et=tb.local_iso(we) if we else "")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for slug in sorted(old):
            w.writerow({c: old[slug].get(c, "") for c in COLS})
    return path


def main():
    ap = argparse.ArgumentParser(description="Compare a bot's record with your real Polymarket fills (read-only)")
    ap.add_argument("--out", default=None, help="the bot folder to compare (omit to just list your real fills)")
    ap.add_argument("--hours", type=float, default=24.0, help="how far back to look (default 24)")
    ap.add_argument("--all-markets", action="store_true", help="include fills in markets other than BTC Up/Down")
    ap.add_argument("--tz", default="America/New_York")
    a = ap.parse_args()
    tb.set_display_tz(a.tz)
    key_id = os.environ.get("POLYMARKET_KEY_ID", "").strip()
    secret = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    if not key_id or not secret:
        sys.exit("Your key is not set. In this Terminal window run:\n"
                 '    export POLYMARKET_KEY_ID="your key id"\n'
                 '    export POLYMARKET_SECRET_KEY="your secret key"\n'
                 "then run this again. Never paste the key into a chat or a code file.")
    try:
        from polymarket_us import PolymarketUS, PolymarketUSError
    except ImportError:
        sys.exit("The Polymarket US library is missing. Run:  pip3 install polymarket-us   (needs Python 3.10 or newer)")
    now = time.time()
    t0 = now - a.hours * 3600
    bot, info = ({}, None)
    if a.out:
        if not os.path.isdir(a.out):
            sys.exit(f"Folder not found: {a.out}")
        bot, info = bot_record(a.out, t0, now)
        if not info["live"]:
            print(f"NOTE: {a.out} is a PAPER record (no real orders were sent from it), so its trades are not expected "
                  f"to exist at Polymarket. This comparison only means something for a live bot.\n")
    print(f"Reading your Polymarket account (read-only), last {a.hours:g} hours...")
    client = PolymarketUS(key_id=key_id, secret_key=secret)
    try:
        acct = fetch_account(client, t0, prefix="" if a.all_markets else "cpc-btc-updown")
    except (PolymarketUSError, ValueError) as e:
        sys.exit(f"Could not read the account: {type(e).__name__}: {e}")
    finally:
        try:
            client.close()
        except Exception:
            pass
    rows = reconcile(bot, acct["fills"])
    if rows:
        print(f"\n{'market':44s} {'side':5s} {'bot':>16s} {'real':>16s} {'diff':>8s}  status")
        for r in rows:
            b = f"{r['bot_shares']} @ {r['bot_price'] * 100:.1f}c" if r["bot_shares"] != "" else "-"
            x = f"{r['real_shares']} @ {r['real_price'] * 100:.1f}c" if r["real_shares"] != "" else "-"
            d = f"{r['price_diff_cents']:+.2f}c" if r["price_diff_cents"] != "" else ""
            print(f"{r['slug'][:44]:44s} {r['side']:5s} {b:>16s} {x:>16s} {d:>8s}  {r['status']}")
    else:
        print("\nNo fills at Polymarket and no bot trades in this period.")
    print("")
    lines = summarize(rows, acct, info)
    for ln in lines:
        print(ln)
    if a.out and rows:
        print(f"Saved: {save(a.out, rows, now)}")
    for ln in lines[:2]:
        print("SUMMARY: " + ln)
    print("\nNothing was bought, sold or changed.")


if __name__ == "__main__":
    main()
