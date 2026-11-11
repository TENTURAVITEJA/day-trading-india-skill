#!/usr/bin/env python3
"""Zerodha Kite Connect client for trader-broker. Standard library only.

Read-only commands (nothing here places, changes or cancels an order):

    python3 kite.py login                 # opens Zerodha's login page; saves today's session
    python3 kite.py status                # is there a valid session?
    python3 kite.py profile               # whose account this is
    python3 kite.py margins               # equity funds available
    python3 kite.py quote RELIANCE TCS    # live quotes (NSE; write BSE:RELIANCE for BSE)
    python3 kite.py quote RELIANCE --write   # also write trader/prices.json
    python3 kite.py positions             # today's positions
    python3 kite.py orders                # today's orders
    python3 kite.py logout                # forget the saved session

Keys: KITE_API_KEY and KITE_API_SECRET in the environment (your shell profile),
never in a file in the project and never in a chat. The app's redirect URL on
developers.kite.trade must be http://127.0.0.1:8765/ .

login: opens https://kite.zerodha.com/connect/login in your browser. You log
in on zerodha.com with your own password and TOTP; Zerodha redirects to this
script on 127.0.0.1 with a one-time request token, which is exchanged for an
access token. The access token is saved to ~/.config/day-trader-india/kite_session.json
(readable only by you) and expires the next morning, so log in once each trading day.

The api secret, the request token and the access token are never printed.
"""

import argparse
import datetime
import hashlib
import http.server
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

API = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login?v=3&api_key=%s"
REDIRECT_HOST, REDIRECT_PORT = "127.0.0.1", 8765
SESSION = os.path.expanduser("~/.config/day-trader-india/kite_session.json")
UA = "day-trader-skill-india/0.1 (python-urllib; personal use)"


class KiteError(Exception):
    pass


def ist_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=5, minutes=30)


def keys():
    k, s = os.environ.get("KITE_API_KEY", "").strip(), os.environ.get("KITE_API_SECRET", "").strip()
    if not k or not s:
        raise KiteError("KITE_API_KEY / KITE_API_SECRET are not set in this shell. Add them to ~/.zshrc "
                        "and open a new terminal.")
    return k, s


# ---------------------------------------------------------------- session file

