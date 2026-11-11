#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan thousands of charts for setups. Standard library only.

    python3 scan.py universe                          # NSE EQUITY_L.csv -> trader/universe.txt
    python3 scan.py universe --list nifty500          # or just the Nifty 50/100/200/500 constituents
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols RELIANCE,TCS,HDFCBANK     # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 9:35 AM IST: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 AM IST
    python3 scan.py orb --offline wall5m.json.gz --now "2026-10-19 10:15" --min-price 0

universe  Downloads NSE's list of listed equities (EQUITY_L.csv from
          nsearchives.nseindia.com) or a Nifty index's constituents (--list
          nifty50 / nifty100 / nifty200 / nifty500) and keeps EQ-series
          stocks only: BE / BZ (trade-for-trade) and SME series cannot be
          traded intraday. For the BSE listing write the symbol with .BO,
          like RELIANCE.BO.

daily     Pre-market scan of daily candles from Zerodha Kite (about 5 months of
          1-day candles, one historical request per symbol). Liquidity: the
          last close >= --min-price (Rs 50) and the last session's volume >=
          --min-volume (200,000 shares). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily candle is dropped until the session's close.

orb       Opening-range breakouts on Kite 5-minute candles (real highs and
          lows). The range is the first --or-minutes (15) of the session:
          range high = the highest high of the range bars, a hit = the first
          completed bar to CLOSE above it, at that bar's close time. A chart
          is usable only when every 5-minute bar from 9:15 to the cutoff is
          there (a missing bar is a halt or no trades). Floors: --min-price
          Rs 50 (last close), --min-volume 0.

Bars: a bar counts only once it is complete (start + 5 minutes <= the clock).
Times are IST. --now pins the clock ("2026-10-19 10:15", IST; ...Z for UTC).

Data: Zerodha Kite Connect historical candles through trader-broker/kite.py
(log in once a day:  python3 trader-broker/kite.py login). Kite allows about 3
historical requests a second, so the Nifty 500 takes about 3 minutes and the
whole NSE list about 12. Partial results are reported as partial.

--offline PATH reads saved Kite candles instead of the network: universe wants
a saved NSE csv (or a folder with it); daily and orb take a folder of
<TICKER>.json files, each Kite's historical answer {"candles": [[time, o, h,
l, c, v], ...]}, or one bundle file {"RELIANCE": {"candles": [...]}, ...}.
.json.gz is fine.

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

import importlib.util as _ilu


def _load_kite():
    """trader-broker/kite.py, the one Kite client every skill shares (a sibling folder)."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(os.path.dirname(here), "trader-broker", "kite.py")
    spec = _ilu.spec_from_file_location("kite", path)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kite = _load_kite()

UA = "day-trader-skill-india/2.0 (python-urllib; personal use)"
NSE_URL = "https://nsearchives.nseindia.com/"
BROWSER_UA = "day-trader-skill-india/2.0 (python-urllib; personal use)"
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MIN_DAILY_BARS = 30
DEFAULT_UNIVERSE = "trader/universe.txt"
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

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

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


def kite_symbol(sym):
    """RELIANCE -> NSE:RELIANCE; RELIANCE.BO -> BSE:RELIANCE (Kite's instrument names)."""
    return kite.instrument(sym)


# ---------------------------------------------------------------- time

def ist_offset(utc=None):
    """Hours from UTC for India: always +5:30 (IST has no daylight saving)."""
    return 5.5


def to_ist(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)


def ist_to_utc(local):
    return local.replace(tzinfo=None) - datetime.timedelta(hours=5, minutes=30)



def ts_to_ist(ts):
    """Unix seconds -> naive IST. Never uses the machine's time zone."""
    return to_ist(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)))


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as IST."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-10-19 10:15" (IST) or ...Z (UTC)')
    if dt.tzinfo is None:
        return ist_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants an IST time like 10:15" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


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


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def plain_symbol(sym):
    """RELIANCE.NS / NSE:RELIANCE -> RELIANCE (the name trader/ files use); RELIANCE.BO keeps its .BO."""
    s = str(sym).strip().upper()
    if s.startswith("NSE:"):
        s = s[4:]
    elif s.startswith("BSE:"):
        s = s[4:] + ".BO"
    return s[:-3] if s.endswith(".NS") else s


# ---------------------------------------------------------------- parsing

