#!/usr/bin/env python3
"""Entry Timer for trader-entry: watches a setup bar by bar and says the
moment to get in. Standard library only. Long side only. It never places an order.

    python3 entry.py check ADANIENT                          # opening-range breakout, 5-minute bars
    python3 entry.py check ADANIENT --now "2026-11-11 10:20" --offline ADANIENT.json
    python3 entry.py check ADANIENT --setup level --level 2800.00    # first close above a price
    python3 entry.py check ADANIENT --interval 1m --min-volume-x 1.5 --write
    python3 entry.py watch ADANIENT --every 30 --until 15:30  # one line per closed bar
    python3 entry.py watchlist                            # every hit in trader/watchlist.json

Setups (v2 plans longs only):
  orb    opening-range breakout. The range is the high and low of the first
         --or-minutes (default 15: the 9:15, 9:20 and 9:25 five-minute bars).
         It triggers on the first COMPLETED bar after the range that CLOSES
         above the range high. A wick above the high is not a trigger.
  level  the first completed bar that closes above --level (for example a
         20-day high from trader-scanner).
  --min-volume-x X  the trigger bar also needs at least X times the average
         volume of the opening-range bars (the 9:15 bar carries the opening
         auction, so 1.0 is already a high bar).

A trigger's time is the trigger bar's CLOSE time (the 10:10 bar closes at
10:15 AM) and its price is that close, to the cent. Only bars on the interval
grid count and a bar counts only
once it has closed: now >= its start + the interval.

Statuses: triggered; waiting (not open yet, range still forming, or no close
above the level yet); expired (no trigger by no_entries_after, default 14:45;
on a 1:00 p.m. early close the cutoff moves up three hours with the close);
no-data (the market is closed today or Kite sent no candles).

trader/config.json (optional): "no_entries_before" (09:30) and
"no_entries_after" (14:45), IST.

watch wakes just after each bar closes (bars only change then), re-polls every
--every seconds (default 30) while Kite has not posted the new bar, prints one
line per closed bar and stops on a trigger (exit 0, writes trader/entry.json),
on expiry, at the close or at --until (exit 1). With --now or --offline it runs
on a simulated clock and replays the day without sleeping.

Data: Zerodha Kite Connect historical candles (1-minute or 5-minute, real
  highs and lows) and the live quote for the last trade time and the previous
  close, through trader-broker/kite.py (log in once a day:  kite.py login).
  --offline PATH reads saved Kite candles instead: Kite's historical answer
  {"candles": [[time, o, h, l, c, v], ...]} with optional "prev_close" and
  "data_time" (unix seconds), as .json or .json.gz, or PATH/<TICKER>.json when
  PATH is a folder. Without --now, an offline file is read as of its data_time
  (or its last candle's close).

Exit codes: check 0 = triggered, 1 = anything else or an error. watch 0 on a
trigger, 1 otherwise. watchlist 0 when at least one symbol triggered.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import importlib.util as _ilu


def _load_kite():
    """trader-broker/kite.py, the one Kite client every skill shares (a sibling folder)."""
    here = os.path.dirname(os.path.abspath(__file__))
    spec = _ilu.spec_from_file_location("kite", os.path.join(os.path.dirname(here), "trader-broker", "kite.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kite = _load_kite()

UA = "day-trader-skill-india/2.0 (python-urllib; personal use)"
PAUSE = 0.35     # seconds between symbols (Kite: 3 historical requests a second)
GRACE = 2        # seconds after a bar closes before watch asks for it
SETTLE = 60      # seconds a closed bar may wait for Kite's candle to catch up
INTERVALS = {"1m": 60, "5m": 300}
DEFAULTS = {"no_entries_before": "09:30", "no_entries_after": "14:45"}
EPOCH = datetime.datetime(1970, 1, 1)

NSE_HOLIDAYS = {
    # NSE equity segment trading holidays 2026 (NSE circular; BSE follows the same list).
    # Add the 2027 dates here once NSE publishes them (usually in December).
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
NSE_EARLY_CLOSE = {}  # NSE has no scheduled half days; Muhurat trading is a special evening session
CALENDAR_YEARS = (2026,)
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
EARLY = datetime.time(13, 0)   # unused: NSE has no scheduled early closes (NSE_EARLY_CLOSE is empty)


class EntryError(Exception):
    pass


# ---------------------------------------------------------------- time

def ist_offset(utc=None):
    """Hours from UTC for India: always +5:30 (IST has no daylight saving)."""
    return 5.5


def to_ist(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)


def ist_to_utc(local):
    return local.replace(tzinfo=None) - datetime.timedelta(hours=5, minutes=30)



def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def utc_of(ts):
    """Unix seconds -> naive UTC, without the machine's time zone."""
    return EPOCH + datetime.timedelta(seconds=int(ts))


