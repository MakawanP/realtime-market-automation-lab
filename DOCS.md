# Makawan: BTC tracker for Polymarket US (5m / 15m / 1h)

Makawan is a tracker and **paper-trading** bot built from the "How Terry thinks" video. It reads Polymarket US's public
market-data endpoints (no API key), works out a chance that BTC finishes above the "price to beat", posts
UP/DOWN calls, and records every call and paper trade against the exchange's own settlement.
The paper bots never place a real order. Only `makawan_live.py` does, and only when you start it and type LIVE.

## Run it

    pip install requests rich tzdata
    python makawan_bot.py --selftest     # reproduces the video's numbers (14 checks)
    python makawan_bot.py --discover     # RUN THIS FIRST on your machine: confirms the live windows are found
    python makawan_bot.py                # live dashboard in the terminal
    python makawan_web.py --open         # live dashboard as a local website: http://localhost:8765
    python makawan_web.py --simulate     # same website with a fake market, no network, 30x speed
    pytest -q test_makawan.py          # 121 tests

## The local website (`makawan_web.py`)
Runs the same engine and serves a page at `http://localhost:8765` (only this computer can open it; the server binds
to 127.0.0.1). It shows, per window: countdown, price to beat, BTC vs the line, the chance with a gauge and a chart
with the 57/53/50/47/43% lines from the video, model vs market, bid/ask, the value check and your paper position.
Below: scorecard, recent settled windows (with the position, e.g. `DOWN 67 @ 73Â¢`, and its P&L), a calls/trades log, and CSV downloads. It follows your light/dark setting.
Options: `--port 9000`, `--open`, `--tz America/Chicago`, plus the same `--horizons --bankroll --no-trade` flags.

## Time zone
Everything on screen is US Eastern (EST/EDT handled automatically; change with `--tz`). The CSVs keep UTC and add
`time_et` / `window_start_et` / `window_end_et` columns. Polymarket's own slugs (`...-1900z`) are UTC.
On Windows, `pip install tzdata` is needed for time zones; without it the bot falls back to UTC and says so.

Useful flags: `--horizons 15m,1h`  `--bankroll 5000`  `--no-trade` (calls only)  `--plain` (log lines)  `--w-market 0.5`
Logs go to `makawan_out/`: `events.csv` (every call, release, buy, settle) and `windows.csv` (one row per window).

## What the video does -> where it is in the code

| Video step | Code |
|---|---|
| Watch BRTI, price to beat, clock, "how jumpy", order book | `Engine._refresh_prices`, `estimate_sigma_per_sec`, `Api.book` |
| Numbers -> chance | `chance_up` / `settle_stats` (settlement is the 60-second average, so variance is sigma^2 (t - 40) early on; this is what reproduces 55 / 78 / 98 / 90%) |
| Blend with the market | `blend` - plain average, (90% + 66%) / 2 = 78% |
| Call at 57 / 43, keep at 53 / 47, first call waits | `next_call`, `Config.warmup_frac` |
| Ask/bid, depth, spread, fee | `Book`, `walk`, `fee_per_share` (0.095 x p x (1-p); see "Fees") |
| Right isn't enough: good up to 67c at 70% | `ev_per_share`, `max_buy_price` (needs 1c of edge after fee) |
| Quarter Kelly: $18.81 / $72.64 / $76.71 | `kelly_dollars` |
| (Sell early if the bid beats holding: **removed**, every position is held to settlement) | - |
| Tested, then changed: no sides under 25c on the 5m desk | `Config.min_buy_px` |

## Things I added that are not in the video
`confirm_s` (a call change must hold 3 s), `min_secs_left` (no new entry in the last 5 s), `max_trade_frac`
(5% of equity cap per trade), one entry per window. All are in `Config`.

## Simulating a week (`simulate_week.py`)
    python simulate_week.py                 # 7 days against a fake market, about 3.5 minutes
    python simulate_week.py --efficient     # control: the fake market is priced perfectly
    python simulate_week.py --seed 2 --params makawan_out/params.json --out sim_runs/b

**This does not tell you whether the strategy makes money on Polymarket.** The fake market's prices are the true chance
plus a little noise, so any profit there is built in by that assumption. My own runs: two noisy-market weeks (seeds 1 and
2) ended at +65% and +216% with drawdowns of $532 and $941, while the same strategy against a perfectly priced market
ended at -45% in a week (fees and the spread cost about 60 cents per trade there). The real question is how far real
Polymarket prices sit from the true chance: you need mispricing bigger than the spread plus fee (about 2-3c a share).
The way to find out is to run the bot live on paper for a week or two, then read `makawan_out/windows.csv`.

