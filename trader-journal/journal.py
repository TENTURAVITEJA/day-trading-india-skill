#!/usr/bin/env python3
"""Trade journal for the day-trader skills: every signal, taken or skipped, with the
checks that passed and the result in R after charges. Standard library only.
It never places an order.

    python3 journal.py log --taken                      # the signal in trader/entry.json + plan + odds + risk
    python3 journal.py log --skip "time machine leaned to the stop"
    python3 journal.py log --skip "news: results today" --news flagged
    python3 journal.py resolve                          # after flat_by: what happened, taken or not
    python3 journal.py report                           # the review: counts, R, rule breaks
    python3 journal.py list

log      Snapshots the pipeline's files for ONE signal into trader/journal.json:
         trader/entry.json (the trigger), trader/plan.json (stop, target, R),
         trader/timemachine.json (odds, average R after charges), trader/state/risk.json
         (PASS / BLOCK) and your decision. A file about another symbol or day is
         ignored (that check counts as "not run"). With no plan for the signal it
         uses the default one (opening-range midpoint stop, the config's
         min_reward_risk as the target R) so a skipped signal can still be scored.
         Logging the same signal again replaces its record.

         The checks:
           trigger       entry.json says triggered
           reward_risk   the plan's reward:risk is at least min_reward_risk
           time_machine  average R after charges above 0, and the history's too when it fired
           risk          trader-risk said PASS for this symbol today
           news          what you say with --news clear|flagged (not checked by code)

resolve  For every record of a finished session without a result: reads that day's
         5-minute Kite candles and plays the plan forward "by the rules": the fill
         is the next bar's open after the trigger bar closes, the stop is checked
         before the target, a bar that opens under the stop sells at that open,
         anything still open sells at the close of the last bar by flat_by. Shares
         come from the config's limits, charges are Zerodha's intraday charges.
         That is done for skipped signals too: it is the only way to see what a
         skip cost or saved. For a taken trade it also reads your recorded fills
         (trader/state/broker.json) for the actual result.

report   Signals, taken, skipped, the average R by the rules for each group, your
         actual net rupees, each check's pass rate, and rule breaks: a trade taken
         with a failed or missing check, and a signal skipped with every check passed.
         Under 30 resolved signals it says the sample is too small to judge an edge.

Data: Zerodha Kite 5-minute candles through trader-broker/kite.py (resolve only).
Times are IST. --now "2026-11-11 15:20" pins the clock; --dir picks another trader/ folder.
"""

import argparse
import datetime
import importlib.util
import json
import math
import os
import re
import statistics
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IST = datetime.timedelta(hours=5, minutes=30)
OPEN_MIN = 9 * 60 + 15
SLIPPAGE_PCT = 0.02
SMALL_SAMPLE = 30
CHECKS = ("trigger", "reward_risk", "time_machine", "risk")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kite = _load("kite", "trader-broker/kite.py")
exitp = _load("exitp", "trader-exit/exit.py")
size = _load("size", "trader-budget/size.py")


class JournalError(Exception):
    pass


# ---------------------------------------------------------------- small helpers

def now_ist(text=None):
    if text:
        try:
            return datetime.datetime.fromisoformat(text.strip())
        except ValueError:
            raise JournalError('--now wants a time like "2026-11-11 15:20" (IST)')
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + IST


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_journal(path):
    doc = read_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("records"), list):
        doc = {"created_by": "trader-journal", "records": []}
    return doc


