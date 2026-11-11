#!/usr/bin/env python3
"""Backtest for the day-trader skills: the live rules, day by day, on Kite history.
Standard library only. It never places an order.

    python3 backtest.py fetch --days 730                # 5-minute + daily candles -> trader/cache/
    python3 backtest.py run                             # the rules in trader/config.json
    python3 backtest.py run --stop or-low --target-r 1.5 --min-volume-x 1.0
    python3 backtest.py grid                            # a small grid of variants, split in two halves
    python3 backtest.py run --write                     # also write trader/backtest.json

What one simulated day does (the same code the live skills run):
  1. Every symbol in the universe: trader-entry's evaluate() on that day's 5-minute
     candles: the first completed bar after the 15-minute opening range that CLOSES
     above the range high, inside no_entries_before / no_entries_after, with the
     volume (min_volume_x) and VWAP (require_above_vwap) checks.
  2. ONE trade a day (max_trades_per_day; more than 1 takes the next ones in time
     order, one position at a time is NOT enforced): the earliest trigger, ties
     broken by the highest volume multiple. Nothing later in the day is used to pick.
  3. The plan is trader-exit's compute(): stop (or-mid by default) rounded down to
     the tick, target at R x risk. Plans under min_reward_risk are skipped.
  4. The fill is the NEXT bar's open plus slippage (you act after the trigger bar
     closes), not the trigger bar's close. A fill at or under the stop, or at or
     over the target, is no trade.
  5. Shares: the most that fit max_order_inr, the budget after the cash buffer and
     position cap, and max_loss_per_trade_inr at the stop. Under 1 share (or under
     --min-shares) is no trade.
  6. Bar by bar: the stop is checked before the target. A bar that OPENS under the
     stop sells at that open (a gap: worse than the stop). A stop sells at the
     stop minus slippage. Otherwise it sells at the close of the last bar by flat_by.
  7. P&L in rupees after Zerodha's intraday charges (trader-budget's formula).
     R = net P&L / (shares x planned risk a share).

Optional filters: --daily-setup (the symbol matched a trader-scanner daily setup on
the previous close), --max-rsi N (previous close's daily RSI 14 at most N),
--max-price P or --min-shares N (skip stocks too expensive to size).

A backtest is the past, on 5-minute bars, with assumed fills. It is not a forecast.
The grid tries several variants on the same history: the best one is partly luck,
which is why each line is shown for the first and the second half separately.
"""

import argparse
import datetime
import gzip
import importlib.util
import json
import math
import os
import statistics
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IST = datetime.timedelta(hours=5, minutes=30)
EPOCH = datetime.datetime(1970, 1, 1)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kite = _load("kite", "trader-broker/kite.py")
entry = _load("entry", "trader-entry/entry.py")
exitp = _load("exitp", "trader-exit/exit.py")
size = _load("size", "trader-budget/size.py")
scan = _load("scan", "trader-scanner/scan.py")


class BTError(Exception):
    pass


# ---------------------------------------------------------------- data

def read_universe(path):
    if not os.path.exists(path):
        raise BTError("no %s: run trader-scanner's `scan.py universe --list nifty50` first" % path)
    out = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(ln.split("|")[0].strip().upper())
    return out


def cache_path(cache, sym, kind):
    return os.path.join(cache, "%s-%s.json.gz" % (sym, kind))