def load_session():
    try:
        with open(SESSION, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_session(doc):
    os.makedirs(os.path.dirname(SESSION), mode=0o700, exist_ok=True)
    fd = os.open(SESSION, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def session_expiry(login_ist):
    """Kite access tokens are flushed around 6:00 AM IST the next day."""
    t = datetime.datetime.fromisoformat(login_ist)
    nxt = t.replace(hour=6, minute=0, second=0, microsecond=0)
    if t >= nxt:
        nxt += datetime.timedelta(days=1)
    return nxt


def access_token():
    s = load_session()
    if not s or not s.get("access_token"):
        raise KiteError("no Kite session: run  kite.py login")
    if s.get("api_key") != keys()[0]:
        raise KiteError("the saved session is for a different api key: run  kite.py login")
    if ist_now() >= session_expiry(s["login_ist"]):
        raise KiteError("the Kite session from %s IST has expired (they end around 6 AM): run  kite.py login"
                        % s["login_ist"][:16])
    return s["access_token"]


# ---------------------------------------------------------------- http

def call(method, path, params=None, auth=True, form=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    headers = {"X-Kite-Version": "3", "User-Agent": UA}
    if auth:
        headers["Authorization"] = "token %s:%s" % (keys()[0], access_token())
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
            msg = "%s: %s" % (body.get("error_type", "error"), body.get("message", ""))
        except ValueError:
            msg = "HTTP %d" % e.code
        if e.code == 403:
            msg += " (session invalid or expired: run  kite.py login)"
        raise KiteError("Kite %s %s -> %s" % (method, path, msg))
    except (urllib.error.URLError, OSError) as e:
        raise KiteError("could not reach api.kite.trade: %s" % getattr(e, "reason", e))
    if doc.get("status") != "success":
        raise KiteError("Kite %s %s -> %s" % (method, path, doc.get("message", "unknown error")))
    return doc.get("data")


# ---------------------------------------------------------------- login

class _Catcher(http.server.BaseHTTPRequestHandler):
    result = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "request_token" in q or "status" in q:
            _Catcher.result = {k: v[0] for k, v in q.items()}
            ok = q.get("status", [""])[0] == "success" and "request_token" in q
            body = ("<h2>Logged in to Kite.</h2><p>You can close this tab and go back to the terminal.</p>" if ok
                    else "<h2>Kite login did not complete.</h2><p>Go back to the terminal.</p>")
        else:
            body = "<p>Waiting for Zerodha's redirect.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(("<html><body style='font-family:sans-serif'>%s</body></html>" % body).encode())

    def log_message(self, *a):   # never log the request line: it carries the request token
        pass


def cmd_login(a):
    api_key, api_secret = keys()
    url = LOGIN % urllib.parse.quote(api_key)
    try:
        srv = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _Catcher)
    except OSError as e:
        raise KiteError("cannot listen on %s:%d (%s). Is another login still running?" % (REDIRECT_HOST, REDIRECT_PORT, e))
    srv.timeout = 1
    print("Opening Zerodha's login page. Log in there with your own password and TOTP.")
    print("If the browser does not open, paste this into it:\n  %s" % url)
    print("Waiting up to %d seconds for the redirect to http://%s:%d/ ..." % (a.wait, REDIRECT_HOST, REDIRECT_PORT))
    sys.stdout.flush()
    if not a.no_browser:
        webbrowser.open(url)
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=a.wait)
    while _Catcher.result is None and datetime.datetime.now() < deadline:
        srv.handle_request()
    srv.server_close()
    res = _Catcher.result
    if res is None:
        raise KiteError("no redirect arrived in %d seconds. Check that the app's redirect URL on "
                        "developers.kite.trade is exactly http://%s:%d/ and try again."
                        % (a.wait, REDIRECT_HOST, REDIRECT_PORT))
    if res.get("status") != "success" or not res.get("request_token"):
        raise KiteError("Zerodha reported the login as %r" % res.get("status", "failed"))
    checksum = hashlib.sha256((api_key + res["request_token"] + api_secret).encode()).hexdigest()
    data = call("POST", "/session/token", auth=False,
                form={"api_key": api_key, "request_token": res["request_token"], "checksum": checksum})
    now = ist_now().replace(microsecond=0).isoformat()
    save_session({"api_key": api_key, "access_token": data["access_token"], "user_id": data.get("user_id"),
                  "user_name": data.get("user_name"), "login_ist": now})
    print("Logged in as %s (%s). Session saved; it lasts until about 6 AM IST tomorrow."
          % (data.get("user_name") or "?", data.get("user_id") or "?"))
    return 0


def cmd_status(a):
    s = load_session()
    if not s:
        print("No Kite session. Run: kite.py login")
        return 1
    try:
        access_token()
    except KiteError as e:
        print(str(e))
        return 1
    print("Kite session for %s (%s), logged in %s IST, valid until about %s IST."
          % (s.get("user_name") or "?", s.get("user_id") or "?", s["login_ist"][:16].replace("T", " "),
             session_expiry(s["login_ist"]).strftime("%Y-%m-%d %H:%M")))
    return 0


def cmd_logout(a):
    try:
        os.remove(SESSION)
        print("Forgot the saved Kite session.")
    except OSError:
        print("No saved session.")
    return 0


# ---------------------------------------------------------------- read-only commands

def money(x):
    return "₹{:,.2f}".format(float(x or 0))


def cmd_profile(a):
    p = call("GET", "/user/profile")
    if a.json:
        print(json.dumps(p, indent=2))
        return 0
    print("%s (%s), broker %s" % (p.get("user_name"), p.get("user_id"), p.get("broker")))
    print("Exchanges: %s" % ", ".join(p.get("exchanges") or []))
    print("Products:  %s" % ", ".join(p.get("products") or []))
    print("Order types: %s" % ", ".join(p.get("order_types") or []))
    return 0


def cmd_margins(a):
    m = call("GET", "/user/margins")
    if a.json:
        print(json.dumps(m, indent=2))
        return 0
    eq = m.get("equity") or {}
    av = eq.get("available") or {}
    used = eq.get("utilised") or {}
    print("Equity segment: net available %s" % money(eq.get("net")))
    print("  cash %s, opening balance %s, live balance %s, intraday pay-in %s"
          % (money(av.get("cash")), money(av.get("opening_balance")), money(av.get("live_balance")),
             money(av.get("intraday_payin"))))
    print("  used today: %s" % money(used.get("debits")))
    return 0


def instrument(sym):
    s = sym.strip().upper()
    if ":" in s:
        return s
    if s.endswith(".BO"):
        return "BSE:" + s[:-3]
    if s.endswith(".NS"):
        s = s[:-3]
    return "NSE:" + s


def cmd_quote(a):
    ins = [instrument(s) for s in a.symbols]
    q = call("GET", "/quote", params={"i": ins})
    if a.json:
        print(json.dumps(q, indent=2))
    missing = [i for i in ins if i not in q]
    prices = {}
    for i in ins:
        d = q.get(i)
        if not d:
            continue
        o = d.get("ohlc") or {}
        last, prev = d.get("last_price"), o.get("close")
        chg = (" (%+.2f%% vs prev close %s)" % (100.0 * (last / prev - 1), money(prev))) if last and prev else ""
        depth = d.get("depth") or {}
        bid = ((depth.get("buy") or [{}])[0] or {}).get("price")
        ask = ((depth.get("sell") or [{}])[0] or {}).get("price")
        ba = (" bid %s ask %s" % (bid, ask)) if bid and ask else ""
        sym = i.split(":", 1)[1] + (".BO" if i.startswith("BSE:") else "")
        prices[sym] = last
        if not a.json:
            print("%-14s %s%s  day %s-%s  vol %s%s  %s"
                  % (i, money(last), chg, o.get("low"), o.get("high"), "{:,}".format(d.get("volume") or 0), ba,
                     d.get("last_trade_time") or d.get("timestamp") or ""))
    for i in missing:
        print("no quote for %s (check the symbol)" % i, file=sys.stderr)
    if a.write and prices:
        os.makedirs(os.path.dirname(a.write) or ".", exist_ok=True)
        doc = {"as_of": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "source": "kite", "prices": prices}
        with open(a.write, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        if not a.json:
            print("Wrote %s." % a.write)
    return 0 if prices else 1


def cmd_positions(a):
    p = call("GET", "/portfolio/positions")
    if a.json:
        print(json.dumps(p, indent=2))
        return 0
    rows = p.get("day") or []
    if not rows:
        print("No positions today.")
        return 0
    for r in rows:
        print("%-6s %-14s %-4s qty %5s  avg %s  last %s  P&L %s"
              % (r.get("exchange"), r.get("tradingsymbol"), r.get("product"), r.get("quantity"),
                 money(r.get("average_price")), money(r.get("last_price")), money(r.get("pnl"))))
    return 0


def cmd_orders(a):
    rows = call("GET", "/orders") or []
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No orders today.")
        return 0
    for r in rows:
        print("%s  %-4s %5s %-14s %-6s %-4s %-10s filled %s @ %s  %s"
              % (r.get("order_timestamp"), r.get("transaction_type"), r.get("quantity"), r.get("tradingsymbol"),
                 r.get("order_type"), r.get("product"), r.get("status"), r.get("filled_quantity"),
                 r.get("average_price"), r.get("status_message") or ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Zerodha Kite Connect: login and read-only account / market data.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("login", help="log in on zerodha.com and save today's session")
    p.add_argument("--wait", type=int, default=300, help="seconds to wait for you to log in")
    p.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    p.set_defaults(fn=cmd_login)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("logout").set_defaults(fn=cmd_logout)
    for name, fn in (("profile", cmd_profile), ("margins", cmd_margins), ("positions", cmd_positions),
                     ("orders", cmd_orders)):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)
    p = sub.add_parser("quote", help="live quotes")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--write", nargs="?", const="trader/prices.json", metavar="FILE")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_quote)
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    try:
        return a.fn(a)
    except KiteError as e:
        print("kite.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