## Reading the percentages (which side is it about?)
Inside the bot every chance is stored as the chance of **UP**. The dashboard now shows the chance of **the side you are on**:
the side you hold, else the current call, else whichever side is more likely, and it says which ("DOWN, holding"). So a
DOWN position shows a **high** number, and a low UP chance (such as the model saying 0% UP) means DOWN is almost certain.
The small line under the big number always spells out both: `UP 3% / DOWN 98%`. The gauge and chart are always in UP
terms (left = DOWN more likely, right/top = UP more likely), and the bid/ask shown is the side you are on.
The log lines say "CALL DOWN: 63% chance of DOWN". In the CSV files, `chance`, `model` and `market` (events.csv, ticks.csv)
are always the chance of UP, while `first_call_chance` (windows.csv) is the chance of the side that was called.

## Comparing "late-only" trading with "any time" trading
    python simulate_week.py --seed 1                          # late-only: last 2 min of a 5m, last 5 min of a 15m (default)
    python simulate_week.py --seed 1 --trade-last none --out sim_any   # trades at any time in every window
    python simulate_week.py --seed 1 --efficient              # add --efficient for the perfectly priced control

`summary.json` splits each run's trades into "late" (inside the late-only window) and "early" ones. One trade per window,
so an early trade uses up that window's slot and the late/early groups are not the same set of windows. To compare on
**real** prices, run two bots side by side on the same live data, each with its own folder and port:

    python3 run_both.py --open          # ONE command starts both; Ctrl+C stops both
        late-only: http://localhost:8765   any time: http://localhost:8766   (logs: makawan_out_late/ and makawan_out_any/)

Or use two Terminal windows (each bot keeps its window busy, so they cannot share one line):

    python3 makawan_web.py --port 8765 --out makawan_out_late                       # window 1
    python3 makawan_web.py --port 8766 --trade-last none --out makawan_out_any      # window 2 (new tab: Cmd+T)

Each page's "Model:" line ends with its trade rule ("trades: 5m last 2:00, 15m last 5:00, 1h any time" or "... any time"),
so you can tell the two pages apart.

## Strategy lab: paper-trade popular strategies side by side (`makawan_lab.py`, added Nov 13, 2026)

    python3 makawan_lab.py --open              # leaderboard at http://localhost:8780
    python3 makawan_lab.py --only cut,trend    # just some of them
    python3 makawan_lab.py --fresh             # archive the lab's history and start over

One program, one stream of market data, eight strategies, each with its own paper account ($1,000, $20 a trade)
and its own record in `makawan_out_lab_<name>/`. **Paper only**: it never uses your Polymarket key and never places an order.
Run it **instead of** `run_both.py` (it includes today's rules as "baseline"; both together would ask Polymarket
for data too often). It starts the watchdog too, so the 7:00 daily report covers every strategy.

| Name | Idea | What the evidence says going in |
|---|---|---|
| `baseline` | Fair value: buy when Makawan's chance beats the price by more than the fee (today's "any" rules) | The yardstick |
| `cut` | Baseline + sell a losing position when buyers pay more than the maths says it is worth | Beat holding on every Nov 10 log |
| `cautious` | Weigh the market 65% (not 50%), never pay over 85c, cut losers | Live trades showed the model is overconfident |
| `trend` | Momentum: 30% of Bitcoin's last-10-minute move carries on | Some intraday momentum in studies; unstable |
| `fade` | Mean reversion: 30% of the last minute's move reverses | Small minute-scale reversal in studies |
| `maker` | Baseline signal, but bid and wait (no fee) instead of paying the ask | Makers are the consistent winners in studies; our own maker tests lost |
| `sniper` | Last 60 seconds: buy the side already priced 90-97c | Heavily promoted; large data sets show it losing |
| `terry` | Follow Terry Calls' **first call** on 5m, 15m and 1h (no locks): buy within 15 s of it posting, at most 2c over his price, hold to the result | Terry's follower record (Nov 2026): 5m about +2.5c a contract, 15m flat, 1h positive on few calls |

**`terry` (added Nov 14, 2026)** needs your Terry Calls key: put `TERRY_API_KEY=...` in a `.env` file next to
`makawan_lab.py`. It reads only that key. It is the
only strategy that trades 1h, and its page shows Terry's first call instead of Makawan's.