def clock_minutes(text):
    """'9:40 AM' or '09:40' -> minutes since midnight, or None."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*([AaPp][Mm])?\s*$", str(text or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        pm = m.group(3).lower() == "pm"
        h = (h % 12) + (12 if pm else 0)
    return h * 60 + mi


def money(x):
    if x is None:
        return "—"
    return ("-" if x < 0 else "") + "₹{:,.2f}".format(abs(x))


def same(doc, sym, date):
    return isinstance(doc, dict) and str(doc.get("symbol") or "").upper() == sym and doc.get("date") == date


# ---------------------------------------------------------------- log

def default_plan(entry_doc, cfg):
    """No plan was written for this signal: the default one, so it can still be scored."""
    orr = entry_doc.get("opening_range") or {}
    r = Decimal(str(cfg.get("min_reward_risk") or 2.0))
    try:
        ex, ts = kite.split_symbol(entry_doc["symbol"])
        tick = Decimal(str((kite.instruments(ex).get(ts) or {}).get("tick") or 0.05))
    except Exception:
        tick = Decimal("0.05")
    try:
        if entry_doc.get("setup") == "orb" and orr.get("high") is not None:
            c = exitp.compute(entry_doc["price"], "or_mid", None, "r", r, orr["high"], orr["low"], tick=tick)
        else:
            c = exitp.compute(entry_doc["price"], "pct", Decimal("1"), "r", r, tick=tick)
    except exitp.PlanError:
        return None
    return {"entry": float(c["entry"]), "stop": float(c["stop"]), "target": float(c["target"]),
            "target_r": float(c["target_r"]), "reward_risk": float(c["reward_risk"]),
            "stop_label": c["stop_label"], "source": "default"}


def build_record(folder, a, now):
    cfg = read_json(os.path.join(folder, "config.json")) or {}
    ent = read_json(os.path.join(folder, "entry.json"))
    plan = read_json(os.path.join(folder, "plan.json"))
    sym = (a.symbol or (ent or {}).get("symbol") or (plan or {}).get("symbol") or "").upper()
    if not sym:
        raise JournalError("nothing to log: no trader/entry.json or trader/plan.json, and no --symbol")
    date = a.date or next((d.get("date") for d in (ent, plan) if isinstance(d, dict)
                           and str(d.get("symbol") or "").upper() == sym and d.get("date")), None) \
        or now.date().isoformat()
    ent = ent if same(ent, sym, date) else None
    plan = plan if same(plan, sym, date) else None
    tm = read_json(os.path.join(folder, "timemachine.json"))
    tm = tm if isinstance(tm, dict) and same(tm.get("plan"), sym, date) else None
    risk_doc = read_json(os.path.join(folder, "state", "risk.json"))
    risk_row = None
    if isinstance(risk_doc, dict) and risk_doc.get("date") == date:
        rows = [r for r in risk_doc.get("results") or [] if str(r.get("symbol") or "").upper() == sym
                and str(r.get("side") or "").lower() == "buy"]
        risk_row = rows[0] if rows else None

    rec = {"id": "%s-%s" % (date, sym), "date": date, "symbol": sym, "setup": (ent or plan or {}).get("setup") or "orb",
           "logged_at": now.strftime("%Y-%m-%d %H:%M"), "decision": "taken" if a.taken else "skipped",
           "reason": a.skip if not a.taken else (a.note or ""), "note": a.note or "", "news": a.news}
    if ent:
        rec["signal"] = {"status": ent.get("status"), "time": ent.get("time"), "price": ent.get("price"),
                         "level": ent.get("level"), "opening_range": ent.get("opening_range")}
    p = None
    if plan:
        p = {k: plan.get(k) for k in ("entry", "stop", "target", "target_r", "reward_risk", "stop_label",
                                      "entry_time", "flat_by")}
        p["source"] = "plan"
    elif ent and ent.get("status") == "triggered" and ent.get("price"):
        p = default_plan(ent, cfg)
        if p:
            p["entry_time"] = ent.get("time")
    if p:
        p.setdefault("entry_time", (ent or {}).get("time"))
        p["flat_by"] = p.get("flat_by") or cfg.get("flat_by") or "15:15"
        rec["plan"] = p
    if tm:
        mc, h = tm.get("mc") or {}, tm.get("history") or {}
        rec["time_machine"] = {"p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"), "avg_R": mc.get("avg_R"),
                               "history_avg_R": h.get("avg_R") if h.get("triggered") else None,
                               "verdict": tm.get("verdict")}
    if risk_row:
        rec["risk"] = {"verdict": risk_row.get("verdict"), "reasons": risk_row.get("reasons") or []}

    mrr = float(cfg.get("min_reward_risk") or 0)
    checks = {"trigger": (ent.get("status") == "triggered") if ent else None,
              "reward_risk": (float(plan.get("reward_risk") or 0) + 1e-9 >= mrr) if plan else None,
              "time_machine": None, "risk": (risk_row.get("verdict") == "PASS") if risk_row else None}
    if tm:
        t = rec["time_machine"]
        ok = t["avg_R"] is not None and t["avg_R"] > 0
        if t["history_avg_R"] is not None:
            ok = ok and t["history_avg_R"] > 0
        checks["time_machine"] = ok
    rec["checks"] = checks
    return rec


def check_words(checks, news=None):
    out = []
    for k in CHECKS:
        v = checks.get(k)
        out.append("%s %s" % (k.replace("_", " "), "PASS" if v is True else "FAIL" if v is False else "not run"))
    if news:
        out.append("news %s" % news)
    return ", ".join(out)


def cmd_log(a):
    if bool(a.taken) == bool(a.skip):
        raise JournalError('say what you did: --taken, or --skip "why you passed"')
    now = now_ist(a.now)
    rec = build_record(a.dir, a, now)
    path = os.path.join(a.dir, "journal.json")
    j = load_journal(path)
    old = next((r for r in j["records"] if r["id"] == rec["id"]), None)
    if old and old.get("result") and not a.taken == (old.get("decision") == "taken"):
        old_result = None      # the decision changed: score it again
    else:
        old_result = (old or {}).get("result")
    if old_result:
        rec["result"] = old_result
    j["records"] = [r for r in j["records"] if r["id"] != rec["id"]] + [rec]
    j["records"].sort(key=lambda r: r["id"])
    write_json(path, j)
    print("%s %s %s: %s%s." % ("Replaced" if old else "Logged", rec["date"], rec["symbol"], rec["decision"].upper(),
                               (' ("%s")' % rec["reason"]) if rec["reason"] else ""))
    print("Checks: %s." % check_words(rec["checks"], rec.get("news")))
    p = rec.get("plan")
    if p:
        print("Plan (%s): entry %s, stop %s, target %s." % ("from trader-exit" if p["source"] == "plan" else "default",
                                                            money(p["entry"]), money(p["stop"]), money(p["target"])))
    else:
        print("No trigger price and no plan, so this one cannot be scored later.")
    failed = [k for k in CHECKS if rec["checks"].get(k) is not True]
    if rec["decision"] == "taken" and failed:
        print("Rule break: taken with %s." % ", ".join("%s %s" % (k.replace("_", " "), "failed" if rec["checks"][k] is False
                                                                 else "not run") for k in failed))
    print("After %s run: journal.py resolve" % ((p or {}).get("flat_by") or "15:15"))
    return 0


# ---------------------------------------------------------------- resolve

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


def by_the_rules(rec, bars, cfg):
    """The plan played forward on the day's 5-minute bars. -> result dict."""
    p = rec["plan"]
    start = clock_minutes(p.get("entry_time"))
    flat = clock_minutes(p.get("flat_by") or "15:15") or (15 * 60 + 15)
    if start is None:
        return {"outcome": "unscored", "why": "no entry time on the record"}
    after = [b for b in bars if start <= b["dt"].hour * 60 + b["dt"].minute and
             b["dt"].hour * 60 + b["dt"].minute + 5 <= flat]
    if not after:
        return {"outcome": "unscored", "why": "no 5-minute bars after the trigger"}
    slip = SLIPPAGE_PCT / 100.0
    stop, target = float(p["stop"]), float(p["target"])
    fill = after[0]["o"] * (1 + slip)
    if fill <= stop or fill >= target:
        return {"outcome": "no fill", "why": "the next bar opened past the stop or the target", "fill": round(fill, 2)}
    risk = fill - stop
    qty = shares_for(cfg, fill, risk)
    if qty < 1:
        return {"outcome": "no fill", "why": "too expensive for one share inside your limits", "fill": round(fill, 2)}
    out_price = outcome = out_bar = None
    for i, b in enumerate(after):
        if i and b["o"] <= stop:
            out_price, outcome = b["o"] * (1 - slip), "gap"
        elif b["l"] <= stop:
            out_price, outcome = stop * (1 - slip), "stop"
        elif b["h"] >= target:
            out_price, outcome = target, "target"
        if outcome:
            out_bar = b
            break
    if outcome is None:
        out_bar = after[-1]
        out_price, outcome = out_bar["c"] * (1 - slip), "time"
    gross = (out_price - fill) * qty
    charges = size.intraday_charges(qty, fill, out_price)
    net = gross - charges
    return {"outcome": outcome, "fill": round(fill, 2), "exit": round(out_price, 2),
            "exit_time": (out_bar["dt"] + datetime.timedelta(minutes=5)).strftime("%H:%M"), "qty": qty,
            "gross": round(gross, 2), "charges": round(charges, 2), "net": round(net, 2),
            "R": round(net / (qty * risk), 3)}