def cmd_fetch(a):
    syms = read_universe(a.universe)
    os.makedirs(a.cache, exist_ok=True)
    try:
        kite.access_token()
    except kite.KiteError as e:
        raise BTError(str(e))
    now = kite.ist_now()
    frm = now - datetime.timedelta(days=a.days)
    done = 0
    for i, sym in enumerate(syms):
        try:
            five = kite.candles(sym, "5m", frm=frm, to=now)
            daily = kite.candles(sym, "1d", frm=frm - datetime.timedelta(days=120), to=now)
        except kite.KiteError as e:
            print("  %s: %s" % (sym, e), file=sys.stderr)
            continue
        for kind, rows in (("5m", five), ("1d", daily)):
            with gzip.open(cache_path(a.cache, sym, kind), "wt", encoding="utf-8") as fh:
                json.dump({"symbol": sym, "interval": kind, "fetched": now.isoformat(),
                           "candles": [[r["dt"].isoformat(), r["o"], r["h"], r["l"], r["c"], r["v"]] for r in rows]}, fh)
        done += 1
        if (i + 1) % 10 == 0:
            print("  %d of %d symbols" % (i + 1, len(syms)), file=sys.stderr)
    print("Fetched %d of %d symbols, %d days of 5-minute candles, into %s." % (done, len(syms), a.days, a.cache))
    return 0 if done else 1


def load_symbol(cache, sym):
    """-> (days {date: [bar dicts for entry.evaluate]}, daily [(date, close)]) or None."""
    p5, p1 = cache_path(cache, sym, "5m"), cache_path(cache, sym, "1d")
    if not os.path.exists(p5):
        return None
    with gzip.open(p5, "rt", encoding="utf-8") as fh:
        rows = json.load(fh)["candles"]
    days = {}
    for r in rows:
        dt = datetime.datetime.fromisoformat(r[0])
        if r[4] is None:
            continue
        t = int((dt - IST - EPOCH).total_seconds())
        days.setdefault(dt.date(), []).append({"t": t, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                                               "c": float(r[4]), "v": int(r[5] or 0)})
    daily = []
    if os.path.exists(p1):
        with gzip.open(p1, "rt", encoding="utf-8") as fh:
            for r in json.load(fh)["candles"]:
                if r[4] is not None:
                    daily.append((datetime.datetime.fromisoformat(r[0]).date(), float(r[4])))
    return days, daily


def load_all(cache, syms):
    data = {}
    for s in syms:
        got = load_symbol(cache, s)
        if got and got[0]:
            data[s] = got
    if not data:
        raise BTError("no cached candles in %s: run `backtest.py fetch` first (needs a Kite session)" % cache)
    return data


# ---------------------------------------------------------------- one variant

def daily_context(daily, day):
    """Yesterday's view: (setups, rsi) from daily closes strictly before `day`."""
    closes = [c for d, c in daily if d < day]
    if len(closes) < scan.MIN_DAILY_BARS:
        return [], None
    tags, nums = scan.find_setups(closes)
    return tags, nums.get("rsi")


def shares_for(cfg, fill, risk):
    caps = []
    if cfg.get("max_order_inr"):
        caps.append(float(cfg["max_order_inr"]) / fill)
    if cfg.get("budget"):
        inv = float(cfg["budget"]) * (1 - float(cfg.get("cash_buffer_pct") or 0) / 100.0)
        caps.append(inv * float(cfg.get("max_position_pct") or 100) / 100.0 / fill)
    if cfg.get("max_loss_per_trade_inr"):
        caps.append(float(cfg["max_loss_per_trade_inr"]) / risk)
    return int(math.floor(min(caps))) if caps else 1


def triggers_for_day(data, day, v, cfg, ticks):
    """Every symbol's trigger on `day` under variant v (no fills yet)."""
    out = []
    now_utc = datetime.datetime.combine(day, datetime.time(15, 31)) - IST
    ecfg = {"no_entries_before": cfg.get("no_entries_before") or "09:30",
            "no_entries_after": cfg.get("no_entries_after") or "14:45"}
    for sym, (days, daily) in data.items():
        bars = days.get(day)
        if not bars or len(bars) < 10:
            continue
        if v["daily_setup"] or v["max_rsi"]:
            tags, rsi = daily_context(daily, day)
            if v["daily_setup"] and not tags:
                continue
            if v["max_rsi"] and (rsi is None or rsi > v["max_rsi"]):
                continue
        chart = {"symbol": sym, "meta": {}, "bars": bars, "prev_close": None, "data_time": None}
        try:
            res = entry.evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m",
                                 min_volume_x=v["min_volume_x"] or None, cfg=ecfg, vwap_filter=v["vwap"])
        except entry.EntryError:
            continue
        orr, t = res.get("opening_range"), res.get("trigger")
        if res["status"] != "triggered" or not orr or orr["bars"] != orr["of"]:
            continue
        out.append({"symbol": sym, "slot": t["bar_index"], "time": t["close_time"], "close": t["close"],
                    "volume_x": t.get("volume_x") or 0, "or_high": orr["high"], "or_low": orr["low"],
                    "bars": res["_bars"], "tick": ticks.get(sym, 0.05)})
    out.sort(key=lambda x: (x["slot"], -x["volume_x"], x["symbol"]))
    return out