How often it asks Terry: every second while a first call could come on a market that has none yet (5m: from 1:30 in
until 0:20 left; 15m: from 5:00 in until 1:00 left; 1h: from 15:00 in until 2:00 left), every 15 s otherwise. On the free plan
(20,000 requests a day, reset 00:00 UTC) that would run out by mid-day, so `--terry-budget 19000` (the default)
stretches the fast checks just enough to last the day: about every 4 s, so calls are seen about 2.5 s after
they post. With the Unlimited plan run `--terry-budget 0` for true 1-second checks (about 70,000 requests a
day; calls seen within about 1 s). **Two or more free keys:** add each on its own line in the same `.env`,
`TERRY_API_KEY=...` then `TERRY_API_KEY_2=...` (and `_3`, ...). The lab takes turns with them, so two keys
allow 38,000 requests a day and calls are seen about 1.6 s after they post (2.3 s at worst, 9 times in 10).
`--terry-budget` is per key. `--terry-skip-1h` stops trading and watching 1h calls (only about 13% fewer
requests). Avoid running two processes simultaneously against the same API key. Run just these three:

    python3 makawan_lab.py --only baseline,fade,terry --open

Every strategy shares the same stake, the 1h-off rule, the 20-point gap skip and one trade per window, so two
rows differ only in the idea. The settings were fixed before any results came in; do not tune them to the
leaderboard, or the comparison stops meaning anything.

**Honest paper fills.** Lab orders behave like live ones: a limit at the ask it saw plus 1c (never above the edge),
which fills only if the price is still there one second later (`--delay`), for whatever is on offer at or under
the limit. A miss skips the window. The leaderboard's "Orders filled" shows how many would really have traded.
The fee is the measured 0.095 x p x (1 - p). The old paper bots (`run_both.py`) still fill instantly and so
flatter themselves; the lab does not. (Paper sells in `cut` and resting bids in `maker` are still modelled, not
real, so treat those two rows with extra caution.)

**Judging it.** Use "Per $1 staked" and net P&L, not the hit rate. A strategy needs a few hundred trades before
its row means much, and about 2,000 for a 2-cent edge to stand clear of luck. Write the pass mark down first, e.g.
"at least 300 trades, at least +3c per $1 staked, and not 'could be luck'". Most strategies should be expected to
fail: studies put 67-84% of Polymarket accounts in the red, worst in sub-hour markets.

## Selling early when the market pays more than the maths (`--cut`, added Nov 10, 2026)