def actual_from_fills(rec, state, plan):
    """Your recorded fills for the symbol that day -> the actual result, or None."""
    rows = [o for o in (state or {}).get("orders") or [] if o.get("status") == "filled"
            and o.get("date") == rec["date"] and str(o.get("symbol") or "").upper() == rec["symbol"]]
    buys = [o for o in rows if str(o.get("side")).lower() == "buy"]
    sells = [o for o in rows if str(o.get("side")).lower() == "sell"]
    if not buys:
        return None
    bq = sum(float(o["qty"]) for o in buys)
    buy = sum(float(o["qty"]) * float(o["price"]) for o in buys) / bq
    if not sells:
        return {"status": "open", "buy": round(buy, 2), "qty": bq}
    sq = sum(float(o["qty"]) for o in sells)
    sell = sum(float(o["qty"]) * float(o["price"]) for o in sells) / sq
    q = min(bq, sq)
    gross = (sell - buy) * q
    charges = size.intraday_charges(q, buy, sell)
    net = gross - charges
    risk = buy - float(plan["stop"]) if plan and plan.get("stop") is not None else None
    return {"status": "closed", "buy": round(buy, 2), "sell": round(sell, 2), "qty": q, "gross": round(gross, 2),
            "charges": round(charges, 2), "net": round(net, 2),
            "R": round(net / (q * risk), 3) if risk and risk > 0 else None}