def ts_of(utc):
    return int((utc.replace(tzinfo=None) - EPOCH).total_seconds())


def iso(utc):
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_now(text):
    """--now -> naive UTC. "2026-11-11 10:20" (or ...T10:15, or just 10:15 for
    today) is IST; an offset or Z makes it absolute."""
    if not text:
        return utc_now()
    t = text.strip()
    if t.upper().endswith(" IST"):
        t = t[:-3].strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        t = "%s %s" % (to_ist(utc_now()).date().isoformat(), t.zfill(5))
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise EntryError('--now wants an IST time like "2026-11-11 10:20" (or ...Z for UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, what):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise EntryError("%s wants an IST time like 15:30, not %r" % (what, text))
    return datetime.time(int(m.group(1)), int(m.group(2)))


def hm(t):
    """10:10, 09:45: the contract's wall-clock form."""
    return "%02d:%02d" % (t.hour, t.minute)


def short(t):
    """9:45, 10:10: for sentences."""
    return "%d:%02d" % (t.hour, t.minute)


def ampm(t):
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def trading_day(d):
    """(is_trading_day, reason). Weekends and NSE holiday list."""
    if d.weekday() >= 5:
        return False, "weekend"
    name = NSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NSE holiday: " + name
    return True, ""


def session(d):
    """(open_time, close_time) in IST, or None if closed all day."""
    ok, _ = trading_day(d)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in NSE_EARLY_CLOSE else CLOSE)


def next_open(d):
    nd = d + datetime.timedelta(days=1)
    for _ in range(15):
        if session(nd):
            return "%s 09:15" % nd.isoformat()
        nd += datetime.timedelta(days=1)
    return None


# ---------------------------------------------------------------- Kite

def plain_symbol(sym):
    """ADANIENT.NS / NSE:ADANIENT -> ADANIENT (the name trader/ files use); ADANIENT.BO keeps its .BO."""
    s = str(sym).strip().upper()
    if s.startswith("NSE:"):
        s = s[4:]
    elif s.startswith("BSE:"):
        s = s[4:] + ".BO"
    return s[:-3] if s.endswith(".NS") else s


def _unix(ist_dt):
    return int((ist_to_utc(ist_dt) - datetime.datetime(1970, 1, 1)).total_seconds())


def _ist_of(text):
    """Kite time text ('2026-11-11 15:59:28' IST, or ISO with +05:30) -> naive IST."""
    dt = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = to_ist((dt - dt.utcoffset()).replace(tzinfo=None))
    return dt


def chart_from_candles(sym, rows, prev_close=None, data_time=None, interval=None):
    """Kite candles (kite.candles() dicts or raw [time, o, h, l, c, v]) -> {symbol, meta, bars, prev_close, data_time}."""
    bars = []
    for r in rows:
        if isinstance(r, dict):
            dt, o, h, lo, c, v = r["dt"], r["o"], r["h"], r["l"], r["c"], r["v"]
        else:
            dt, o, h, lo, c = _ist_of(r[0]), r[1], r[2], r[3], r[4]
            v = r[5] if len(r) > 5 else 0
        if c is None:
            continue
        bars.append({"t": _unix(dt), "o": float(o), "h": float(h), "l": float(lo), "c": float(c), "v": int(v or 0)})
    bars.sort(key=lambda b: b["t"])
    if data_time is None and bars:
        data_time = bars[-1]["t"] + INTERVALS.get(interval or "5m", 300)
    return {"symbol": plain_symbol(sym), "meta": {"interval": interval}, "bars": bars,
            "prev_close": prev_close, "data_time": data_time}