def simulate_trade(tr, v, cfg, flat_slot):
    """A trigger -> a trade dict, or (None, why)."""
    try:
        plan = exitp.compute(tr["close"], v["stop"], None, "r", Decimal(str(v["target_r"])),
                             tr["or_high"], tr["or_low"], tick=Decimal(str(tr["tick"])))
    except exitp.PlanError:
        return None, "plan refused"
    stop, target = float(plan["stop"]), float(plan["target"])
    if float(plan["reward_risk"]) + 1e-9 < float(cfg.get("min_reward_risk") or 0):
        return None, "reward:risk under the minimum"
    after = [b for b in tr["bars"] if tr["slot"] < b["slot"] <= flat_slot]
    if not after or after[0]["slot"] != tr["slot"] + 1:
        return None, "no bar after the trigger"
    slip = v["slippage_pct"] / 100.0
    fill = after[0]["o"] * (1 + slip)
    if fill <= stop or fill >= target:
        return None, "the fill was past the stop or the target"
    risk = fill - stop
    if v["max_stop_pct"] and risk / fill * 100 > v["max_stop_pct"]:
        return None, "stop too wide"
    if v["min_stop_pct"] and risk / fill * 100 < v["min_stop_pct"]:
        return None, "stop too tight for the charges"
    qty = shares_for(cfg, fill, risk)
    if qty < max(1, v["min_shares"]):
        return None, "too expensive to size"
    if v["max_price"] and fill > v["max_price"]:
        return None, "too expensive to size"
    out_price, outcome, out_slot = None, None, None
    for b in after:
        if b["o"] <= stop and b is not after[0]:
            out_price, outcome = b["o"] * (1 - slip), "gap"          # opened through the stop
        elif b["l"] <= stop:
            out_price, outcome = stop * (1 - slip), "stop"
        elif b["h"] >= target:
            out_price, outcome = target, "target"
        if outcome:
            out_slot = b["slot"]
            break
    if outcome is None:
        out_price, outcome, out_slot = after[-1]["c"] * (1 - slip), "time", after[-1]["slot"]
    gross = (out_price - fill) * qty
    charges = size.intraday_charges(qty, fill, out_price)
    net = gross - charges
    return {"symbol": tr["symbol"], "time": tr["time"], "entry": round(fill, 2), "stop": stop, "target": target,
            "qty": qty, "exit": round(out_price, 2), "outcome": outcome, "gross": round(gross, 2),
            "charges": round(charges, 2), "net": round(net, 2), "R": round(net / (qty * risk), 3),
            "gross_R": round(gross / (qty * risk), 3), "stop_pct": round(risk / fill * 100, 2),
            "volume_x": tr["volume_x"], "bars_held": out_slot - tr["slot"]}, None