Off unless you add `--cut`. With it, a **losing** position is sold before the end when what buyers will pay for
your side, minus the fee, is worth more than Makawan's own chance of winning, by at least 1c a share. "Losing" means
that selling now would get back less than was paid (fee included). A winning position is always held to
settlement. On Nov 10's logs this beat both holding everything and cutting winners too: live -$20.78 held, -$3.82
cutting everything, +$5.66 cutting only losers; paper "any" +$248 / +$299 / +$378 (assuming sales at the logged
bid, so real results will be somewhat worse). Example: holding DOWN,
the maths says DOWN 60%; if buyers pay 45c, nothing happens; if buyers pay 75c (72c after the fee), it sells.
The same for UP. A turned call on its own does not sell (that rule exists, `cut_on_flip` in a settings file, but
is off: on Nov 10's logs it lost more than it saved).

The sale is fill-now-or-cancel, no lower than the bid it saw minus 1c, never in the last 10 seconds, at most 3
tries per window. It pays the taker fee again. The record shows `CUT` (sent), `SELL` (filled, with the profit or
loss taken) and an `exit` of `cut` or `part cut`. A failed or unconfirmed sell halts trading.

## Coinbase live price (`--spot`, added Nov 10, 2026)

The official index (BRTI) reaches the bot 0.6-0.9 s old and only once a second. Coinbase's public WebSocket
(`wss://advanced-trade-ws.coinbase.com`, BTC-USD ticker, no account or key) arrives within milliseconds, and
Coinbase is one of the exchanges BRTI is built from. The bot brings the index up to date like this:

    estimate now = last index print + (Coinbase now - Coinbase at the moment of that print)

so the few-dollar gap between Coinbase's price and the index doesn't matter, only Coinbase's movement. It is
ignored when Coinbase is more than 3 s old, or says Bitcoin moved more than 0.4% since the last print. The price to
beat and the settlement are always the index.

- `--spot watch` (default, every bot): the model is worked out both ways, with and without Coinbase; trading uses
  the index alone, and `ticks.csv` logs both (`coinbase`, `brti_now_est`, `p_model_index`, `p_model_spot`).
- `--spot use`: trading uses the Coinbase-adjusted model.
- `--spot off`: no Coinbase connection. Simulations never use it.

The daily report has a section "Coinbase vs index alone" that scores both models against what really happened,
by time left. Give it a few days before switching with `--spot use`: each window gives many readings, so a
day's difference is easy to over-read. Not yet checked against Coinbase itself (tested against a stand-in): the
page's "Coinbase price" box shows whether it is connected. The +1 s matching of an index print to Coinbase is my
best reading of how the index is stamped; the logged columns let it be checked later.

## Live trading with real money (`makawan_live.py`)

Everything else in this folder is paper. This one file sends real orders. **You** start it, in Terminal, with your
own key; the key is read from the two environment variables and is never stored.

Do the steps in order:

    export POLYMARKET_KEY_ID="..."            # a NEW key (revoke any key that was ever pasted into a chat)
    export POLYMARKET_SECRET_KEY="..."

    python3 makawan_live.py --rehearse        # 1. sends nothing; Polymarket previews each order it would send
    python3 makawan_live.py --max-trades 1    # 2. ONE real trade, then it halts
    python3 makawan_reconcile.py --out makawan_out_live     # 3. does Polymarket's record match the bot's?
    python3 makawan_live.py --resume          # 4. real trading

Before a real start it shows your balance and the plan and waits for you to type `LIVE`.

Defaults: `--strategy any` (the port-8766 rules: 5m and 15m, any time in the window, skip when the model is 20+
points above the market, 1h tracked only), `--bankroll 100 --stake 5 --stop-loss 0.5`. Page: http://localhost:8770,
record in `makawan_out_live/`. `--strategy late` uses the late-only rules instead.

How an order goes out: a **limit** order that fills at once or is cancelled (nothing is ever left resting), priced
at the ask the bot saw plus 1 cent and never above the highest price that still has an edge. If it is not filled,
the window is skipped. Optional: `--retries 3` tries again up to 3 more times in that window, 3 seconds apart. Each retry re-checks the
call, the fresh price, the edge after the fee, the 20-point gap rule and the stop-loss, so it never pays more than
the first try would have allowed. A fill (even a partial one) ends the tries: one position per window.
The share count is rounded down so the whole cost including the fee cannot exceed the stake. Positions are held
to settlement, as in the paper bots. Polymarket's price field is always the UP ("Yes") side's price, so buying
DOWN at 30c is sent as `BUY_SHORT` at 0.70.

**Hybrid (`--hybrid`, off by default).** If the fill-now order misses, the bot leaves a real limit order at the
same price and size. It is looked up every second and any fills (even partial) are booked as they happen. It is
cancelled if it has not filled within 5 seconds (`--rest-max`), when the call changes or is released, when the edge at its price is gone, 20 seconds before the window
closes, or when trading halts; it also carries a good-till-date expiry at that same moment, so Polymarket removes
it by itself if the bot or the Mac stops. A fill that slips in before a cancel lands is still booked. Money in a
resting order counts against the stop-loss as if it will fill and lose. If the bot is restarted while an order was
resting, it halts so you can check the account first. Warning: the paper maker bots, which do the same thing on
paper, lost money while the taker bots made money (Nov 9-10), because a waiting order mostly fills when the price
moves against it.

It halts by itself (and stays halted across restarts until `--resume`) when:
the stop-loss is reached; an order's result cannot be confirmed; a fill comes back worse than its limit;
Polymarket rejects three orders in a row; the bot was stopped in the middle of an order; or the `--max-trades`
count is reached. Every request and answer is saved in `makawan_out_live/live_orders.jsonl` (no key in it).

The watchdog watches the live bot once it has been started, the daily report lists it as `live (REAL)`, and the
reconciler is the check that the bot's record matches Polymarket's.

**What is and is not proven.** The order path was tested end to end against a stand-in exchange on my side:
signed requests, the order format, fills, partial fills, no fill, a slow answer, a stuck order, rejections, a
dropped connection, restarts. It has **never** talked to the real Polymarket order system; your rehearsal and
first single trade are the first real test. Paper results came from a model of fills, and real fills are usually
worse. The bot limits its own losses to the bankroll minus the floor; nothing else protects the rest of the
account balance.

## The five helpers ("agents") added on Nov 10, 2026

| Helper | File | What it does | Runs |
|---|---|---|---|
| Risk guard | inside `makawan_bot.py` | Enforces the stake and the stop-loss; halts trading when the stop is reached | Always, inside every bot |
| Watchdog | `makawan_watchdog.py` | Checks the bots are alive, the price feed is fresh and the Mac has not slept; messages your phone | Started by `run_both.py` |
| Daily report | `makawan_report.py` | P&L by bot, where money was made and lost, anything odd | 07:00 each day (by the watchdog), or by hand |
| Reconciler | `makawan_reconcile.py` | Compares a bot's record with your real Polymarket fills and balance (read-only) | By hand, live only |
| Tuner | `makawan_tune.py backtest --propose` | Re-runs the backtest and PROPOSES settings; you decide | Sunday 06:30 (by the watchdog), or by hand |

None of them places, changes or cancels an order.

### 1. Risk guard
Two limits, checked before every trade and on every tick:

- **Stop-loss** (`--stop-loss 0.5`, the default): once the account has lost 50% of its starting bankroll, trading
  **halts for good**. It judges on the worst case: a trade is refused if losing it, together with every trade and
  bid still open, would take the account under the floor, so the floor is never crossed. With
  `--bankroll 100 --stake 5` the floor is $50 and the last trade allowed is at $55. In four two-day live-size
  simulations (all three window lengths trading at once) the cash never went under $50.
- **Daily loss limit** (`--daily-loss 15`, off by default): after losing that many dollars in one day (your time
  zone), trading pauses until the next day.

When halted the bot keeps tracking and settling, shows a red banner on its page, writes a `HALT` row in `events.csv`
and remembers the halt in `state.json`, so **restarting does not clear it**. Only `--resume` does (or `--fresh`,
which starts a new record). If the account is still at the stop after `--resume`, it halts again at once.

    python3 run_both.py --bankroll 100 --stake 5 --stop-loss 0.5        # your planned live-size limits, on paper
    python3 run_both.py --resume                                        # clear a halt on every bot

The paper bots keep their $1,000 / $20 defaults unless you pass these; the 50% stop now applies to them too
(floor $500). `--stop-loss 0` switches it off.

### 2. Watchdog and phone messages
`run_both.py` starts it automatically (`--no-watchdog` to skip). One-time phone setup:

    python3 makawan_watchdog.py --setup      # makes a private channel name, shows phone steps, sends a test
    python3 makawan_watchdog.py --test       # send another test
    python3 makawan_watchdog.py --once       # check the bots once and print what it sees

It uses **ntfy** (ntfy.sh), a free push service: install the ntfy app, subscribe to the channel name `--setup`
prints. Messages (bot names, problems, P&L headline) pass through ntfy's servers and anyone who knows the channel
name can read them, which is why the name is long and random. No API key is ever sent. Without setup it still shows
notifications on the Mac and writes `makawan_watchdog/watchdog.log`.

It messages when a problem **starts**, when it **clears**, and hourly while it lasts:
a bot not answering, a bot stuck, the price feed stale, trading halted or paused, a bot 80% of the way to its stop,
the live stream switched off, and "this Mac was asleep from X to Y" as soon as it wakes.

**If the Mac sleeps or loses power it cannot send anything.** To be told about that, create a free check at a
"dead man's switch" service such as healthchecks.io (set it to expect a ping every minute, alert after 5), then:

    python3 makawan_watchdog.py --set-heartbeat https://hc-ping.com/your-check-id

The watchdog pings it once a minute while at least one bot answers; when the pings stop, that service alerts you.

### 3. Daily report

    python3 makawan_report.py --open         # last 24 hours, opens the page
    python3 makawan_report.py --hours 72     # or --all

Saved in `makawan_reports/` as `.txt` and `.html`. Sections: P&L by bot with a plain "is it luck?" reading; where
each bot made and lost money (by window length, price paid, time left, and how far the model was above the
market); biggest wins and losses; how much of the time the bots were really running (from `ticks.csv`); and
anything odd: a trade over the stake limit, bought with no market price, a long shot under 5c, bought seconds
after waking, a halt, a losing streak. Run on the night of Nov 9-10 it singles out the 3:00 AM trade and the
sleep gaps by itself.

### 4. Reconciler (live only)

    export POLYMARKET_KEY_ID="..."; export POLYMARKET_SECRET_KEY="..."
    python3 makawan_reconcile.py                          # your real fills and balance, last 24 hours
    python3 makawan_reconcile.py --out <live bot folder>  # compare that bot's record with the real fills

Per market: bot shares and price against real shares and price, the difference in cents (slippage), and a status:
matched, paid more/less than recorded, MISSED (bot recorded a trade, Polymarket has none), UNKNOWN FILL. Saved as
`reconcile.csv` in the bot's folder. It only reads balance, positions and activity.
**Every folder you have now is paper**, so there is nothing real to match yet; it becomes useful when a live mode
exists. It was built from Polymarket's published library and tested against stand-in data only. Polymarket's fill
record does not name the side, so the price is compared both ways round. Check its first real run by eye.

### 5. Tuner on a schedule (proposes, never applies)

    python3 makawan_tune.py backtest --propose --days 14   # what the watchdog runs each Sunday
    python3 makawan_tune.py apply                          # YOUR decision: start using the proposal
    python3 makawan_tune.py revert                         # go back to the built-in settings

`--propose` writes `makawan_tune/params_proposed.json` and a plain-words `tune_proposal.txt`, and changes nothing.
After `apply`, `run_both.py` picks up `makawan_tune/params.json` on its next start and says so
(`--params none` ignores it). The backtest scores UP/DOWN accuracy on past index prices only; it does not use
market prices, so a better score is not proof of more profit, and changing settings mid-test makes before/after
harder to compare.

## How these markets really settle (found on Nov 10, 2026)
Polymarket US's BTC Up/Down windows settle on **the last one-second BRTI print before the close**, not on a
60-second average. In the logs, 98 of 98 closes where the bot sampled the index one second before the close matched
the exchange's settlement price to the cent, and every window's price to beat equals the previous window's
settlement. The model now uses that rule (`avg_window_s = 1`). The old 60-second rule made the model far too sure of
itself in the last two minutes, and sometimes pointed the wrong way in the last minute. `--preset video` keeps the
60-second average, because the video's example numbers assume it. The simulator and `makawan_tune.py backtest` use the
same closing-print rule.

Fixes made at the same time:
* **Gap rule with a one-sided book.** With no bid there is no mid price, and the rule was skipped. It now compares
  the model with the price you would pay (the ask).
* **The stake is a hard cap.** An order that walks up several price levels is shrunk until its total cost fits.
* **Maker bids keep their dollar size** when they are re-priced (they kept their share count before).
* **Sleep.** `run_both.py`, `makawan_web.py` and `makawan_bot.py` now keep a Mac awake while they run, using macOS's own
  `caffeinate` (nothing is changed permanently; closing the lid still sleeps it; `--allow-sleep` turns this off). If
  the clock jumps anyway, the page says how long the computer was asleep and the bot waits 20 s before new calls.

## Trading rules added on Nov 9, 2026 (from the first day of paper results)
All four bots now use three extra rules. They came from replaying one day of paper trades, so they are a test, not a
proven improvement:
1. **No trades on the 1h market.** It is still tracked and scored. Change with `--no-trade-on none` (or `5m,1h`).
2. **Skip a trade when the model is more than 20 points above the market's price** for the side being bought (for
   example model 90%, market 64%). Change with `--max-gap 0.15`; `--max-gap 1` turns it off.