def cmd_resolve(a):
    now = now_ist(a.now)
    path = os.path.join(a.dir, "journal.json")
    j = load_journal(path)
    cfg = read_json(os.path.join(a.dir, "config.json")) or {}
    state = read_json(os.path.join(a.dir, "state", "broker.json"))
    todo = [r for r in j["records"] if a.all or not (r.get("result") or {}).get("rules")]
    if not todo:
        print("Nothing to resolve: every logged signal already has a result.")
        return 0
    done = 0
    for r in todo:
        p = r.get("plan")
        if not p:
            r["result"] = {"rules": {"outcome": "unscored", "why": "no plan and no trigger price"}}
            continue
        flat = clock_minutes(p.get("flat_by") or "15:15")
        day = datetime.date.fromisoformat(r["date"])
        if now < datetime.datetime.combine(day, datetime.time(flat // 60, flat % 60)):
            print("%s %s: wait until %s IST." % (r["date"], r["symbol"], p.get("flat_by") or "15:15"))
            continue
        try:
            bars = kite.candles(r["symbol"], "5m", frm=datetime.datetime.combine(day, datetime.time(9, 0)),
                                to=datetime.datetime.combine(day, datetime.time(15, 59)))
        except kite.KiteError as e:
            raise JournalError(str(e))
        res = {"rules": by_the_rules(r, bars, cfg), "resolved_at": now.strftime("%Y-%m-%d %H:%M")}
        if r["decision"] == "taken":
            res["actual"] = actual_from_fills(r, state, p)
        r["result"] = res
        done += 1
        ru = res["rules"]
        line = "%s %s (%s): by the rules, %s" % (r["date"], r["symbol"], r["decision"], ru["outcome"])
        if ru.get("R") is not None:
            line += " at %s, %s shares, net %s, %+.2fR after charges" % (ru["exit_time"], ru["qty"], money(ru["net"]), ru["R"])
        elif ru.get("why"):
            line += " (%s)" % ru["why"]
        print(line + ".")
        ac = res.get("actual")
        if r["decision"] == "taken":
            if not ac:
                print("   No recorded fill for it: record the paper fill with trader-broker (ticket.py fill).")
            elif ac["status"] == "open":
                print("   Your buy at %s is recorded but no sell: record the exit fill." % money(ac["buy"]))
            else:
                print("   Your fills: bought %s, sold %s, net %s%s." % (money(ac["buy"]), money(ac["sell"]), money(ac["net"]),
                                                                       (", %+.2fR" % ac["R"]) if ac.get("R") is not None else ""))
    write_json(path, j)
    print("Resolved %d signal%s. Review: journal.py report" % (done, "" if done == 1 else "s"))
    return 0


# ---------------------------------------------------------------- report / list

def all_passed(r):
    return all(r["checks"].get(k) is True for k in CHECKS)


def avg(xs):
    return statistics.mean(xs) if xs else None


def cmd_report(a):
    j = load_journal(os.path.join(a.dir, "journal.json"))
    recs = [r for r in j["records"] if not a.since or r["date"] >= a.since]
    if not recs:
        print("The journal is empty%s. Log a signal with: journal.py log --taken | --skip \"why\""
              % (" since %s" % a.since if a.since else ""))
        return 1
    taken = [r for r in recs if r["decision"] == "taken"]
    skipped = [r for r in recs if r["decision"] == "skipped"]
    R = lambda rs: [(r.get("result") or {}).get("rules", {}).get("R") for r in rs
                    if (r.get("result") or {}).get("rules", {}).get("R") is not None]
    scored = R(recs)
    days = sorted({r["date"] for r in recs})
    doc = {"from": days[0], "to": days[-1], "days": len(days), "signals": len(recs), "taken": len(taken),
           "skipped": len(skipped), "scored": len(scored),
           "avg_R_rules_taken": avg(R(taken)), "avg_R_rules_skipped": avg(R(skipped)), "avg_R_rules_all": avg(scored)}
    act = [(r["result"]["actual"]) for r in taken if (r.get("result") or {}).get("actual")
           and r["result"]["actual"].get("status") == "closed"]
    doc["actual_net"] = round(sum(x["net"] for x in act), 2) if act else None
    doc["actual_trades"] = len(act)
    doc["checks"] = {k: {"pass": sum(1 for r in recs if r["checks"].get(k) is True),
                         "fail": sum(1 for r in recs if r["checks"].get(k) is False),
                         "not_run": sum(1 for r in recs if r["checks"].get(k) is None)} for k in CHECKS}
    breaks = [r for r in taken if not all_passed(r)]
    missed = [r for r in skipped if all_passed(r)]
    doc["rule_breaks"] = [r["id"] for r in breaks]
    doc["skipped_with_all_checks_passed"] = [r["id"] for r in missed]
    if a.json:
        print(json.dumps(doc, indent=2))
        return 0
    f = lambda x: "—" if x is None else "%+.2fR" % x
    print("Journal %s to %s: %d signal%s on %d day%s, %d taken, %d skipped."
          % (days[0], days[-1], len(recs), "" if len(recs) == 1 else "s", len(days), "" if len(days) == 1 else "s",
             len(taken), len(skipped)))
    print("")
    print("%-10s %-11s %-8s %-28s %-9s %8s %10s" % ("date", "symbol", "decision", "checks (T R:R TM risk)", "by rules", "R", "your net"))
    for r in recs:
        ru = (r.get("result") or {}).get("rules") or {}
        ac = (r.get("result") or {}).get("actual") or {}
        marks = " ".join("ok" if r["checks"].get(k) is True else "FAIL" if r["checks"].get(k) is False else "--"
                         for k in CHECKS)
        print("%-10s %-11s %-8s %-28s %-9s %8s %10s" % (
            r["date"], r["symbol"], r["decision"], marks, ru.get("outcome") or "pending",
            ("%+.2f" % ru["R"]) if ru.get("R") is not None else "—",
            money(ac["net"]) if ac.get("net") is not None else "—"))
    print("")
    print("By the rules, after charges (fill at the next bar's open, stop before target):")
    print("  taken    %d scored, average %s" % (len(R(taken)), f(doc["avg_R_rules_taken"])))
    print("  skipped  %d scored, average %s   (what the skips would have made)" % (len(R(skipped)), f(doc["avg_R_rules_skipped"])))
    if act:
        print("Your recorded fills: %d closed trade%s, net %s after charges." % (len(act), "" if len(act) == 1 else "s",
                                                                              money(doc["actual_net"])))
    print("Checks: " + "; ".join("%s %d pass / %d fail / %d not run" % (k.replace("_", " "), v["pass"], v["fail"], v["not_run"])
                                 for k, v in doc["checks"].items()) + ".")
    if breaks:
        print("Rule breaks (taken with a failed or missing check): %s." % ", ".join(r["id"] for r in breaks))
    else:
        print("Rule breaks: none. Every trade taken had all four checks passed." if taken else "Rule breaks: none (no trades taken).")
    if missed:
        print("Skipped although every check passed: %s." % ", ".join("%s (%s)" % (r["id"], r.get("reason") or "no reason")
                                                                   for r in missed))
    pending = [r for r in recs if not (r.get("result") or {}).get("rules")]
    if pending:
        print("%d signal%s not resolved yet: run journal.py resolve after the close." % (len(pending), "" if len(pending) == 1 else "s"))
    if len(scored) < SMALL_SAMPLE:
        print("%d scored signal%s is too few to judge an edge (it takes about %d). What this sample does show: "
              "whether you followed your rules." % (len(scored), "" if len(scored) == 1 else "s", SMALL_SAMPLE))
    return 0


def cmd_list(a):
    j = load_journal(os.path.join(a.dir, "journal.json"))
    if not j["records"]:
        print("The journal is empty.")
        return 1
    for r in j["records"]:
        ru = (r.get("result") or {}).get("rules") or {}
        print("%s  %-11s %-8s %s%s" % (r["date"], r["symbol"], r["decision"],
                                       ("%s %+.2fR" % (ru["outcome"], ru["R"])) if ru.get("R") is not None
                                       else (ru.get("outcome") or "pending"),
                                       ("  — " + r["reason"]) if r.get("reason") else ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Trade journal: every signal, taken or skipped, scored after charges.")
    ap.add_argument("--dir", default="trader", help="the trader/ folder (default ./trader)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("log", help="record the current signal and what you did")
    p.add_argument("--taken", action="store_true")
    p.add_argument("--skip", metavar="WHY", help="you passed on it, and why")
    p.add_argument("--symbol", help="default: the symbol in trader/entry.json")
    p.add_argument("--date", help="YYYY-MM-DD (default: the signal's date)")
    p.add_argument("--news", choices=["clear", "flagged"], help="what the news check said")
    p.add_argument("--note")
    p.add_argument("--now")
    p = sub.add_parser("resolve", help="score finished sessions from Kite candles")
    p.add_argument("--all", action="store_true", help="score every record again")
    p.add_argument("--now")
    p = sub.add_parser("report", help="the review")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--json", action="store_true")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"log": cmd_log, "resolve": cmd_resolve, "report": cmd_report, "list": cmd_list}[a.cmd](a)
    except JournalError as e:
        print("journal.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