def run_variant(data, days, v, cfg, ticks, trig_cache=None):
    flat = cfg.get("flat_by") or "15:15"
    hh, mm = [int(x) for x in flat.split(":")]
    flat_slot = ((hh * 60 + mm) - (9 * 60 + 15)) // 5 - 1          # the bar that ENDS at flat_by
    per_day = int(cfg.get("max_trades_per_day") or 1)
    trades, skipped, trig_days = [], {}, 0
    key = (v["min_volume_x"], v["vwap"], v["daily_setup"], v["max_rsi"],
           cfg.get("no_entries_before"), cfg.get("no_entries_after"))
    for day in days:
        if trig_cache is not None and (key, day) in trig_cache:
            trigs = trig_cache[(key, day)]
        else:
            trigs = triggers_for_day(data, day, v, cfg, ticks)
            if trig_cache is not None:
                trig_cache[(key, day)] = trigs
        if trigs:
            trig_days += 1
        taken = 0
        for tr in trigs:
            if taken >= per_day:
                break
            t, why = simulate_trade(tr, v, cfg, flat_slot)
            if t is None:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            t["date"] = day.isoformat()
            trades.append(t)
            taken += 1
    return trades, skipped, trig_days


def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    R = [t["R"] for t in trades]
    net = [t["net"] for t in trades]
    eq = peak = dd = 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    sd = statistics.pstdev(R) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else None
    streak = worst = 0
    for x in net:
        streak = streak + 1 if x < 0 else 0
        worst = max(worst, streak)
    cnt = lambda o: sum(1 for t in trades if t["outcome"] == o)
    return {"n": n, "wins": sum(1 for x in net if x > 0), "win_pct": round(100.0 * sum(1 for x in net if x > 0) / n, 1),
            "target": cnt("target"), "stop": cnt("stop"), "gap": cnt("gap"), "time": cnt("time"),
            "avg_R": round(statistics.mean(R), 3), "gross_avg_R": round(statistics.mean(t["gross_R"] for t in trades), 3),
            "se_R": round(se, 3) if se is not None else None,
            "t": round(statistics.mean(R) / se, 2) if se else None,
            "net": round(sum(net), 2), "charges": round(sum(t["charges"] for t in trades), 2),
            "max_drawdown": round(dd, 2), "worst_losing_streak": worst,
            "avg_stop_pct": round(statistics.mean(t["stop_pct"] for t in trades), 2),
            "worst_trade": round(min(net), 2), "best_trade": round(max(net), 2)}


def all_days(data):
    days = set()
    for d, _ in data.values():
        days.update(d)
    return sorted(days)


def variant_from(a, cfg):
    return {"stop": a.stop.replace("-", "_"), "target_r": a.target_r if a.target_r else float(cfg.get("min_reward_risk") or 2.0),
            "min_volume_x": (float(cfg.get("min_volume_x", 1.5)) if a.min_volume_x is None else a.min_volume_x),
            "vwap": bool(cfg.get("require_above_vwap", True)) and not a.no_vwap,
            "daily_setup": a.daily_setup, "max_rsi": a.max_rsi, "max_price": a.max_price,
            "min_shares": a.min_shares, "slippage_pct": a.slippage_pct,
            "min_stop_pct": a.min_stop_pct, "max_stop_pct": a.max_stop_pct}


def label(v):
    bits = ["stop %s" % v["stop"].replace("_", "-"), "%gR" % v["target_r"],
            "vol %gx" % v["min_volume_x"] if v["min_volume_x"] else "no vol", "vwap" if v["vwap"] else "no vwap"]
    if v["daily_setup"]:
        bits.append("daily setup")
    if v["max_rsi"]:
        bits.append("rsi<=%g" % v["max_rsi"])
    if v["min_stop_pct"]:
        bits.append("stop>=%g%%" % v["min_stop_pct"])
    if v["min_shares"] > 1:
        bits.append(">=%d sh" % v["min_shares"])
    return ", ".join(bits)