3. **Flat $20 per trade, fee included**, instead of quarter-Kelly sizing. Change with `--stake 40`; `--stake 0`
   brings back quarter-Kelly.

The page's "Model:" line shows the rules in force. `--preset video` turns all three off. The same settings can go in
`params.json` under `"defaults"`: `flat_stake`, `max_model_gap`, `no_trade_horizons`. A folder that already has history
keeps it, so its scorecard mixes trades made under the old and new rules; start with `--fresh` for a clean record.

## Fees, maker vs taker, gross vs net
The bot is **always a taker**: it buys at the ask straight away, so every fill pays Polymarket US's taker fee,
`0.095 x price x (1 - price)` per share (about 1.5c a share at 70-85c, 2.3c at 50c). The rate was 0.0695 until
the first real trade on Nov 10, 2026 was charged $0.26 on 11.44 shares at 41.7c, which works out to 0.092-0.096.
That is one trade, so the rate is provisional; paper results before this change understate fees by about a third. A maker (a resting limit order that someone
else fills) pays no fee at maker tier 1, but may never get filled. `windows.csv` now has, for each traded window:
`liquidity` (taker/maker), `gross_pnl` (payout - shares x price), `fee`, and `net_pnl` (= gross - fee; `pnl` is the
same number, kept for older tools). The scorecard and Recent windows show Gross, Fees and Net. Older sheets are
upgraded on the next start (all past fills were taker fills); the original is kept as `windows.csv.bak`.
The paper fee is computed on the average fill price of the whole order; the exchange rounds each fill to the cent,
so a real fee can differ by a cent or two.

