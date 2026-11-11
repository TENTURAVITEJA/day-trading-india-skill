---
name: trader-broker
description: >-
  Order Executor for Indian stocks (NSE/BSE). Sends approved orders to your
  broker (Zerodha Kite, through Zerodha's official Kite MCP server), paper
  mode by default, never without your yes. Shows each order as a plain ticket
  (the MIS buy, the SL-M protective stop, the target that trader-alerts
  watches, and the max loss at the stop) plus the exact Kite place_order
  calls, records your yes word for word, then Claude's own Allow once / Deny
  card asks again. Builds the sell ticket that squares off an intraday trade
  by your flat-by time (before Zerodha's ~3:20 PM auto square-off), records
  fills, and logs each trade's max loss so trader-risk can hold the daily
  limit. Covers connecting Kite MCP, logging in without sharing passwords,
  and setting order tools to "Needs approval". Use when the user says "place
  the order", "buy it", "execute", "send it to Zerodha", "square off",
  "close my position", "flatten", "get me out", "connect Kite", or after
  /trader-risk passes.
argument-hint: "[order number]"
---

# trader-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. An order reaches NSE/BSE only after three
separate gates, every time, for every order:

1. **trader-risk passed this exact order today** (matched by fingerprint).
2. **The user typed an explicit yes for this one order** in the chat.
3. **The user clicked Allow once** on Claude's own permission card, which
   appears because the Kite order tools are set to "Needs approval".

Two yeses from the user, one PASS from the rules. No yes, no order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every ticket, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --status placed --broker-id ID --filled-qty 16 --filled-price 2768.50
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --symbol ADANIENT --side sell --qty 16 --price 2737.75
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol ADANIENT --qty 16
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that order, builds the square-off ticket, and keeps the log
in `trader/state/broker.json`. `--now "2026-11-11 09:45"` pins the clock (IST).

## Paper mode (the default)

Zerodha has **no paper-trading account**. So `"paper": true` (the default in
`trader/config.json`) means: nothing is ever sent to Kite. After the user's
yes, record a simulated fill at the current price with `ticket.py fill`, and
track the trade with trader-alerts exactly as if it were live. The user flips
`"paper": false` themselves when they choose to trade real money. Never flip it.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | the proposed orders (trader-budget writes it; `flatten` replaces it) |
| `trader/state/risk.json` | trader-risk's verdicts; only a PASS from today is READY |
| `trader/state/broker.json` | every yes, no, placed, failed and filled, with each entry's max loss |
| `trader/portfolio.json` | cash and shares, updated by `fill` and by `result --filled-qty` |
| `trader/plan.json` | the flat-by time for `flatten` |

## Setup: Zerodha Kite MCP

Zerodha's Kite MCP server comes in two forms. Say which one the user has
before promising anything.

**A. Hosted, read-only (free, no API key).** `https://mcp.kite.trade/mcp`.
Zerodha's hosted server only reads: profile, margins, holdings, positions,
orders, quotes. **It cannot place, modify or cancel regular orders.** It is
enough for paper mode with real quotes and for checking positions.

- Claude Desktop / claude.ai: Customize > Connectors > Add custom connector,
  URL `https://mcp.kite.trade/mcp`. Or the block in
  `claude_desktop_config.example.json` here (uses `npx mcp-remote`, needs Node.js).
- Claude Code: `claude mcp add --transport http kite https://mcp.kite.trade/mcp`

**B. Self-hosted, with order tools (needed for LIVE orders).** The user runs
Zerodha's open-source server (github.com/zerodha/kite-mcp-server, Go) on
their own computer with their own Kite Connect API key:

1. A Zerodha trading account with TOTP 2FA enabled.
2. Sign up at `developers.kite.trade` and create an app. They get an
   `api_key` and an `api_secret`. The plans and prices change: tell the user
   to read the current ones on that page (historically a free personal plan
   without market data, and a paid Connect plan with live data).
3. Set the app's redirect URL to the one the server's README gives for local
   use, build and run the server with `KITE_API_KEY` and `KITE_API_SECRET` in
   its environment (never in the chat), and add its local URL to Claude.
4. SEBI's retail algo-trading rules apply to API orders (brokers may require
   a registered static IP for order placement and limit orders per second).
   Tell the user to check Zerodha's current requirements before going live.

Either way:

- Log in with the Kite `login` tool: it returns a link; the user logs in **on
  zerodha.com** with their own password and TOTP. **Never ask for, accept or
  repeat the user's Kite password, TOTP, PIN, api_secret or access token.** If
  they paste one, tell them to delete the message and regenerate it. The
  session expires every morning (around 6 AM IST): log in again each trading day.
- **Tool permissions** (Customize > Connectors > kite, or Claude Code
  `"permissions": {"ask": [...]}`): `place_order`, `modify_order`,
  `cancel_order` and the GTT tools: **Needs approval**, never Always allow.
  Read-only tools (`get_profile`, `get_margins`, `get_holdings`,
  `get_positions`, `get_orders`, `get_quotes`, `get_ltp`): Always allow is fine.
- Check it: `get_profile` and `get_margins`. If `place_order` is not in the
  tool list, the user is on the hosted read-only server: say so, stay in paper
  mode, or show the ticket for them to place by hand in the Kite app.

Other brokers (Upstox, Angel One, Groww, Dhan...): set `"broker"` to its name.
The ticket then shows the order in plain words and the user places it in their
own broker app; this pack drives only Kite.

## How an intraday trade is sent (no bracket orders in India)

Zerodha discontinued bracket and cover orders, so the plan's three prices go out like this:

1. **Entry**: `place_order` — `variety: regular`, `exchange: NSE`,
   `transaction_type: BUY`, `product: MIS` (intraday), `order_type: MARKET`
   (or `LIMIT` with `price`), `quantity`, `validity: DAY`.
2. **Protective stop, only after the entry FILLED** (check `get_order_history`
   or `get_positions`): `place_order` — `transaction_type: SELL`,
   `order_type: SL-M`, `trigger_price` = the plan's stop, same quantity, `MIS`.
   This is a second order, so it needs its own Allow once; the user's one yes
   for the ticket covers both legs of THAT ticket, nothing else.
3. **Target: NOT sent as an order.** Two live sell orders on the same shares
   can both fill and leave the user short. trader-alerts rings at the target;
   then cancel the SL-M (`cancel_order`) and square off with a `flatten` ticket.

`product: MIS` positions are auto squared-off by Zerodha from about 3:20 PM
IST with an extra charge, which is why `flat_by` defaults to 15:15.
`allow_margin` is false by default, so the size uses the user's own cash, not
MIS leverage.

## Steps: the entry

1. `ticket.py show`. Only READY tickets go to the user. A ticket that is NOT
   READY or BLOCKED goes back to `/trader-risk`.
2. Show ONE ticket and ask: "Place this order? Reply yes or no." One order
   per question. Never offer "yes to all".
3. Run `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   If it prints "Not recorded", the reply was not an explicit yes for this
   order. Ask again. Never turn a reply into a yes yourself.
4. Paper: `ticket.py fill` at the current price (from `/trader-alerts quote`).
   Live: call `place_order` with exactly the entry args the ticket printed;
   Claude's card appears: Allow once or Deny. When it has filled, place the
   SL-M stop exactly as printed.
5. Record what happened: `ticket.py result --index N --status placed
   --broker-id <order_id> --filled-qty Q --filled-price P`, or `--status denied`,
   or `--status failed` with `--note`. A rejected order (insufficient margin,
   circuit limit, stock in ban / ASM / trade-for-trade) is not retried without
   a new check and a new yes.

## Steps: while it is open, and out by the close

- trader-alerts watches the stop, the target and the flat-by time
  (`alerts.py add --from-plan`). The SL-M on the exchange does the stopping.
- When the SL-M fills, record it: `ticket.py fill --symbol ADANIENT --side sell --qty 16 --price 2737.75`.
- At the target, or by `flat_by` (15:15 IST), close what is still open:
  1. `get_positions` (read only): the MIS quantity held.
  2. `ticket.py flatten --symbol ADANIENT --qty 16` writes a MARKET SELL MIS ticket.
  3. `/trader-risk`. A sell that only closes what the user holds is never
     blocked by the daily limits or the entry window.
  4. `ticket.py show`, then the user's yes for that one order.
  5. `get_orders`, then `cancel_order` the open SL-M (Needs approval) so it
     cannot also fill. Then `place_order` with the ticket, then `result`.
  6. `get_positions` again: net quantity must be 0.

## Example

```
ORDER 1 of 1, PAPER account, Zerodha Kite
  BUY 16 ADANIENT, limit 2768.50, good for today
  stop-loss 2737.75
  take-profit 2830.00
  max loss about ₹492 if the stop fills at 2737.75 (it can fill worse in a fast market)
  why: opening-range breakout on ADANIENT: 16 shares, bound by your ₹500 max loss per trade (16 x 30.75 = ₹492 at the stop)
  READY: trader-risk PASS, waiting for your yes
  PAPER: Zerodha has no paper account, so nothing is sent. Record a simulated fill with
  ticket.py fill once you say yes. These are the calls a LIVE run would make:
    entry: place_order
      {"variety": "regular", "exchange": "NSE", "tradingsymbol": "ADANIENT", "quantity": 16,
       "product": "MIS", "validity": "DAY", "transaction_type": "BUY", "order_type": "LIMIT", "price": 2768.5}
    after the BUY has filled: protective stop: place_order
      {"variety": "regular", "exchange": "NSE", "tradingsymbol": "ADANIENT", "quantity": 16,
       "product": "MIS", "validity": "DAY", "transaction_type": "SELL", "order_type": "SL-M", "trigger_price": 2737.75}
```

## What it never does

- Never places an order without all three gates, and never two tickets on one yes.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats Kite passwords, TOTP, PIN or API secrets.
- Never switches `"paper"` to false. The user does that themselves.
- Never places F&O, currency or commodity orders, CNC delivery trades, or short sells unless the config allows it and the user confirms.
- Never sends the target as a second live sell order (it could leave the user short).
- Never promises a fill price: a market order and an SL-M fill at whatever the market gives, and a stock locked at its circuit limit may not fill at all.

## Safety

- Education and tooling, not investment advice; this pack is not a SEBI-registered investment adviser or research analyst. A PASS is a rules check, not a recommendation.
- SEBI's July 2023 study found that about 7 in 10 individual intraday traders in the equity cash segment lost money in FY23. Say so if the user is new.
- Paper by default (`"paper": true`). Going live is the user's own decision, in their own config.
- No margin (MIS leverage), short selling, F&O or options unless the config allows it and the user confirms.