def read_text(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def offline_file(path, sym):
    if os.path.isdir(path):
        for name in (sym + ".json", sym + ".json.gz"):
            p = os.path.join(path, name)
            if os.path.exists(p):
                return p
        raise EntryError("no offline file for %s in %s (want %s.json)" % (sym, path, sym))
    if not os.path.exists(path):
        raise EntryError("no offline file %s" % path)
    return path


def get_chart(sym, interval, day=None, offline=None, today=None):
    """Bars for one ticker on `day`, from Kite or a saved file of Kite candles."""
    sym = sym.upper()
    if offline:
        f = offline_file(offline, sym)
        try:
            doc = json.loads(read_text(f))
        except (OSError, ValueError) as e:
            raise EntryError("could not read %s: %s" % (f, e))
        if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
            doc = dict(doc["data"], **{k: v for k, v in doc.items() if k != "data"})
        if not isinstance(doc, dict) or "candles" not in doc:
            raise EntryError("%s is not saved Kite candles ({\"candles\": [...]})" % f)
        return chart_from_candles(sym, doc["candles"], doc.get("prev_close"), doc.get("data_time"), interval)
    today = today or to_ist(utc_now()).date()
    day = day or today
    if interval == "1m" and (today - day).days > 55:
        raise EntryError("Kite serves 1-minute candles for about 60 days: use --interval 5m for %s" % day)
    try:
        rows = kite.candles(sym, interval, frm=datetime.datetime.combine(day, datetime.time(9, 0)),
                            to=datetime.datetime.combine(day, datetime.time(15, 59)))
        prev_close, data_time = None, None
        if day == today:
            q = kite.quotes([sym]).get(sym) or {}
            prev_close = q.get("prev_close")
            if q.get("time"):
                data_time = _unix(_ist_of(q["time"]))
        else:
            daily = kite.candles(sym, "1d", frm=day - datetime.timedelta(days=12), to=day - datetime.timedelta(days=1))
            prev_close = daily[-1]["c"] if daily else None
    except kite.KiteError as e:
        raise EntryError(str(e))
    return chart_from_candles(sym, rows, prev_close, data_time, interval)


# ---------------------------------------------------------------- the rule

def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise EntryError("%s is not valid JSON: %s" % (path, e))
        for k in DEFAULTS:
            if doc.get(k):
                cfg[k] = doc[k]
    for k in DEFAULTS:
        parse_hhmm(cfg[k], k)
    return cfg


def r2(x):
    return None if x is None else round(float(x), 2) + 0.0   # + 0.0 turns -0.0 into 0.0


def early_cutoff(day, t, close_dt):
    """A cutoff on an early-close day moves up with the close: 15:30 (30 minutes
    before a 16:00 close) becomes 12:30 before a 1:00 p.m. close. A cutoff that
    is already before the early close stays."""
    at = datetime.datetime.combine(day, t)
    if at < close_dt:
        return at
    moved = at - (datetime.datetime.combine(day, CLOSE) - close_dt)
    return moved if moved.time() > OPEN else close_dt


def evaluate(chart, now_utc, setup="orb", or_minutes=15, interval="5m", level=None,
             min_volume_x=None, cfg=None):
    """Pure function: the setup's status at now_utc from one session's bars."""
    cfg = cfg or dict(DEFAULTS)
    if interval not in INTERVALS:
        raise EntryError("--interval is 5m or 1m")
    step = INTERVALS[interval]
    if or_minutes <= 0 or (or_minutes * 60) % step:
        raise EntryError("--or-minutes must be a whole number of %s bars" % interval)
    if setup not in ("orb", "level"):
        raise EntryError("--setup is orb or level")
    if setup == "level" and level is None:
        raise EntryError("--setup level needs --level PRICE (for example a 20-day high)")
    n_or = or_minutes * 60 // step
    local = to_ist(now_utc)
    day = local.date()
    res = {"symbol": plain_symbol(chart["symbol"]), "setup": setup, "side": "buy", "date": day.isoformat(),
           "interval": interval, "or_minutes": or_minutes, "now": local.strftime("%Y-%m-%d %H:%M:%S"), "status": "waiting",
           "phase": "", "reason": "", "opening_range": None, "level": None, "trigger": None,
           "last": None, "forming": None, "min_volume_x": min_volume_x,
           "prev_close": chart.get("prev_close"), "calendar_known": day.year in CALENDAR_YEARS, "_bars": []}
    dt = chart.get("data_time")
    res["data_time"] = to_ist(utc_of(dt)).strftime("%Y-%m-%d %H:%M:%S") if dt else None
    ok, why = trading_day(day)
    if not ok:
        res.update(status="no-data", phase="closed", reason="NSE is closed today (%s)" % why,
                   next_open=next_open(day))
        return res
    o, c = session(day)
    open_dt = datetime.datetime.combine(day, o)
    close_dt = datetime.datetime.combine(day, c)
    early = c != CLOSE
    before = datetime.datetime.combine(day, parse_hhmm(cfg["no_entries_before"], "no_entries_before"))
    cutoff = early_cutoff(day, parse_hhmm(cfg["no_entries_after"], "no_entries_after"), close_dt)
    or_end = open_dt + datetime.timedelta(minutes=or_minutes)
    res.update(opens=hm(o), closes=hm(c), early_close=NSE_EARLY_CLOSE.get(day.isoformat()),
               no_entries_before=hm(before), no_entries_after=hm(cutoff))

    by_slot = {}
    for b in chart["bars"]:
        if b["t"] % step:
            continue                       # a bar off the interval grid is not a bar
        et = to_ist(utc_of(b["t"]))
        if et.date() != day or not (open_dt <= et < close_dt):
            continue
        slot = int((et - open_dt).total_seconds()) // step
        by_slot[slot] = dict(b, et=et, end=et + datetime.timedelta(seconds=step), slot=slot)
    bars, pending = [], None
    now_ts = ts_of(now_utc)
    top = max(by_slot) if by_slot else -1
    for slot in sorted(by_slot):
        b = by_slot[slot]
        end_ts = b["t"] + step
        if end_ts <= now_ts:
            # Closed by the clock. On live data it counts once Kite has caught up: its last
            # trade is at or after the close, a later bar exists, or a minute has passed.
            if dt is None or dt >= end_ts or slot < top or now_ts >= end_ts + SETTLE:
                bars.append(b)
            else:
                pending = b
                res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
                res["lag_note"] = ("Kite's last trade is %s, before the %s bar's close: it counts once Kite "
                                   "catches up (a minute at most)" % (to_ist(utc_of(dt)).strftime("%H:%M:%S"),
                                                                       short(b["et"])))
        elif b["et"] <= local:
            res["forming"] = {"bar_start": hm(b["et"]), "closes": ampm(b["end"]), "price": r2(b["c"])}
    res["_bars"] = bars
    res["bars_closed"] = len(bars)

    if local < open_dt:
        res.update(phase="pre-open", reason="NSE opens at 9:15%s" % (
            " and the %d-minute range is set at %s" % (or_minutes, short(or_end)) if setup == "orb" else ""))
        return res

    or_bars = [b for b in bars if b["slot"] < n_or]
    avg_vol = (sum(b["v"] for b in or_bars) / float(len(or_bars))) if or_bars else None
    if or_bars:
        res["opening_range"] = {"high": r2(max(b["h"] for b in or_bars)), "low": r2(min(b["l"] for b in or_bars)),
                                "minutes": or_minutes, "bars": len(or_bars), "of": n_or,
                                "complete": local >= or_end, "avg_volume": int(round(avg_vol))}
    if setup == "orb":
        if local < or_end:
            res.update(phase="forming", reason="%d of %d range bars closed; the range is set at %s" % (
                len(or_bars), n_or, short(or_end)))
            return res
        if not or_bars:
            res.update(status="no-data", phase="open", reason="Kite sent no candles for the opening range")
            return res
        lvl = max(b["h"] for b in or_bars)
    else:
        lvl = float(level)
    res["level"] = r2(lvl)

    first_slot = n_or if setup == "orb" else 0
    trig, thin = None, []
    for b in bars:
        if b["slot"] < first_slot or b["end"] < before or b["end"] > cutoff:
            continue
        if b["c"] > lvl:
            vx = (b["v"] / avg_vol) if avg_vol else None
            if min_volume_x and (vx is None or vx < min_volume_x):
                thin.append(b)
                continue
            trig = b
            break
    last = bars[-1] if bars else None
    if last:
        res["last"] = {"bar_start": hm(last["et"]), "close": r2(last["c"]),
                       "vs_level_pct": r2(100.0 * (last["c"] / lvl - 1))}

    if trig:
        vx = (trig["v"] / avg_vol) if avg_vol else None
        res.update(status="triggered", phase="open" if local < close_dt else "after-close",
                   reason="the %s bar closed above %s" % (short(trig["et"]), "the range high" if setup == "orb" else "the level"))
        res["trigger"] = {"time": ampm(trig["end"]), "close_time": hm(trig["end"]), "bar_start": hm(trig["et"]),
                          "bar_index": trig["slot"], "price": r2(trig["c"]), "close": trig["c"],
                          "volume": trig["v"], "volume_x": r2(vx), "pct_over": r2(100.0 * (trig["c"] / lvl - 1))}
        res["minutes_since"] = int((local - trig["end"]).total_seconds() // 60)
        if dt and utc_of(dt) < ist_to_utc(trig["end"]):
            res["data_note"] = ("Kite's last trade is %s, before this bar closed: its close can still move a cent or two"
                                % to_ist(utc_of(dt)).strftime("%H:%M:%S"))
        return res
    if not bars:
        res.update(status="no-data", phase="open", reason="Kite sent no candles for %s yet" % day.isoformat())
        return res
    what = "close above the range high ₹%.2f" % lvl if setup == "orb" else "close above ₹%.2f" % lvl
    if local >= cutoff and not (pending and pending["end"] <= cutoff):
        res.update(status="expired", phase="after-close" if local >= close_dt else "after-cutoff",
                   reason="no %s by %s, the last time for a new entry%s" % (
                       what, short(cutoff), " (1:00 p.m. early close today)" if early else ""))
        return res
    res["phase"] = "open"
    if last["c"] > lvl and last in thin:
        res["reason"] = "the %s bar closed above at ₹%.2f but on %.2fx the range's volume, under your %.2fx" % (
            short(last["et"]), last["c"], last["v"] / avg_vol if avg_vol else 0, min_volume_x)
    elif last["c"] > lvl and last["end"] < before:
        res["reason"] = "the %s bar closed above at ₹%.2f, but no entries before %s (config)" % (
            short(last["et"]), last["c"], short(before))
    else:
        gap = lvl - last["c"]
        res["reason"] = "last close ₹%.2f (%s bar), ₹%.2f %s %s (%+.2f%%)" % (
            last["c"], short(last["et"]), abs(gap), "under" if gap >= 0 else "over",
            "the range high ₹%.2f" % lvl if setup == "orb" else "the level ₹%.2f" % lvl,
            100.0 * (last["c"] / lvl - 1))
    return res


def public(res):
    """The result without the internal bar list, ready for JSON."""
    return {k: v for k, v in res.items() if not k.startswith("_")}


def entry_doc(res, now_utc):
    """trader/entry.json, the contract shape (plus a plain-words reason)."""
    t = res.get("trigger") or {}
    orr = res.get("opening_range")
    return {"created_by": "trader-entry", "symbol": res["symbol"], "setup": res["setup"], "side": "buy",
            "date": res["date"], "status": res["status"], "time": t.get("time"), "bar_start": t.get("bar_start"),
            "bar_index": t.get("bar_index"), "price": t.get("price"), "level": res.get("level"),
            "interval": res["interval"],
            "opening_range": {"high": orr["high"], "low": orr["low"], "minutes": orr["minutes"]} if orr else None,
            "source": "kite", "checked_at": iso(now_utc), "reason": res.get("reason")}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- words

LABEL = {"triggered": "TRIGGERED", "waiting": "WAITING", "expired": "EXPIRED", "no-data": "NO DATA"}
PHASE_LABEL = {"pre-open": "NOT OPEN YET", "forming": "RANGE FORMING", "closed": "MARKET CLOSED"}
SHORT = {"pre-open": "not open", "forming": "forming", "closed": "closed"}


def setup_name(res):
    if res["setup"] == "orb":
        return "opening-range breakout (%d-minute range)" % res["or_minutes"]
    return "close above ₹%.2f" % res["level"] if res["level"] is not None else "close above a level"


def describe(res):
    """Plain lines for a person."""
    when = datetime.datetime.strptime(res["now"], "%Y-%m-%d %H:%M:%S")
    bars = "5-minute" if res["interval"] == "5m" else "1-minute"
    head = "%s  %s %s IST  %s on %s Kite candles" % (
        res["symbol"], when.strftime("%a %Y-%m-%d"), when.strftime("%H:%M"), setup_name(res), bars)
    if res.get("data_time") and res["data_time"] <= res["now"] and res["data_time"][:10] == res["date"]:
        head += ", data to %s" % res["data_time"][11:]
    out = [head]
    orr = res.get("opening_range")
    if orr:
        start = datetime.datetime.strptime(res["opens"], "%H:%M")
        end = start + datetime.timedelta(minutes=orr["minutes"])
        out.append("Opening range %s-%s: high ₹%.2f, low ₹%.2f (%d of %d bars%s)." % (
            short(start), short(end), orr["high"], orr["low"], orr["bars"], orr["of"],
            "" if orr["complete"] else ", still forming"))
    label = PHASE_LABEL.get(res["phase"]) or LABEL.get(res["status"], res["status"].upper())
    t = res.get("trigger")
    if t:
        out.append("TRIGGERED %s at ₹%.2f: the %s bar (bar %d) closed above %s ₹%.2f (%+.2f%%%s)." % (
            t["time"], t["price"], short(datetime.datetime.strptime(t["bar_start"], "%H:%M")), t["bar_index"],
            "the range high" if res["setup"] == "orb" else "the level", res["level"], t["pct_over"],
            (", %.2fx the range's volume" % t["volume_x"]) if res.get("min_volume_x") and t.get("volume_x") else ""))
        step = INTERVALS[res["interval"]] // 60
        if res.get("minutes_since", 0) > step and res.get("last"):
            out.append("That was %d minutes ago; the last close is ₹%.2f (%s bar). A late entry is a different "
                       "trade: the price, the stop distance and the odds have moved." % (
                           res["minutes_since"], res["last"]["close"], res["last"]["bar_start"]))
        if res.get("data_note"):
            out.append(res["data_note"] + ".")
        if res.get("minutes_since", 0) > step:
            out.append("Nothing was placed.")
        else:
            out.append("This is the moment to get in, if you take the trade. Nothing was placed.")
        out.append("Next: set the stop and target BEFORE you buy: /trader-exit "
                   "(exit.py plan --from trader/entry.json).")
    else:
        out.append("%s  %s." % (label, res["reason"]))
        f = res.get("forming")
        if res.get("lag_note") and res["status"] == "waiting":
            out.append(res["lag_note"] + ".")
        elif f and res["status"] == "waiting" and res["phase"] in ("open", "forming"):
            out.append("The %s bar is still forming (₹%.2f so far, closes %s); it does not count until it closes." % (
                f["bar_start"].lstrip("0"), f["price"], f["closes"]))
        if res["status"] == "expired":
            out.append("No entry on this setup today. Nothing was placed.")
        if res.get("next_open"):
            out.append("Next open: %s IST." % res["next_open"])
    if res.get("early_close"):
        out.append("Early close today at 1:00 p.m. (%s): no new entries after %s." % (
            res["early_close"], res["no_entries_after"]))
    if not res.get("calendar_known"):
        out.append("Holiday list covers %s only: check nseindia.com for this date." % " and ".join(map(str, CALENDAR_YEARS)))
    return out


def bar_line(b, res):
    """One watch line for a bar that just closed."""
    start = short(b["et"])
    orr = res.get("opening_range") or {}
    lvl = res.get("level")
    t = res.get("trigger")
    n_or = orr.get("of") or 0
    if res["setup"] == "orb" and b["slot"] < n_or:
        seen = [x for x in res["_bars"] if x["slot"] <= b["slot"]]
        note = "range so far ₹%.2f to ₹%.2f" % (min(x["l"] for x in seen), max(x["h"] for x in seen))
        if b["slot"] == n_or - 1:
            note = "opening range set: high ₹%.2f, low ₹%.2f" % (orr["high"], orr["low"])
    elif t and t["bar_index"] == b["slot"]:
        note = "TRIGGERED: closed above ₹%.2f (%+.2f%%). Get in now if you take it: %s, ₹%.2f" % (
            lvl, t["pct_over"], t["time"], t["price"])
    elif lvl is None:
        note = ""
    elif b["c"] > lvl:
        note = "above ₹%.2f" % lvl
        if res["setup"] == "level" or b["slot"] >= n_or:
            avg = orr.get("avg_volume")
            if res.get("min_volume_x") and avg:
                note += ", but volume %.2fx is under %.2fx" % (b["v"] / float(avg), res["min_volume_x"])
            elif b["end"] < datetime.datetime.combine(b["et"].date(), parse_hhmm(res["no_entries_before"], "x")):
                note += ", but no entries before %s" % res["no_entries_before"]
    else:
        note = "₹%.2f under ₹%.2f" % (lvl - b["c"], lvl)
    return "%5s bar  closed ₹%.2f  %s" % (start, b["c"], note)


# ---------------------------------------------------------------- watch

class SimClock(object):
    """A clock that moves only when it sleeps: replays a day without waiting."""

    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += datetime.timedelta(seconds=max(1, int(round(seconds))))


def watch_loop(provider, clock, sleep, emit, opts, every=30, until=None, max_errors=5, on_trigger=None):
    """Poll bar by bar until a trigger (0), expiry, the close or --until (1).
    provider(now_utc) -> chart, clock() -> naive UTC, sleep(seconds), emit(line).
    It sleeps in steps of at most `every` seconds and re-reads the clock each time,
    so a computer that dozed off catches up as soon as it wakes."""
    step = INTERVALS[opts.get("interval", "5m")]
    printed, errors, said_preopen, first = -1, 0, False, True
    next_fetch = None
    while True:
        now = clock()
        if next_fetch is not None and now < next_fetch:
            sleep(max(1, min(every, (next_fetch - now).total_seconds())))
            continue
        local = to_ist(now)
        day = local.date()
        s = session(day)
        if not s:
            emit("%s: market closed today (%s). Nothing to watch." % (day.isoformat(), trading_day(day)[1]))
            return 1, None
        open_dt = datetime.datetime.combine(day, s[0])
        close_dt = datetime.datetime.combine(day, s[1])
        if local < open_dt + datetime.timedelta(seconds=step):
            if not said_preopen:
                emit("%s IST  %s: watching from the first bar's close at %s." % (
                    local.strftime("%H:%M"), "not open yet" if local < open_dt else "open, no bar closed yet",
                    short(open_dt + datetime.timedelta(seconds=step))))
                said_preopen = True
            next_fetch = ist_to_utc(open_dt) + datetime.timedelta(seconds=step + GRACE)
            continue
        try:
            chart = provider(now)
            errors = 0
        except EntryError as e:
            errors += 1
            emit("%s IST  could not read bars: %s" % (local.strftime("%H:%M:%S"), e))
            if errors >= max_errors:
                emit("Stopping after %d failed reads in a row." % errors)
                return 1, None
            next_fetch = now + datetime.timedelta(seconds=every)
            continue
        res = evaluate(chart, now, **opts)
        bars = res["_bars"]
        lines = describe(res)
        fresh = first
        if first:
            emit(lines[0])
            if len(lines) > 1 and lines[1].startswith("Opening range"):
                emit(lines[1])
            if bars and not res.get("trigger"):
                emit(bar_line(bars[-1], res))
            printed = bars[-1]["slot"] if bars else -1
            first = False
        else:
            for b in bars:
                if b["slot"] > printed:
                    emit(bar_line(b, res))
                    printed = b["slot"]
        if res["status"] == "triggered":
            for line in lines[1:]:
                if line.startswith("Opening range") or (line.startswith("TRIGGERED") and not fresh):
                    continue
                emit(line)
            if on_trigger:
                on_trigger(res, now)
            return 0, res
        if res["status"] == "expired" or local >= close_dt:
            emit("EXPIRED  %s." % (res["reason"] or "the market closed without a trigger"))
            return 1, res
        if until and local >= until:
            emit("Stopped at --until %s without a trigger. %s." % (short(until), res["reason"]))
            return 1, res
        # sleep until just after the next bar closes; poll every `every` seconds if Kite is behind
        cur_slot = int((local - open_dt).total_seconds()) // step
        expected_last = cur_slot - 1
        behind = (not bars) or bars[-1]["slot"] < expected_last
        if behind:
            wait = every
        else:
            nxt = open_dt + datetime.timedelta(seconds=(cur_slot + 1) * step + GRACE)
            wait = (nxt - local).total_seconds()
        cutoff = datetime.datetime.combine(day, parse_hhmm(res.get("no_entries_after") or "14:45", "cutoff"))
        for stop_at in (until, cutoff, close_dt):
            if stop_at and stop_at > local:
                wait = min(wait, (stop_at - local).total_seconds())
        next_fetch = now + datetime.timedelta(seconds=max(1, wait))


# ---------------------------------------------------------------- commands

def _opts(a, cfg):
    if a.side in ("sell", "short"):
        raise EntryError("v2 plans longs only: no short entries. Watch for a buy (--side buy) instead.")
    if a.setup == "orb" and a.level is not None:
        raise EntryError("--level is for --setup level (an orb's level is its range high)")
    if a.min_volume_x is not None and a.min_volume_x <= 0:
        raise EntryError("--min-volume-x must be above 0")
    if a.level is not None and a.level <= 0:
        raise EntryError("--level must be a price above 0")
    return {"setup": a.setup, "or_minutes": a.or_minutes, "interval": a.interval, "level": a.level,
            "min_volume_x": a.min_volume_x, "cfg": cfg}


def _load(a, sym, now):
    """(chart, now). An offline file without --now is read as of its own time."""
    real_today = to_ist(utc_now()).date()
    if a.offline and now is None:
        ch = get_chart(sym, a.interval, offline=a.offline)
        return ch, (utc_of(ch["data_time"]) if ch.get("data_time") else utc_now())
    now = now or utc_now()
    return get_chart(sym, a.interval, to_ist(now).date(), a.offline, real_today), now


def cmd_check(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    chart, now = _load(a, sym, parse_now(a.now) if a.now else None)
    res = evaluate(chart, now, **opts)
    if a.write:
        write_json(a.write, entry_doc(res, now))
    if a.json:
        print(json.dumps(public(res), indent=2))
    else:
        for line in describe(res):
            print(line)
        if a.write:
            print("Wrote %s (status %s)." % (a.write, res["status"]))
    return 0 if res["status"] == "triggered" else 1


def cmd_watch(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    sym = a.symbol.upper()
    if a.every <= 0:
        raise EntryError("--every must be above 0 seconds")
    start = parse_now(a.now) if a.now else None
    if a.offline or start is not None:
        if start is None:
            start = _load(a, sym, None)[1]
        clock = SimClock(start)
        now_fn, sleep_fn = clock.now, clock.sleep
    else:
        now_fn, sleep_fn = utc_now, _sleep
    real_today = to_ist(utc_now()).date()

    def provider(now):
        return get_chart(sym, a.interval, to_ist(now).date(), a.offline, real_today)

    until = None
    if a.until:
        t = parse_hhmm(a.until, "--until")
        until = datetime.datetime.combine(to_ist(now_fn()).date(), t)

    def on_trigger(res, now):
        write_json(a.out, entry_doc(res, now))
        print("Wrote %s." % a.out, flush=True)

    def emit(line):
        print(line, flush=True)

    code, _ = watch_loop(provider, now_fn, sleep_fn, emit, opts, every=a.every, until=until,
                         on_trigger=on_trigger)
    return code


def cmd_watchlist(a):
    cfg = read_config(a.config)
    opts = _opts(a, cfg)
    if not os.path.exists(a.watchlist):
        raise EntryError("no watchlist at %s: run /trader-scanner first (it writes the hits there)" % a.watchlist)
    try:
        with open(a.watchlist, encoding="utf-8") as fh:
            doc = json.load(fh)
    except ValueError as e:
        raise EntryError("%s is not valid JSON: %s" % (a.watchlist, e))
    hits = [h for h in (doc.get("hits") or []) if isinstance(h, dict) and h.get("symbol")]
    if not hits:
        raise EntryError("no hits in %s: the scanner found nothing to watch" % a.watchlist)
    if a.offline and not os.path.isdir(a.offline):
        raise EntryError("watchlist --offline wants a folder of <TICKER>.json files")
    now = parse_now(a.now) if a.now else None
    rows = []
    for i, h in enumerate(hits):
        sym = str(h["symbol"]).upper()
        o = dict(opts)
        if a.setup == "level" and o["level"] is None:
            o["level"] = h.get("level")
        try:
            if a.setup == "level" and o["level"] is None:
                raise EntryError("needs a level: --level PRICE or a \"level\" in the watchlist")
            if i and not a.offline:
                _sleep(PAUSE)
            chart, when = _load(a, sym, now)
            rows.append((sym, evaluate(chart, when, **o), None))
        except EntryError as e:
            rows.append((sym, None, str(e)))
    if a.json:
        print(json.dumps([{"symbol": s, "result": public(r) if r else None, "error": e} for s, r, e in rows], indent=2))
    else:
        print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % ("SYMBOL", "STATUS", "TIME", "PRICE", "LEVEL", "LAST", "VS LEVEL"))
        for s, r, e in rows:
            if e:
                print("%-6s  error: %s" % (s, e))
                continue
            t = r.get("trigger") or {}
            last = r.get("last") or {}
            status = SHORT.get(r["phase"], r["status"]) if r["status"] != "triggered" else "triggered"
            print("%-6s  %-9s  %-8s  %-8s  %-9s  %-9s  %s" % (
                s, status[:9], t.get("time") or "-", ("₹%.2f" % t["price"]) if t else "-",
                ("₹%.2f" % r["level"]) if r.get("level") is not None else "-",
                ("₹%.2f" % last["close"]) if last else "-",
                ("%+.2f%%" % last["vs_level_pct"]) if last.get("vs_level_pct") is not None else "-"))
        n = sum(1 for _, r, _ in rows if r and r["status"] == "triggered")
        print("%d of %d triggered. Pick one, then: entry.py check <TICKER> --write, and /trader-exit." % (n, len(rows)))
        print("Zerodha Kite candles. Nothing was placed.")
    return 0 if any(r and r["status"] == "triggered" for _, r, _ in rows) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Entry Timer (trader-entry): watches a setup bar by bar.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--setup", choices=["orb", "level"], default="orb")
        p.add_argument("--or-minutes", type=int, default=15, help="opening range length (default 15)")
        p.add_argument("--interval", choices=["5m", "1m"], default="5m")
        p.add_argument("--level", type=float, help="the price to close above (--setup level)")
        p.add_argument("--min-volume-x", type=float, help="trigger bar volume vs the range bars' average")
        p.add_argument("--side", default="buy", choices=["buy", "long", "sell", "short"], help="v2: buy only")
        p.add_argument("--now", help='pretend it is this IST, e.g. "2026-11-11 10:20"')
        p.add_argument("--offline", metavar="PATH", help="saved Kite candles JSON (.json/.json.gz) or a folder")
        p.add_argument("--config", default="trader/config.json")

    p = sub.add_parser("check", help="the setup's status right now")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--write", nargs="?", const="trader/entry.json", metavar="PATH",
                   help="save the result (default trader/entry.json)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("watch", help="poll bar by bar until it triggers or expires")
    p.add_argument("symbol")
    common(p)
    p.add_argument("--every", type=float, default=30, help="seconds between polls while Kite is behind")
    p.add_argument("--until", help="stop at this IST (HH:MM)")
    p.add_argument("--out", default="trader/entry.json", help="where a trigger is written")
    p = sub.add_parser("watchlist", help="check every hit in trader/watchlist.json")
    common(p)
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"check": cmd_check, "watch": cmd_watch, "watchlist": cmd_watchlist}[a.cmd](a)
    except EntryError as e:
        print("entry.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("entry.py: stopped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