## Paper maker test (`--exec maker`)
Each strategy now also runs as a **maker**: instead of buying at the ask, it posts a limit bid (one tick above the
best bid if that is still below the ask, never above what its edge allows, never crossing the ask), re-prices it each
second, and cancels it if the call changes or flips, or the window gets within 5 seconds of closing. A maker pays no
fee (maker tier 1: no fee, no rebate). A paper bid at price P counts as filled when:
1. the best ask on its side drops to P or lower (filled up to the size offered at or below P), or
2. the book shows new volume since the last poll and the last trade printed below P (or at P when the bid was alone
   at a new best price, i.e. first in the queue), filled up to the new volume.

Only the last trade between two one-second polls is visible, so rule 2 is approximate, and queue position is a guess.
**Maker mode is live paper only.** The simulator has no trades, so a simulated bid could only fill when the price moves
against it; that test would be unfair, so `--simulate --exec maker` is refused, `simulate_week.py` is taker only, and
`run_both.py --simulate` starts only the two taker bots.

`run_both.py` (live) starts four bots (each keeps its own folder):

| Bot | Page | Folder |
|---|---|---|
| late-only, taker | http://localhost:8765 | `makawan_out_late/` |
| any time, taker | http://localhost:8766 | `makawan_out_any/` |
| late-only, maker | http://localhost:8767 | `makawan_out_late_maker/` |
| any time, maker | http://localhost:8768 | `makawan_out_any_maker/` |