def series_from_candles(rows):
    """Kite candles -> the plain series the scan rules read: {"t": unix seconds, "o","h","l","c","v", "meta"}.
    rows are kite.candles() dicts ({"dt": naive IST, ...}) or Kite's raw [time, o, h, l, c, v] lists."""
    t, o, h, l, c, v = [], [], [], [], [], []
    for r in rows:
        if isinstance(r, dict):
            dt, oo, hh, ll, cc, vv = r["dt"], r["o"], r["h"], r["l"], r["c"], r["v"]
        else:
            dt = datetime.datetime.fromisoformat(str(r[0]).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = (dt - dt.utcoffset()).replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)
            oo, hh, ll, cc, vv = r[1], r[2], r[3], r[4], r[5] if len(r) > 5 else 0
        t.append(int((ist_to_utc(dt) - datetime.datetime(1970, 1, 1)).total_seconds()))
        o.append(oo); h.append(hh); l.append(ll); c.append(cc); v.append(vv or 0)
    meta = {"last": c[-1] if c else None, "volume": v[-1] if v else None}
    return {"t": t, "c": c, "o": o, "h": h, "l": l, "v": v, "meta": meta}


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors. A Kite historical answer
    ({"candles": [...]}, optionally wrapped in {"status", "data"}) or a bundle of them."""
    if isinstance(doc, dict) and "data" in doc and isinstance(doc["data"], dict) and "candles" in doc["data"]:
        doc = doc["data"]
    if isinstance(doc, dict) and "candles" in doc:
        if not name_hint:
            raise ScanError("offline Kite candles without a symbol: name the file <TICKER>.json")
        sym = plain_symbol(name_hint)
        if doc["candles"]:
            data[sym] = series_from_candles(doc["candles"])
        else:
            errors[sym] = "no candles for %s" % sym
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not Kite candles or a bundle of them" % name_hint)


def load_offline(path):
    """A folder of <T>.json(.gz) files or one bundle file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            _absorb(doc, _stem(f), data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

NSE_LISTS = {
    "all": NSE_URL + "content/equities/EQUITY_L.csv",
    "nifty50": NSE_URL + "content/indices/ind_nifty50list.csv",
    "nifty100": NSE_URL + "content/indices/ind_nifty100list.csv",
    "nifty200": NSE_URL + "content/indices/ind_nifty200list.csv",
    "nifty500": NSE_URL + "content/indices/ind_nifty500list.csv",
}
INTRADAY_SERIES = {"EQ"}   # BE / BZ / SM etc. are trade-for-trade or SME: no intraday


def classify(row):
    """One NSE list row -> 'keep' or the rule that drops it."""
    sym = (row.get("SYMBOL") or row.get("Symbol") or "").strip().upper()
    series = (row.get("SERIES") or row.get(" SERIES") or row.get("Series") or "EQ").strip().upper()
    if not sym:
        return "no symbol"
    if series not in INTRADAY_SERIES:
        return "series %s (no intraday)" % series
    if not re.match(r"^[A-Z0-9&\-]{1,20}$", sym):
        return "not a plain NSE symbol"
    return "keep"


def parse_nse_csv(text):
    """NSE EQUITY_L.csv or an index constituents csv -> rows (dicts, stripped headers)."""
    import csv as _csv
    rdr = _csv.reader(io_lines(text))
    rows, hdr = [], None
    for rec in rdr:
        if not rec or not any(x.strip() for x in rec):
            continue
        if hdr is None:
            hdr = [h.strip() for h in rec]
            if "SYMBOL" not in [h.upper() for h in hdr]:
                raise ScanError("not an NSE symbol list (header %s)" % ",".join(hdr)[:60])
            hdr = [h.upper() if h.upper() in ("SYMBOL", "SERIES") else h for h in hdr]
            continue
        rows.append(dict(zip(hdr, [x.strip() for x in rec])))
    return rows


def io_lines(text):
    return text.replace("\r", "").split("\n")


def build_universe(text):
    """NSE csv text -> (kept [(SYM, name)], counts, None)."""
    kept, counts = {}, {}
    rows = parse_nse_csv(text)
    counts["rows"] = len(rows)
    for r in rows:
        why = classify(r)
        counts[why] = counts.get(why, 0) + 1
        if why == "keep":
            sym = (r.get("SYMBOL") or "").strip().upper()
            kept[sym] = " ".join((r.get("NAME OF COMPANY") or r.get("Company Name") or "").split())
    return sorted(kept.items()), counts, None


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes (standard Wilder smoothing)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_ist(now_utc)
    today = now.date()
    s = session(today)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_ist(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_volume):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = daily_closes(s, now_utc)
        meta = s.get("meta") or {}
        price = meta.get("last")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("volume")
        if price is None or price < min_price or (min_volume > 0 and (vol is None or vol < min_volume)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                         "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                         "volume": int(vol) if vol is not None else None})
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most dollars traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff):
    """5-minute bars of `day` that are complete at `cutoff` (naive IST),
    on the grid, with a close. Sorted, one per slot."""
    s = session(day)
    close_t = s[1] if s else CLOSE
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        et = ts_to_ist(ts)
        if et.date() != day or et.second or et.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if et.time() < OPEN or et.time() >= close_t:
            continue  # pre-market, after hours, the 3:30 PM closing print
        if et + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[et] = {"start": et, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range):
    """Every slot from 9:15 to the cutoff is there, and at least the range."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    s = session(day)
    if s:
        want = min(want, int((datetime.datetime.combine(day, s[1]) - slot(day, 0)).total_seconds()
                             // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None):
    """-> (day, cutoff naive IST) or raise ScanError with the plain message."""
    now = to_ist(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    ok, why = trading_day(day)
    if not ok:
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = session(day)
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s IST)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s IST on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s IST: the %d-minute opening range runs 9:15 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s IST, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(data, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0):
    """data = {SYM: series} of 5-minute Kite candles (real highs and lows).
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0}
    hits, failed = [], {}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        bars = session_bars(s, day, cutoff)
        if not usable(bars, day, cutoff, n_range):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars)
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if not orb_candidate(bars, n_range):
            continue
        counts["candidates"] += 1
        counts["confirmed"] += 1
        h = orb_hit(bars, n_range)
        if h:
            hits.append(dict({"symbol": sym, "setups": ["orb"]}, **h))
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_candles(symbols, interval, frm, to, a=None):
    """One Kite historical request per symbol (about 3 a second). -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    try:
        kite.access_token()
    except kite.KiteError as e:
        raise ScanError(str(e))
    n = len(symbols)
    for k, sym in enumerate(symbols):
        try:
            rows = kite.candles(sym, interval, frm=frm, to=to)
        except kite.KiteError as e:
            msg = str(e)
            if "session" in msg or "api key" in msg or "TokenException" in msg:
                raise ScanError(msg)
            failed[sym] = msg
            continue
        if rows:
            data[sym] = series_from_candles(rows)
        else:
            failed[sym] = "no candles from Kite"
        if a is not None and n > 100 and (k + 1) % 100 == 0:
            progress(a, "  %d of %d charts" % (k + 1, n))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    else:
        path = a.universe or DEFAULT_UNIVERSE
        if not os.path.exists(path):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % path)
        syms = symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def money(x):
    return "₹{:,.2f}".format(x)


def price_cap(a, cfg):
    """The highest share price worth listing: --max-price, else the config's order cap divided by
    min_shares (so at least that many whole shares fit). None = no cap."""
    if getattr(a, "max_price", None):
        return float(a.max_price), "--max-price"
    try:
        n = int(cfg.get("min_shares") or 0)
        cap = float(cfg.get("max_order_inr") or 0)
    except (TypeError, ValueError):
        return None, ""
    if n >= 1 and cap > 0:
        return cap / n, "config: %d share%s inside the ₹%s order cap" % (n, "" if n == 1 else "s", "{:,.0f}".format(cap))
    return None, ""


def apply_price_cap(hits, cap):
    kept = [h for h in hits if h["price"] <= cap]
    return kept, len(hits) - len(kept)


# ---------------------------------------------------------------- commands

def cmd_universe(a):
    t0 = time.time()
    which = a.list
    if a.offline:
        p = a.offline if os.path.isfile(a.offline) else os.path.join(a.offline, os.path.basename(NSE_LISTS[which]))
        if not os.path.exists(p):
            raise ScanError("no %s" % p)
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    else:
        try:
            req = urllib.request.Request(NSE_LISTS[which], headers={"User-Agent": BROWSER_UA, "Accept": "text/csv,*/*"})
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise ScanError("nseindia.com answered HTTP %d for the %s list" % (e.code, which))
        except (urllib.error.URLError, OSError) as e:
            raise ScanError("could not reach nseindia.com: %s" % getattr(e, "reason", e))
    kept, counts, _ = build_universe(text)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\n" % (sym, name))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"list": which, "count": len(kept), "rows": counts.get("rows", 0),
                          "dropped": dropped, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("NSE %s list: %s rows." % (which, "{:,}".format(counts.get("rows", 0))))
        print("Kept %s NSE equities (EQ series, intraday allowed)." % "{:,}".format(len(kept)))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                          for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily candles from Kite for %s symbols (about %d seconds)..."
                 % ("{:,}".format(len(symbols)), int(len(symbols) * kite.HIST_GAP) + 1))
        local = to_ist(now)
        data, failed = fetch_candles(symbols, "1d", local - datetime.timedelta(days=150), local, a)
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, a.min_volume)
    cap, cap_why = price_cap(a, read_config(a.config))
    too_dear = 0
    if cap:
        hits, too_dear = apply_price_cap(hits, cap)
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "offline" if a.offline else "kite",
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume},
           "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    local = to_ist(now)
    print("Daily scan, %s IST, closes through %s (%s daily candles, closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing",
        "saved" if a.offline else "Zerodha Kite"))
    print("Fetched %s of %s charts%s. %s passed the liquidity filter (₹%s+ and %s+ shares)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"]), _n(a.min_price),
                           "{:,}".format(a.min_volume),
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    if too_dear:
        print("Left out %d match%s priced above %s (%s)." % (too_dear, "" if too_dear == 1 else "es", money(cap), cap_why))
    if hits:
        per = [(k, sum(1 for h in hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(hits), "" if len(hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
        print("Most traded first (price x volume):")
    else:
        print("No setups.")
    for h in hits[:a.show]:
        print("  %-6s %10s  RSI %5.1f  %s" % (h["symbol"], money(h["price"]), h["rsi"],
                                              ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER.")
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or "14:45", "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_ist(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day)
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "5-minute candles from Kite for %s symbols (about %d seconds)..."
                 % ("{:,}".format(len(symbols)), int(len(symbols) * kite.HIST_GAP) + 1))
        data, failed = fetch_candles(symbols, "5m", datetime.datetime.combine(day, OPEN), cutoff, a)
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume)
    cap, cap_why = price_cap(a, cfg)
    too_dear = 0
    if cap:
        hits, too_dear = apply_price_cap(hits, cap)
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s IST" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "offline" if a.offline else "kite", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s IST: %d-minute range (9:15 to %s), 5-minute candles, %s."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, a.or_minutes // BAR_MIN)),
             "saved" if a.offline else "Zerodha Kite"))
    print("Fetched %s of %s charts%s; %s usable%s; %s closed at or above the range's highest close, %s checked against the range high (real highs and lows)."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / volume floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if too_dear:
        print("Left out %d breakout%s priced above %s (%s)." % (too_dear, "" if too_dear == 1 else "s", money(cap), cap_why))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s:"
              % (len(hits), "{:,}".format(counts["scanned"]), clock_label(cutoff)))
        for h in hits[:a.show]:
            print("  %8s  %-6s %10s  range %s to %s  %+.2f%% over" % (
                h["at"], h["symbol"], money(h["price"]), _p(h["or_low"]), _p(h["or_high"]), h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER and /trader-entry TICKER.")
    return 0 if hits else 1


def _p(x):
    return "{:,.2f}".format(x)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): daily setups and opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download NSE's equity list -> trader/universe.txt")
    p.add_argument("--list", default="all", choices=sorted(NSE_LISTS),
                   help="all = every NSE EQ-series stock (~2,000); or a Nifty index's constituents")
    p.add_argument("--out", default=DEFAULT_UNIVERSE)
    p.add_argument("--offline", metavar="PATH", help="a saved EQUITY_L.csv / ind_nifty*list.csv, or a folder with it")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated tickers instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default %s)" % DEFAULT_UNIVERSE)
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price)
        p.add_argument("--min-volume", type=int, default=min_volume)
        p.add_argument("--max-price", type=float,
                       help="leave out stocks above this price (default: config max_order_inr / min_shares)")
        p.add_argument("--config", default="trader/config.json")
        p.add_argument("--offline", metavar="PATH", help="saved Kite candles (folder or bundle) instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" IST, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 50.0, 200000)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 5.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--at", help="cut the clock at this IST, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, 14:45)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