def load_cfg(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        raise BTError("no usable config at %s" % path)


def setup(a):
    cfg = load_cfg(a.config)
    syms = read_universe(a.universe)
    data = load_all(a.cache, syms)
    days = all_days(data)
    if a.since:
        days = [d for d in days if d.isoformat() >= a.since]
    try:
        ticks = {s: i["tick"] for s, i in kite.instruments("NSE").items() if s in data}
    except Exception:
        ticks = {}
    return cfg, data, days, ticks


def money(x):
    return ("-" if x < 0 else "") + "₹{:,.0f}".format(abs(x))


def cmd_run(a):
    cfg, data, days, ticks = setup(a)
    v = variant_from(a, cfg)
    trades, skipped, trig_days = run_variant(data, days, v, cfg, ticks)
    s = stats(trades)
    half = len(days) // 2
    cut = days[half].isoformat()
    s1, s2 = stats([t for t in trades if t["date"] < cut]), stats([t for t in trades if t["date"] >= cut])
    doc = {"created_by": "trader-backtest", "variant": v, "label": label(v), "symbols": len(data), "sessions": len(days),
           "from": days[0].isoformat(), "to": days[-1].isoformat(), "days_with_a_trigger": trig_days,
           "config": {k: cfg.get(k) for k in ("budget", "max_order_inr", "max_loss_per_trade_inr", "max_trades_per_day",
                                               "min_reward_risk", "no_entries_before", "no_entries_after", "flat_by")},
           "stats": s, "first_half": s1, "second_half": s2, "skipped": skipped, "trades": trades}
    if a.write:
        os.makedirs(os.path.dirname(a.write) or ".", exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
            fh.write("\n")
    if a.json:
        print(json.dumps({k: v_ for k, v_ in doc.items() if k != "trades"}, indent=2))
        return 0
    print("Backtest: %s" % label(v))
    print("%d symbols, %d sessions (%s to %s), budget %s, max order %s, max loss a trade %s, %d trade(s) a day."
          % (len(data), len(days), days[0], days[-1], money(cfg.get("budget") or 0), money(cfg.get("max_order_inr") or 0),
             money(cfg.get("max_loss_per_trade_inr") or 0), int(cfg.get("max_trades_per_day") or 1)))
    if not s["n"]:
        print("No trades. Skipped: %s" % (skipped or "nothing triggered"))
        return 1
    print("%d trades on %d sessions (a trigger on %d): %d target, %d stop, %d gap through the stop, %d sold at %s."
          % (s["n"], len(days), trig_days, s["target"], s["stop"], s["gap"], s["time"], cfg.get("flat_by") or "15:15"))
    print("  Win rate        %.1f %% (%d of %d after charges)" % (s["win_pct"], s["wins"], s["n"]))
    print("  Average R       %+.3f after charges (%+.3f before), standard error %s, t = %s"
          % (s["avg_R"], s["gross_avg_R"], s["se_R"], s["t"]))
    print("  Net P&L         %s after %s of charges" % (money(s["net"]), money(s["charges"])))
    print("  Worst drawdown  %s; worst losing streak %d trades; worst trade %s, best %s"
          % (money(s["max_drawdown"]), s["worst_losing_streak"], money(s["worst_trade"]), money(s["best_trade"])))
    print("  Average stop    %.2f %% under the fill" % s["avg_stop_pct"])
    for name, h in (("First half ", s1), ("Second half", s2)):
        if h["n"]:
            print("  %s     %d trades, average R %+.3f, net %s" % (name, h["n"], h["avg_R"], money(h["net"])))
    if skipped:
        print("  Not taken: " + ", ".join("%d %s" % (n, k) for k, n in sorted(skipped.items(), key=lambda kv: -kv[1])))
    t = s["t"]
    if t is None or abs(t) < 2:
        print("Verdict: not distinguishable from zero on this sample (|t| under 2). No proven edge either way.")
    elif t >= 2:
        print("Verdict: positive after charges on this sample (t %.1f). Past sessions, assumed fills: not a promise." % t)
    else:
        print("Verdict: NEGATIVE after charges on this sample (t %.1f). These rules lost money here." % t)
    if a.write:
        print("Wrote %s." % a.write)
    return 0


def cmd_grid(a):
    cfg, data, days, ticks = setup(a)
    base = variant_from(a, cfg)
    cut = days[len(days) // 2].isoformat()
    cache, rows = {}, []
    combos = []
    for vol in (0, 1.0, 1.5, 2.0):
        for vw in (True, False):
            for stop in ("or_mid", "or_low"):
                for r in (1.5, 2.0, 3.0):
                    combos.append(dict(base, min_volume_x=vol, vwap=vw, stop=stop, target_r=r))
    for i, v in enumerate(combos):
        if v["target_r"] + 1e-9 < float(cfg.get("min_reward_risk") or 0):
            cfg_v = dict(cfg, min_reward_risk=v["target_r"])
        else:
            cfg_v = cfg
        trades, _, _ = run_variant(data, days, v, cfg_v, ticks, cache)
        s = stats(trades)
        s1, s2 = stats([t for t in trades if t["date"] < cut]), stats([t for t in trades if t["date"] >= cut])
        rows.append((v, s, s1, s2))
        if (i + 1) % 12 == 0:
            print("  %d of %d variants" % (i + 1, len(combos)), file=sys.stderr)
    rows.sort(key=lambda r: -(r[1].get("avg_R") or -9))
    print("Grid: %d variants, %d symbols, %d sessions (%s to %s). Average R is after charges."
          % (len(rows), len(data), len(days), days[0], days[-1]))
    print("%-44s %6s %7s %6s %9s %8s %8s" % ("variant", "trades", "avg R", "t", "net", "1st half", "2nd half"))
    for v, s, s1, s2 in rows:
        if not s["n"]:
            continue
        print("%-44s %6d %+7.3f %6s %9s %+8.3f %+8.3f" % (label(v), s["n"], s["avg_R"], s["t"], money(s["net"]),
                                                          s1.get("avg_R") or 0, s2.get("avg_R") or 0))
    print("%d variants were tried on one history: the top line is partly luck. Trust a variant only if both "
          "halves agree and t is above 2." % len(rows))
    if a.write:
        os.makedirs(os.path.dirname(a.write) or ".", exist_ok=True)
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump([{"variant": v, "label": label(v), "stats": s, "first_half": s1, "second_half": s2}
                       for v, s, s1, s2 in rows], fh, indent=1)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backtest the live day-trade rules on cached Kite candles.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("fetch", help="download candles for the universe into the cache")
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--universe", default="trader/universe.txt")
    p.add_argument("--cache", default="trader/cache")
    for name in ("run", "grid"):
        p = sub.add_parser(name)
        p.add_argument("--universe", default="trader/universe.txt")
        p.add_argument("--cache", default="trader/cache")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--since", help="only sessions from this date, YYYY-MM-DD")
        p.add_argument("--stop", default="or-mid", choices=["or-mid", "or-low"])
        p.add_argument("--target-r", type=float, help="default: the config's min_reward_risk")
        p.add_argument("--min-volume-x", type=float, help="default: config min_volume_x (1.5); 0 = off")
        p.add_argument("--no-vwap", action="store_true")
        p.add_argument("--daily-setup", action="store_true", help="only symbols with a scanner daily setup the day before")
        p.add_argument("--max-rsi", type=float, help="skip when the previous close's daily RSI 14 is above this")
        p.add_argument("--max-price", type=float, help="skip stocks above this price")
        p.add_argument("--min-shares", type=int, default=1, help="skip a trade that sizes to fewer shares")
        p.add_argument("--min-stop-pct", type=float, help="skip stops tighter than this %% of the price")
        p.add_argument("--max-stop-pct", type=float, help="skip stops wider than this %% of the price")
        p.add_argument("--slippage-pct", type=float, default=0.02, help="on the entry and on stop / time exits (0.02)")
        p.add_argument("--write", nargs="?", const="trader/backtest.json", metavar="PATH")
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"fetch": cmd_fetch, "run": cmd_run, "grid": cmd_grid}[a.cmd](a)
    except BTError as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