`run_both.py --no-maker` runs only the two taker bots. The maker pages show "Bids filled" (fills / bids posted) in the
scorecard, and unfilled bids in Recent windows. `windows.csv` gains `order_status` (filled / partial / unfilled) and
`order_px`; `events.csv` gains ORDER and CANCEL rows, and maker fills are BUY rows whose note starts with "maker".
Four bots make about 16 requests a second together; Polymarket US's limit is 25 per second per IP address.

## Blank fields: empty order books
When a card shows "no quotes" / "nobody quoting", the order book for that window was empty at that moment: nobody was
offering to buy or sell, so there is no market price, nothing to trade, and the "Chance" is the model alone. It is
common on Polymarket US. In the first day of logs, 5m books were empty about 44% of the time (about 1 in 4 windows had
no book at all, and the last minute was emptiest), 15m about 37% and 1h about 13%. A one-sided book shows the missing
side as "none" (for example `61c / none`).

## Live order-book stream (`--stream`)
By default the bot polls each order book once a second, and each poll takes about 0.4 s, so the prices it acts on are
up to a second or two old. With `--stream` it listens to Polymarket US's markets WebSocket instead, which pushes every
order-book change as it happens. It is read-only: it cannot place, change or cancel orders.

    export POLYMARKET_KEY_ID="..."          # the stream needs your API key (set in this Terminal window)
    export POLYMARKET_SECRET_KEY="..."
    pip3 install polymarket-us
    python3 run_both.py --stream --open     # or: python3 makawan_web.py --stream

* The page shows "order books: live (1,234 updates)" when it is working, or why it is polling instead.
* **Fallback:** if the key is missing, the connection drops, or nothing arrives for 15 s, the bot polls as before and
  reconnects in the background.
* **Safety check:** every 15 s per market it also polls the book and compares. Five disagreements in a row switch the
  stream off for that run and show a warning.
* The Bitcoin index (BRTI) is still polled once a second; Polymarket's stream does not carry it.
* Each bot opens its own connection, so `run_both.py --stream` opens four with one key.
* Not available with `--simulate`.

## Restarting keeps your history
Stopping the bot (Ctrl+C) and starting it again **carries on where it left off**. On start-up it reads the folder's
`windows.csv` and `events.csv` and rebuilds the scorecard, recent windows, the calls-and-trades log and your paper cash.
A position that was still open when you stopped is carried over and settled normally, even if its window closed while
the bot was off. The page shows a note such as "Picked up from makawan_out/: 120 settled windows, paper P&L +$45.10".

* Each folder is its own record: `makawan_out/` (live), `makawan_out_sim/` (`--simulate`), `makawan_out_late/` and
  `makawan_out_any/` (`run_both.py`; `_sim` added in simulation). Use `--out myfolder` for a separate record.
* The starting bankroll is remembered in the folder's `state.json`; `--bankroll 5000` changes it.
* `--fresh` moves the old files into `archive-<date-time>/` inside the folder and starts from scratch (nothing is
  deleted). `run_both.py --fresh` does it for both bots. `simulate_week.py` always starts clean this way.
* Files from older versions with different columns are converted in place; the original is kept as `*.csv.bak`.
* A restarted `--simulate` run continues its fake clock after the last run instead of replaying the same windows.

## When it trades, and no early selling
Calls and the scorecard run all window long, but **trades only open late in the window**: in the last **2 minutes of a 5m
window** and the last **5 minutes of a 15m window**. The 1h window has no limit unless you set one. The dashboard shows
"trading opens at 2:00 left" until then, and a bar on each chart marks the trade zone. **There is no early selling:** every
paper position is held to settlement, so the result of a trade is exactly what the exchange settles (a win pays $1 a
share, a loss pays nothing). One trade per window at most.

    python makawan_bot.py --trade-last 5m=90,15m=240,1h=600   # change the trade windows (seconds left); 0 = any time
    {"defaults": {"trade_last_s": {"5m": 120, "15m": 300}}}  # same thing in params.json

