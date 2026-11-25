#!/usr/bin/env python3
"""
makawan_live.py - run ONE strategy with REAL money on Polymarket US
====================================================================
Do these in order. Each step is started by you, in Terminal, with your own key.

    export POLYMARKET_KEY_ID="..."            # a NEW key; never one that has been pasted into a chat
    export POLYMARKET_SECRET_KEY="..."

    python3 makawan_live.py --rehearse        # 1. sends NOTHING. Checks key and balance; asks Polymarket to
                                              #    preview each order it would have sent
    python3 makawan_live.py --max-trades 1    # 2. ONE real trade, then it halts. Check it on polymarket.us
    python3 makawan_reconcile.py --out makawan_out_live     # 3. does Polymarket's record match the bot's?
    python3 makawan_live.py --resume          # 4. real trading

Defaults: the "any time" strategy, $100 bankroll, $5 a trade, stop for good at $50 (-50%).
    --strategy late     trade only in the last 2 min (5m) / 5 min (15m)
    --bankroll 100 --stake 5 --stop-loss 0.5 --daily-loss 15

How an order is sent: a LIMIT order that fills at once or is cancelled (nothing is left resting), priced at the
ask the bot saw plus 1 cent, and never above the highest price that still has an edge. If it is not filled, the
window is skipped (--retries N would try again N more times, 3 seconds apart, re-checking everything each time).
The size is chosen so the whole cost, fee included, cannot exceed the stake.

It stops itself (trading halts; tracking continues) when: the stop-loss is reached; an order's result cannot be
confirmed; a fill comes back worse than its limit; Polymarket rejects three orders in a row; or the bot was
stopped in the middle of an order. A halt survives restarts until you add --resume.

Every request and answer is written to <folder>/live_orders.jsonl (no key in it).

NOT YET PROVEN: this was built from Polymarket's published library and documents and tested against a stand-in
exchange. Until your first real order, nobody has seen it trade. That is what steps 1 to 3 are for.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

import makawan_bot as tb

REQUIRES = "2026.11.13"
if getattr(tb, "__version__", None) != REQUIRES:
    sys.exit("{} and makawan_bot.py come from different downloads. Put ALL the files from the latest download in one "
             "folder.".format(os.path.basename(__file__)))

DONE = ("ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED",
        "ORDER_STATE_REPLACED")
FILL_TYPES = ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL")
END_TYPES = ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_CANCELED", "EXECUTION_TYPE_REJECTED", "EXECUTION_TYPE_EXPIRED")


def amount(x):
    if isinstance(x, dict):
        x = x.get("value")
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def price_text(px):
    return f"{px:.4f}".rstrip("0").rstrip(".")


def order_request(slug, side, limit, qty, tick=0.01, block_s=2):
    """The order as Polymarket wants it. UP is the market's long ("Yes") side. Polymarket's price field is ALWAYS
    the long side's price, so buying DOWN at 30c is sent as BUY_SHORT with price 0.70."""
    long_px = limit if side == "UP" else 1.0 - limit
    long_px = round(round(long_px / tick) * tick, 4)
    if not (0.01 <= long_px <= 0.99):
        raise ValueError(f"price {long_px} is outside 0.01-0.99")
    return {"marketSlug": slug,
            "intent": "ORDER_INTENT_BUY_LONG" if side == "UP" else "ORDER_INTENT_BUY_SHORT",
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": price_text(long_px), "currency": "USD"},
            "quantity": int(qty) if abs(qty - round(qty)) < 1e-9 else round(qty, 2),
            "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "maxBlockTime": str(int(block_s))}


def rest_request(slug, side, limit, qty, tick, expire_ts):
    """A resting limit order that Polymarket cancels by itself at `expire_ts` (good till date)."""
    req = order_request(slug, side, limit, qty, tick)
    req.pop("synchronousExecution", None)
    req.pop("maxBlockTime", None)
    req["tif"] = "TIME_IN_FORCE_GOOD_TILL_DATE"
    req["goodTillTime"] = tb.iso(int(expire_ts))
    return req


def sell_request(slug, side, floor_px, qty, tick=0.01):
    """Sell what is held. Polymarket's price is always UP's: selling DOWN for at least 30c = SELL_SHORT at 0.70."""
    req = order_request(slug, side, floor_px, qty, tick)
    req["intent"] = "ORDER_INTENT_SELL_LONG" if side == "UP" else "ORDER_INTENT_SELL_SHORT"
    long_px = floor_px if side == "UP" else 1.0 - floor_px
    long_px = (math.floor if side == "UP" else math.ceil)(round(long_px / tick, 6)) * tick   # never worse than the floor
    req["price"] = {"value": price_text(round(long_px, 4)), "currency": "USD"}
    return req


def side_price(px, side, limit):
    """Polymarket reports fill prices for the long ("Yes" = UP) side, also on BUY_SHORT orders: confirmed on all 11
    real DOWN fills of Nov 10-11, 2026 (each long price was at or above the long limit, and commissionSpreadPx
    carried 1 - price). So the price paid for DOWN is always 1 - px."""
    if px is None or side == "UP":
        return px
    return round(1.0 - px, 6)

def read_result(resp, side, limit):
    """Turn Polymarket's answer (create-order response, or a looked-up order) into
    {id, filled, price, fee, status, final}. `final` is False when the order may still be open."""
    order = resp.get("order") if isinstance(resp.get("order"), dict) else None
    execs = resp.get("executions") or []
    oid = resp.get("id") or (order or {}).get("id") or ""
    filled = cost = fee = 0.0
    state, final, why, refused = (order or {}).get("state", ""), False, "", False
    for e in execs:
        o = e.get("order") or {}
        state = o.get("state") or state
        order = o or order
        if e.get("type") in FILL_TYPES:
            sh, px = amount(e.get("lastShares")), amount(e.get("lastPx"))
            if sh and px is not None:
                filled += sh
                cost += sh * px
            fee += amount(e.get("commissionNotionalCollected")) or 0.0
        if e.get("type") in END_TYPES:                # FILL means completely filled; the others end the order too
            final = True
        refused = refused or e.get("type") == "EXECUTION_TYPE_REJECTED"
        why = e.get("orderRejectReason") or e.get("text") or why
    if state in DONE:
        final = True
    refused = refused or state == "ORDER_STATE_REJECTED"
    px = cost / filled if filled > 0 else None
    if order:                                         # the order's own totals win when they are there
        cum, avg = amount(order.get("cumQuantity")), amount(order.get("avgPx"))
        if cum and avg and cum > filled + 1e-6:       # fills are missing from the answer: fall back on the order's
            filled, px = cum, avg                     # totals (its average price is rounded to a cent)
        fee = amount(order.get("commissionNotionalTotalCollected")) or fee
    if refused and not state:
        state = "ORDER_STATE_REJECTED"
    words = {"ORDER_STATE_FILLED": "filled", "ORDER_STATE_CANCELED": "not filled at that price (cancelled)",
             "ORDER_STATE_REJECTED": "rejected", "ORDER_STATE_EXPIRED": "expired"}.get(state, state or "no answer yet")
    if filled > 0 and state != "ORDER_STATE_FILLED":
        words = "partly filled, rest cancelled"
    return {"id": oid, "filled": filled, "price": side_price(px, side, limit), "fee": fee if fee > 0 else None,
            "status": words + (f" ({why})" if why and filled <= 0 else ""), "final": final,
            "rejected": refused}


class LiveBroker:
    """Sends real orders through Polymarket's own library. One method: buy()."""

    def __init__(self, client, out_dir, rehearse=False, sleep=time.sleep):
        self.client, self.out_dir, self.rehearse, self.sleep = client, out_dir, rehearse, sleep
        self.path = os.path.join(out_dir, "live_orders.jsonl")

    def note(self, kind, **data):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(dict({"time": tb.iso(time.time()), "kind": kind}, **data), default=str) + "\n")
        except OSError:
            pass

    def rest(self, slug, side, limit, qty, tick, expire_ts):
        """Leave a real limit order on the book until `expire_ts`. Returns {"id"} or {"id": "", "status"}."""
        req = rest_request(slug, side, limit, qty, tick, expire_ts)
        try:
            from polymarket_us import APIStatusError
        except ImportError:
            class APIStatusError(Exception):
                pass
        self.note("rest-request", side=side, limit=limit, request=req)
        try:
            resp = self.client.orders.create(req)
        except APIStatusError as e:
            code = getattr(e, "status_code", 0)
            self.note("rest-refused", code=code, error=str(e)[:300])
            if code and 400 <= code < 500:
                return {"id": "", "status": f"refused by Polymarket ({code}: {str(e)[:120]})"}
            raise
        self.note("rest-answer", answer=resp)
        return {"id": (resp or {}).get("id", "")}

    def balance(self):
        """Read-only: the account's cash balance and buying power."""
        bal = self.client.account.balances()
        rows = bal.get("balances", []) if isinstance(bal, dict) else []
        usd = next((b for b in rows if b.get("currency", "USD") == "USD"), rows[0] if rows else {})
        return {"balance": amount(usd.get("currentBalance")), "buying_power": amount(usd.get("buyingPower")),
                "open_orders": amount(usd.get("openOrders"))}

    def check(self, order_id, side, limit):
        looked = self.client.orders.retrieve(order_id)
        self.note("rest-lookup", answer=looked)
        return read_result(dict(looked or {}, id=order_id), side, limit)

    def sell(self, slug, side, floor_px, qty, tick=0.01):
        """Sell a position now: fill-now-or-cancel, never below `floor_px` for the side held."""
        req = sell_request(slug, side, floor_px, qty, tick)
        try:
            from polymarket_us import APIStatusError
        except ImportError:
            class APIStatusError(Exception):
                pass
        self.note("sell-request", side=side, floor=floor_px, request=req)
        try:
            resp = self.client.orders.create(req)
        except APIStatusError as e:
            code = getattr(e, "status_code", 0)
            self.note("sell-refused", code=code, error=str(e)[:300])
            if code and 400 <= code < 500:
                return {"id": "", "filled": 0.0, "price": None, "final": True, "rejected": True,
                        "status": f"refused by Polymarket ({code}: {str(e)[:120]})"}
            raise
        self.note("sell-answer", answer=resp)
        res = read_result(resp or {}, side, floor_px)
        tries = 0
        while not res["final"] and res["id"] and tries < 3:
            self.sleep(0.5)
            tries += 1
            try:
                res = read_result(dict(self.client.orders.retrieve(res["id"]) or {}, id=res["id"]), side, floor_px)
            except Exception as e:
                self.note("sell-lookup-failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        if not res["final"]:
            try:
                self.client.orders.cancel(res["id"], {"marketSlug": slug})
                res = read_result(dict(self.client.orders.retrieve(res["id"]) or {}, id=res["id"]), side, floor_px)
            except Exception:
                pass
        if not res["final"]:
            res["status"] = "unknown"
        return res

    def sell_preview(self, slug, side, floor_px, qty, tick=0.01):
        req = sell_request(slug, side, floor_px, qty, tick)
        self.note("rehearsal-sell", request=req)
        return self.client.orders.preview({"request": req})

    def cancel(self, order_id, slug):
        self.note("rest-cancel", id=order_id)
        try:
            self.client.orders.cancel(order_id, {"marketSlug": slug})
        except Exception as e:
            self.note("rest-cancel-failed", id=order_id, error=f"{type(e).__name__}: {str(e)[:300]}")
            raise
        self.note("rest-cancel-accepted", id=order_id)

    def buy(self, slug, side, limit, qty, tick=0.01):
        req = order_request(slug, side, limit, qty, tick)
        if self.rehearse:                             # ask Polymarket what it WOULD do. Nothing is sent
            self.note("rehearsal-request", side=side, limit=limit, request=req)
            ans = self.client.orders.preview({"request": req})
            self.note("rehearsal-answer", answer=ans)
            return {"status": "rehearsal", "filled": 0.0, "price": None, "final": True}
        try:
            from polymarket_us import APIStatusError
        except ImportError:                           # only in tests without the library
            class APIStatusError(Exception):
                pass
        self.note("request", side=side, limit=limit, request=req)
        try:
            resp = self.client.orders.create(req)
        except APIStatusError as e:
            code = getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", 0)
            self.note("refused", code=code, error=str(e)[:300])
            if code and 400 <= code < 500:            # Polymarket said no: the order does not exist
                return {"id": "", "filled": 0.0, "price": None, "final": True, "rejected": True,
                        "status": f"refused by Polymarket ({code}: {str(e)[:120]})"}
            raise                                     # 5xx: it may or may not exist -> the engine halts
        self.note("answer", answer=resp)
        res = read_result(resp or {}, side, limit)
        tries = 0
        while not res["final"] and res["id"] and tries < 5:      # no final word yet: look the order up
            self.sleep(0.5)
            tries += 1
            try:
                looked = self.client.orders.retrieve(res["id"])
                self.note("lookup", answer=looked)
                res = read_result(dict(looked or {}, id=res["id"]), side, limit)
            except Exception as e:
                self.note("lookup-failed", error=f"{type(e).__name__}: {str(e)[:200]}")
            if not res["final"] and tries == 3:       # still open after 1.5 s: it must not rest. Cancel it
                try:
                    self.client.orders.cancel(res["id"], {"marketSlug": slug})
                    self.note("cancel-sent", id=res["id"])
                except Exception as e:
                    self.note("cancel-failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        if not res["final"]:
            res["status"] = "unknown"
        return res


def money(x):
    return "?" if x is None else f"${float(x):,.2f}"


def main():
    ap = argparse.ArgumentParser(description="Run one Makawan strategy with REAL money (you start it, with your key)")
    ap.add_argument("--strategy", choices=("any", "late"), default="any",
                    help="any = trade at any time in the window (default); late = last 2 min of 5m / 5 min of 15m")
    ap.add_argument("--bankroll", type=float, default=100.0, help="money this bot may use (default 100)")
    ap.add_argument("--stake", type=float, default=5.0, help="dollars per trade, fee included (default 5)")
    ap.add_argument("--stop-loss", type=float, default=0.5, help="halt for good after losing this share (default 0.5)")
    ap.add_argument("--daily-loss", type=float, default=None, help="pause until tomorrow after losing this much in a day")
    ap.add_argument("--max-trades", type=int, default=0, help="halt after this many real trades (use 1 the first time)")
    ap.add_argument("--cut", action="store_true",
                    help="sell a LOSING position when buyers pay more for it (after the fee) than the maths says it "
                         "is worth; winning positions are held to settlement")
    ap.add_argument("--hybrid", action="store_true",
                    help="if the fill-now order misses, leave a limit order at the same price until the call changes, "
                         "the edge goes, or 20 s before the window closes")
    ap.add_argument("--rest-max", type=float, default=5.0,
                    help="with --hybrid: cancel a resting order that has not filled within this many seconds (default 5)")
    ap.add_argument("--retries", type=int, default=0,
                    help="after an order is not filled, try again up to this many times in the same window (default 0: "
                         "skip the window)")
    ap.add_argument("--rehearse", action="store_true", help="send nothing: check the key and preview the orders")
    ap.add_argument("--resume", action="store_true", help="clear a halt and trade again")
    ap.add_argument("--fresh", action="store_true", help="archive this folder's history and start a new record")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--out", default=None, help="log folder (default makawan_out_live)")
    ap.add_argument("--params", default=None)
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--spot", choices=("off", "watch", "use"), default="watch",
                    help="Coinbase live price: watch = log only (default); use = also use it in the model")
    ap.add_argument("--allow-sleep", action="store_true")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--tz", default="America/New_York")
    ap.add_argument("--test-exchange", default=None, help=argparse.SUPPRESS)   # a stand-in on this computer, for testing
    a = ap.parse_args()
    if a.test_exchange and not a.test_exchange.startswith(("http://127.0.0.1:", "http://localhost:")):
        sys.exit("--test-exchange only accepts a stand-in running on this computer.")
    out = a.out or ("makawan_out_rehearsal" if a.rehearse else "makawan_out_live")
    if a.stake <= 0 or a.bankroll <= 0 or a.stake > a.bankroll * 0.2:
        sys.exit("--stake must be more than 0 and no more than a fifth of --bankroll.")
    if not (0 < a.stop_loss < 1):
        sys.exit("--stop-loss must be between 0 and 1. A live bot always runs with a stop-loss.")

    key_id = os.environ.get("POLYMARKET_KEY_ID", "").strip()
    secret = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    if not key_id or not secret:
        sys.exit("Your key is not set. In this Terminal window run:\n"
                 '    export POLYMARKET_KEY_ID="your key id"\n'
                 '    export POLYMARKET_SECRET_KEY="your secret key"\n'
                 "then run this again. Use a NEW key, and never paste it into a chat or a code file.")
    try:
        from polymarket_us import PolymarketUS, PolymarketUSError
    except ImportError:
        sys.exit("The Polymarket US library is missing. Run:  pip3 install polymarket-us   (needs Python 3.10 or newer)")
    extra = {"api_base_url": a.test_exchange, "gateway_base_url": a.test_exchange} if a.test_exchange else {}
    try:
        client = PolymarketUS(key_id=key_id, secret_key=secret, timeout=8.0, max_retries=0, **extra)
    except TypeError:
        client = PolymarketUS(key_id=key_id, secret_key=secret, **extra)
    try:
        bal = client.account.balances()
        rows = bal.get("balances", []) if isinstance(bal, dict) else []
        usd = next((b for b in rows if b.get("currency", "USD") == "USD"), rows[0] if rows else {})
        open_orders = client.orders.list()
        n_orders = len(open_orders.get("orders", []) if isinstance(open_orders, dict) else [])
    except (PolymarketUSError, ValueError) as e:
        sys.exit(f"Could not sign in to Polymarket with this key: {type(e).__name__}: {e}")
    balance, power = amount(usd.get("currentBalance")), amount(usd.get("buyingPower"))
    floor = a.bankroll * (1 - a.stop_loss)
    when = "at any time in the window" if a.strategy == "any" else "only in the last 2 min (5m) / 5 min (15m)"
    print(f"""
Key ending ...{key_id[-4:]} works. Polymarket balance {money(balance)}, buying power {money(power)}, open orders {n_orders}.

{'REHEARSAL: nothing will be sent.' if a.rehearse else 'THIS WILL SEND REAL ORDERS WITH REAL MONEY.'}
  strategy     {a.strategy}: 5m and 15m BTC Up/Down, buys {when}; 1h is tracked, not traded
  per trade    up to ${a.stake:g}, fee included; one position per window; """ + (
              "losing positions SOLD EARLY when buyers pay more than the maths says (--cut); winners held" if a.cut else "held to settlement") + f"""
  orders       taker: fill now or cancel; """ + (
              f"if not filled, a limit order rests at that price (HYBRID) for up to {a.rest_max:g}s, cancelled "
              f"sooner if the call changes or the edge goes" if a.hybrid else
              f"if not filled, up to {a.retries} more tries, 3s apart" if a.retries else
              "if not filled, the window is skipped") + f"""
  bankroll     ${a.bankroll:g}; trading halts for good at ${floor:g} (-{a.stop_loss:.0%})"""
          + (f"\n  daily limit  pause for the day after losing ${a.daily_loss:g}" if a.daily_loss else "")
          + (f"\n  first run    halts after {a.max_trades} real trade(s)" if a.max_trades else "")
          + f"\n  record       {out}/   page http://localhost:{a.port}\n")
    if not a.rehearse:
        if power is None or power < a.stake:
            sys.exit(f"Buying power is {money(power)}, less than one ${a.stake:g} trade. Nothing was sent.")
        if balance is not None and balance < a.bankroll - 0.005:
            print(f"NOTE: the account holds {money(balance)}, less than the ${a.bankroll:g} bankroll. The stop-loss is "
                  f"counted from ${a.bankroll:g}, so it would halt at ${floor:g} on the bot's books, not at half of what "
                  f"is really there. Consider --bankroll {int(balance)}.")
        if balance is not None and balance > a.bankroll * 1.5:
            print(f"NOTE: the account holds {money(balance)}. The bot only limits ITS OWN losses to "
                  f"${a.bankroll - floor:g}; the rest of the balance is not protected by anything but that.")
        print("Paper results do not promise real results. Real fills are usually worse than paper fills.")
        try:
            said = input('Type LIVE (capitals) and press Enter to start, or anything else to stop: ')
        except (EOFError, KeyboardInterrupt):
            said = ""
        if said.strip() != "LIVE":
            sys.exit("Stopped. Nothing was sent.")

    def make_broker(cfg, args):
        tb.write_state(cfg.out_dir, live=not a.rehearse, strategy=a.strategy, stake=a.stake)
        return LiveBroker(client, cfg.out_dir, rehearse=a.rehearse)

    argv = ["--port", str(a.port), "--out", out, "--bankroll", str(a.bankroll), "--stake", str(a.stake),
            "--stop-loss", str(a.stop_loss), "--exec", "taker", "--tz", a.tz]
    if a.test_exchange:                               # stand-in exchange => made-up market data too
        argv += ["--simulate", "--speed", "300", "--no-ticks"]
    if a.strategy == "any":
        argv += ["--trade-last", "none"]
    if a.daily_loss is not None:
        argv += ["--daily-loss", str(a.daily_loss)]
    params = a.params or os.path.join("makawan_tune", "params.json")
    if params != "none" and os.path.exists(params):
        argv += ["--params", params]
    argv += ["--spot", "off" if a.test_exchange else a.spot]
    for flag in ("resume", "fresh", "stream", "allow_sleep", "open", "cut"):
        if getattr(a, flag):
            argv.append("--" + flag.replace("_", "-"))
    import makawan_web as tw
    try:
        tw.main(argv, live=make_broker, overrides={"live_max_trades": a.max_trades, "live_retries": max(0, a.retries),
                                                     "min_trade_usd": min(1.0, a.stake * 0.5),
                                                     "live_rest": a.hybrid, "live_rest_max_s": max(1.0, a.rest_max)})
    finally:
        try:
            client.close()
        except Exception:
            pass
        print("\nStopped. Open positions (if any) are held to settlement by Polymarket; nothing is left resting.")


if __name__ == "__main__":
    main()
