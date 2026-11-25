#!/usr/bin/env python3
"""
makawan_connect_check.py - check that your Polymarket US API key works (READ-ONLY)
===================================================================================
It signs in with your key and reads your balance, open positions and open orders.
It never places, changes or cancels an order.

One-time setup:
    pip3 install polymarket-us

Your key is read from two environment variables, never from this file:
    export POLYMARKET_KEY_ID="..."         # the Key ID
    export POLYMARKET_SECRET_KEY="..."     # the Secret Key (shown once when you create the key)

Then:
    python3 makawan_connect_check.py
"""
import os
import sys


def main():
    key_id = os.environ.get("POLYMARKET_KEY_ID", "").strip()
    secret = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
    if not key_id or not secret:
        sys.exit("Your key is not set. In this Terminal window run:\n"
                 '    export POLYMARKET_KEY_ID="your key id"\n'
                 '    export POLYMARKET_SECRET_KEY="your secret key"\n'
                 "then run this script again. Never paste the key into a chat or a code file.")
    try:
        from polymarket_us import PolymarketUS
        from polymarket_us import APIConnectionError, AuthenticationError, PermissionDeniedError, PolymarketUSError
    except ImportError:
        sys.exit("The Polymarket US library is missing. Run:  pip3 install polymarket-us   (needs Python 3.10 or newer)")

    print(f"Key ID ends in ...{key_id[-4:]} (secret not shown). Connecting to Polymarket US, read-only...")
    client = PolymarketUS(key_id=key_id, secret_key=secret)
    try:
        bal = client.account.balances()
        rows = bal.get("balances", []) if isinstance(bal, dict) else []
        print("\nConnected. Your key works.\n\nBalances:")
        if not rows:
            print("  (none returned)")
        for b in rows:
            print(f"  {b.get('currency', '?')}: balance {b.get('currentBalance')}   buying power {b.get('buyingPower')}   "
                  f"tied up in open orders {b.get('openOrders')}")
        try:
            pos = client.portfolio.positions()
            n = len(pos.get("positions", pos) if isinstance(pos, dict) else pos)
            print(f"\nOpen positions: {n}")
        except PolymarketUSError as e:
            print(f"\nCould not read positions: {e}")
        try:
            od = client.orders.list()
            n = len(od.get("orders", od) if isinstance(od, dict) else od)
            print(f"Open orders: {n}")
        except PolymarketUSError as e:
            print(f"Could not read open orders: {e}")
        print("\nNothing was bought, sold or changed.")
    except AuthenticationError:
        sys.exit("\nPolymarket rejected the key (not authorised). Check both values were copied in full, that the key "
                 "has not been revoked, and that your Mac's clock is correct.")
    except PermissionDeniedError as e:
        sys.exit(f"\nThe key signed in but is not allowed to do this: {e}")
    except APIConnectionError as e:
        sys.exit(f"\nCould not reach Polymarket US: {e}")
    except ValueError as e:
        sys.exit(f"\nThe secret key does not look right (it should be the long text ending in '=='): {e}")
    except PolymarketUSError as e:
        sys.exit(f"\nPolymarket returned an error: {e}")
    finally:
        try:
            client.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