## When the first call comes
A fresh window's "price to beat" is the average of the last 60 seconds, so right at the open BTC is usually a little
off the line and the chance leaves 50% quickly, then falls back. On simulated data, with no waiting, about 7 in 10
five-minute windows got a call in their first 15 seconds and about 6 in 10 of those later flipped. So each window now
waits **max(45 seconds, 10% of the window)** before its first call: 45 s on 5m, 90 s on 15m, 6 min on 1h. A window the bot
joins part-way through also waits 20 s after you start it. The dashboard shows "first call waits (31s)" while it counts down.

    python makawan_bot.py --warmup-s 90                  # one wait for every window length
    {"5m": {"warmup_min_s": 90}, "1h": {"warmup_min_s": 900}}   # per window length, in params.json
    python makawan_bot.py --preset video                 # the video's 1/15 of the window, no join wait

`makawan_tune.py backtest` prints a "when the first call comes" table for each window length (how often a first call
at each point in the window was right, and how often it later flipped), so you can pick the wait from your own data.

## Not limited to the video's numbers
The video's 57/53/47/43% call lines, 50/50 blend and plain bell curve were examples. They are now just the
`--preset video` setting. Everything is a setting you can change per window length, and a tuning tool measures them on data:

| What | Default (`pro`) | How it gets better |
|---|---|---|
| Volatility ("how jumpy") | 50% weight on a recent-volatility estimate (5-minute memory), 50% on the 30-minute estimate | `makawan_tune.py backtest` picks the weight per window length |
| Fat tails | 10% of moves are "wild" (twice the size), keeping the same overall volatility | picked by the backtest |
| Volatility scale | 1.0 | picked by the backtest |
| Blend with the market | simple average, 50% market | `makawan_tune.py fit-blend` learns the log-odds weights from your own logs |
| Call lines, warm-up, confirm, min edge, Kelly fraction | 57/53/47/43%, 1/15, 3 s, 1c, 25% | edit by hand per window length in `params.json` |

    python makawan_tune.py backtest --days 14   # replays past windows on real BRTI history (public data, no market prices)
    python makawan_tune.py fit-blend            # after the bot has logged a while: how much to trust the market
    python makawan_bot.py --params makawan_out/params.json

`backtest` chooses settings on the first 70% of the history and judges them on the last 30%, and only saves a window
length's settings if they beat the video model by more than twice the standard error. `fit-blend` needs at least 300
settled windows per window length (about 1 day of 5m, 3 days of 15m, 12 days of 1h) and a clear gain over the plain
average, otherwise it leaves the average alone. The bot writes `ticks.csv` (every 5 s) to feed it.

`params.json` example (any key is optional):

    {"defaults": {"vol_halflife_s": 300},
     "5m":  {"vol_short_weight": 1.0, "tail_w": 0.0, "call_up": 0.60, "keep_up": 0.55, "keep_down": 0.45, "call_down": 0.40},
     "1h":  {"blend_mode": "logit", "blend_a": 0.8, "blend_b": 0.4, "blend_c": 0.0}}

What this does not do: the backtest tunes the *chance model* only, because Polymarket does not publish historical
market prices. Being well calibrated is not the same as being profitable: the spread and the fee still apply.
The `pro` defaults are my starting guesses, not measured on live data. On my synthetic tests the plain model was
already right on calm bell-curve data, and the recent-volatility estimate won clearly on data where volatility
clusters; real Bitcoin will decide, which is what the backtest is for.

## Read before trusting any result
* **I could not reach Polymarket from my build sandbox**, so the live path was tested against a local mock that
  returns the response shapes in Polymarket US's docs, not against the real service. `--discover` is the first
  check; if it finds your three windows and a recent BRTI print, you are good.
* **5m settlement is unclear.** The official docs describe 15m and 60m Up/Down windows settling on the BRTI 60-second
  average. 5m windows were reported live on Oct 27, 2026, with a news report citing Chainlink TWAP. The bot reads each
  market's `indexSymbol` and shows a warning if it is not BRTI. Treat 5m chances as a proxy until you check the rules.
* The scorecard counts each window's **first** call (the final call is nearly always right because the answer is
  known by then). `first_call_secs_left` in `windows.csv` lets you drop late, trivial calls in Excel.
* Short windows are close to coin flips after fees. Simulator P&L means nothing about real markets.
* Keep your PC clock on NTP; the bot times the close from your clock.
* Example sizing only. Not financial advice.

