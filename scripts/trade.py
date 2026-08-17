"""Hype Trader - acts on Hype Scout's EARLY signals, with the same decision
engine driving either a paper simulator or a real Alpaca account.

Runs right after each scan. Stdlib only.

  MODE=sim      (default) no broker, no account, no money. Simulates fills
                against real prices with a spread/slippage model.
  MODE=alpaca   sends real orders to Alpaca. Paper endpoint unless you
                explicitly point it at the live one AND confirm.

The point of sim mode is that the strategy has never been measured. Hype Scout
went live hours ago with an empty track record. Running the identical decision
engine on paper first is the only way to find out whether these signals are
worth money before any is at stake - and it costs nothing to run.

Writes:
  data/portfolio.json   cash, open positions, every closed trade, equity curve

WHAT THE SIMULATOR CANNOT DO, stated plainly:
  * It sees prices once an hour, so it cannot model an intraday stop fill. When
    a stop is breached it fills at the WORSE of (stop price, observed price),
    which is the pessimistic direction on purpose.
  * Its slippage model is an estimate, not your broker's actual fill.
  * It assumes your order does not move the price. At $100 a position that is
    roughly true; it stops being true well before $10,000.
  A simulator that flatters the strategy is worse than no simulator at all.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ------------------------------------------------------------------- config

CAPITAL = 500.00           # starting paper cash
POSITION = 100.00          # dollars committed per trade
MAX_POSITIONS = 5          # CAPITAL / POSITION

STOP_PCT = -15.0           # cut a loser here
TARGET_PCT = 30.0          # take profit here
TIME_STOP_DAYS = 5         # hype that hasn't paid in 5 days isn't going to

# Entry gates. Deliberately stricter than what the app displays: the app is for
# reading, this spends money.
MIN_SCORE = 45
MIN_RELVOL = 1.5           # volume must confirm - never trade a TALK
MIN_PRICE = 0.50           # under this the spread eats the trade
MIN_DOLLAR_VOL = 2_000_000  # must be exitable
MAX_MARKET_CAP = 10e9      # $100 of retail hype cannot move a mega-cap
COOLDOWN_DAYS = 3          # don't immediately re-buy a name we just exited
ENTRY_STAGES = ("EARLY",)  # RUNNING means the move already happened

# Whole shares only. Fractional orders cannot carry bracket legs at Alpaca,
# and a position with no broker-side stop is a position with no stop at all
# once this script stops running.
MAX_PRICE = POSITION       # must afford at least 1 share

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")

MODE = os.environ.get("HYPE_TRADER_MODE", "sim").strip().lower()

# US market holidays 2026. Trading outside RTH on these names is a bad idea
# even where the broker allows it - the spreads are enormous.
HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


# ------------------------------------------------------------------ helpers

def http(url, timeout=25, retries=2, headers=None, method="GET", body=None):
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    if data:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            print("  ! HTTP %s %s -> %s" % (e.code, url[:60], detail))
            return None
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError) as e:
            if attempt == retries - 1:
                print("  ! failed %s (%s)" % (url[:60], e))
                return None
            time.sleep(2)
    return None


def num(s):
    s = re.sub(r"[^0-9.\-]", "", str(s or ""))
    if s in ("", "-", ".", "-."):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def now():
    return datetime.now(timezone.utc)


def market_open(t=None):
    """US regular hours only: 09:30-16:00 ET, weekdays, not a holiday.

    ET is UTC-4 in summer and UTC-5 in winter; using -5 year round means the
    bot simply idles for the first half hour of some sessions rather than
    firing orders into a market that is shut.
    """
    t = t or now()
    if t.weekday() >= 5:
        return False
    if t.date().isoformat() in HOLIDAYS_2026:
        return False
    mins = t.hour * 60 + t.minute
    return 14 * 60 + 30 <= mins <= 21 * 60      # 14:30-21:00 UTC


def slippage_pct(price):
    """What a market order really costs you on the way in and out.

    These are estimates of half-spread plus impact for a small order. Cheap
    stocks are punishing: a $0.60 name routinely quotes 0.59/0.61, which is
    over 3% round trip before the stock has done anything.
    """
    if price < 1.00:
        return 3.0
    if price < 5.00:
        return 1.5
    if price < 20.00:
        return 0.7
    return 0.3


# ------------------------------------------------------------------- prices

def price_from_scan(scan):
    return {c["symbol"]: c for c in scan.get("candidates", [])}


def live_price(sym):
    """Held positions drop out of the top-30 scan; we still need their price."""
    d = http("https://api.nasdaq.com/api/quote/%s/info?assetclass=stocks" % sym)
    p = ((d or {}).get("data") or {}).get("primaryData") or {}
    return num(p.get("lastSalePrice"))


def resolve_prices(symbols, scan_map):
    out = {}
    for s in symbols:
        if s in scan_map and scan_map[s].get("price"):
            out[s] = scan_map[s]["price"]
        else:
            px = live_price(s)
            if px > 0:
                out[s] = px
            time.sleep(0.3)
    return out


# ---------------------------------------------------------------- portfolio

def load_portfolio():
    p = os.path.join(DATA, "portfolio.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                pf = json.load(f)
            for k, v in (("positions", []), ("closed", []), ("equity", []),
                         ("day_trades", []), ("log", [])):
                pf.setdefault(k, v)
            return pf
        except (json.JSONDecodeError, OSError):
            pass
    return {"mode": MODE, "start_capital": CAPITAL, "cash": CAPITAL,
            "positions": [], "closed": [], "equity": [], "day_trades": [],
            "log": []}


def save_portfolio(pf):
    with open(os.path.join(DATA, "portfolio.json"), "w") as f:
        json.dump(pf, f, indent=1)


def note(pf, msg):
    print("  " + msg)
    pf["log"].append({"ts": now().isoformat(timespec="seconds"), "msg": msg})
    pf["log"] = pf["log"][-120:]


# ----------------------------------------------------------- day-trade rule

def day_trades_used(pf, t=None):
    """FINRA pattern-day-trader rule: in a MARGIN account under $25,000 equity,
    a 4th day trade inside 5 rolling business days gets the account restricted.

    Buying and selling the same symbol on the same day is one day trade, which
    is exactly what "ride the hype and get out early" does. At $500 of capital
    this is the binding constraint on the whole strategy, so the bot counts its
    own day trades rather than trusting a broker field to exist.
    """
    t = t or now()
    cutoff = (t - timedelta(days=7)).date().isoformat()
    return len([d for d in pf["day_trades"] if d[0] >= cutoff])


def would_be_day_trade(pf, sym, t=None):
    today = (t or now()).date().isoformat()
    return any(p["symbol"] == sym and p["entry_ts"][:10] == today
               for p in pf["positions"])


# ------------------------------------------------------------------ signals

def exit_reason(pos, price, cand, t):
    """Why we would sell this position right now, or None to keep holding."""
    pct = (price - pos["entry_price"]) / pos["entry_price"] * 100
    if pct <= STOP_PCT:
        return "stop loss %.1f%%" % pct
    if pct >= TARGET_PCT:
        return "target hit %.1f%%" % pct
    if cand and cand.get("stage") in ("PEAKING", "FADING"):
        return "hype rolled over (%s)" % cand["stage"]
    held = (t - datetime.fromisoformat(pos["entry_ts"])).days
    if held >= TIME_STOP_DAYS:
        return "time stop, %d days" % held
    return None


def entry_candidates(pf, scan):
    """Everything the app shows, filtered down to what is worth $100."""
    held = {p["symbol"] for p in pf["positions"]}
    recent = {}
    for c in pf["closed"]:
        recent[c["symbol"]] = max(recent.get(c["symbol"], ""), c["exit_ts"])
    cutoff = (now() - timedelta(days=COOLDOWN_DAYS)).isoformat()

    out = []
    for c in scan.get("candidates", []):
        s = c["symbol"]
        if s in held or recent.get(s, "") > cutoff:
            continue
        if c.get("stage") not in ENTRY_STAGES:
            continue
        if c.get("hype_score", 0) < MIN_SCORE:
            continue
        if c.get("rel_volume", 0) < MIN_RELVOL:
            continue
        if not (MIN_PRICE <= c.get("price", 0) <= MAX_PRICE):
            continue
        if c.get("dollar_volume", 0) < MIN_DOLLAR_VOL:
            continue
        if c.get("market_cap", 0) > MAX_MARKET_CAP:
            continue
        out.append(c)
    out.sort(key=lambda c: -c["hype_score"])
    return out


# --------------------------------------------------------------- simulation

def sim_sell(pf, pos, price, reason, t):
    # A breached stop fills at the worse of the stop level and what we can
    # actually see, because an hourly snapshot cannot prove the price passed
    # through the stop gently.
    fill = price * (1 - slippage_pct(price) / 100)
    if reason.startswith("stop"):
        fill = min(fill, pos["entry_price"] * (1 + STOP_PCT / 100))
    proceeds = fill * pos["qty"]
    cost = pos["entry_price"] * pos["qty"]
    pf["cash"] += proceeds
    if pos["entry_ts"][:10] == t.date().isoformat():
        pf["day_trades"].append([t.date().isoformat(), pos["symbol"]])
    pf["closed"].append({
        "symbol": pos["symbol"], "qty": pos["qty"],
        "entry_price": round(pos["entry_price"], 4),
        "entry_ts": pos["entry_ts"],
        "exit_price": round(fill, 4),
        "exit_ts": t.isoformat(timespec="seconds"),
        "reason": reason,
        "pnl": round(proceeds - cost, 2),
        "pnl_pct": round((proceeds - cost) / cost * 100, 2),
        "entry_stage": pos.get("entry_stage", ""),
        "entry_score": pos.get("entry_score", 0),
    })
    pf["positions"] = [p for p in pf["positions"] if p is not pos]
    note(pf, "SELL %s x%d @ %.4f (%s) -> %+.2f" % (
        pos["symbol"], pos["qty"], fill, reason, proceeds - cost))


def sim_buy(pf, c, t):
    price = c["price"]
    fill = price * (1 + slippage_pct(price) / 100)
    qty = int(POSITION // fill)
    if qty < 1 or pf["cash"] < fill * qty:
        return False
    pf["cash"] -= fill * qty
    pf["positions"].append({
        "symbol": c["symbol"], "qty": qty, "entry_price": round(fill, 4),
        "entry_ts": t.isoformat(timespec="seconds"),
        "entry_stage": c["stage"], "entry_score": c["hype_score"],
        "stop": round(fill * (1 + STOP_PCT / 100), 4),
        "target": round(fill * (1 + TARGET_PCT / 100), 4),
    })
    note(pf, "BUY  %s x%d @ %.4f (score %d, relvol %.1fx)" % (
        c["symbol"], qty, fill, c["hype_score"], c.get("rel_volume", 0)))
    return True


# ------------------------------------------------------------------- alpaca

class Alpaca:
    """Thin REST client. Paper endpoint unless explicitly pointed elsewhere."""

    def __init__(self):
        self.key = os.environ.get("ALPACA_KEY_ID", "").strip()
        self.secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
        self.base = os.environ.get(
            "ALPACA_BASE_URL", "https://paper-api.alpaca.markets").strip().rstrip("/")
        if not self.key or not self.secret:
            raise SystemExit(
                "MODE=alpaca needs ALPACA_KEY_ID and ALPACA_SECRET_KEY set.")
        # Guard rail: the live endpoint requires saying so out loud.
        if "paper-api" not in self.base:
            if os.environ.get("HYPE_TRADER_ALLOW_LIVE") != "I_UNDERSTAND":
                raise SystemExit(
                    "Refusing to trade real money. ALPACA_BASE_URL is not the "
                    "paper endpoint and HYPE_TRADER_ALLOW_LIVE is not set to "
                    "I_UNDERSTAND.")
            print("  *** LIVE MONEY MODE - %s ***" % self.base)

    def _h(self):
        return {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

    def account(self):
        return http(self.base + "/v2/account", headers=self._h()) or {}

    def positions(self):
        return http(self.base + "/v2/positions", headers=self._h()) or []

    def buy_bracket(self, sym, qty, stop, target):
        return http(self.base + "/v2/orders", headers=self._h(), method="POST",
                    body={"symbol": sym, "qty": str(qty), "side": "buy",
                          "type": "market", "time_in_force": "day",
                          "order_class": "bracket",
                          "take_profit": {"limit_price": round(target, 2)},
                          "stop_loss": {"stop_price": round(stop, 2)}})

    def close(self, sym):
        return http("%s/v2/positions/%s" % (self.base, sym),
                    headers=self._h(), method="DELETE")


def run_alpaca(scan, scan_map):
    """Same decision engine, real orders. Broker state is the source of truth."""
    api = Alpaca()
    acct = api.account()
    if acct.get("trading_blocked") or acct.get("account_blocked"):
        raise SystemExit("Account is blocked; not trading.")

    equity = num(acct.get("equity"))
    cash = num(acct.get("cash"))
    # Alpaca reports these on the account; fall back to counting locally if a
    # given account type doesn't expose them.
    dt_count = acct.get("daytrade_count")
    flagged = acct.get("pattern_day_trader")
    print("  equity $%.2f  cash $%.2f  daytrades %s  PDT-flagged %s"
          % (equity, cash, dt_count, flagged))

    broker_pos = {p["symbol"]: p for p in api.positions()}
    t = now()

    # Exits the broker cannot do for us: stop and target are already resting as
    # bracket legs, so this only handles the signal and time exits.
    for sym, p in broker_pos.items():
        entry = num(p.get("avg_entry_price"))
        px = num(p.get("current_price")) or live_price(sym)
        cand = scan_map.get(sym)
        stage = (cand or {}).get("stage")
        if stage in ("PEAKING", "FADING"):
            print("  CLOSE %s - hype rolled over (%s)" % (sym, stage))
            api.close(sym)
        elif entry and px and (px - entry) / entry * 100 <= STOP_PCT * 1.5:
            print("  CLOSE %s - past stop and bracket leg didn't fire" % sym)
            api.close(sym)

    if not market_open(t):
        print("  market closed - no new entries")
        return

    if isinstance(dt_count, (int, float)) and equity < 25000 and dt_count >= 3:
        print("  holding off: %s day trades used, PDT limit is 3 under $25k"
              % dt_count)
        return

    room = MAX_POSITIONS - len(broker_pos)
    pf_stub = {"positions": [{"symbol": s, "entry_ts": t.isoformat()}
                             for s in broker_pos], "closed": []}
    for c in entry_candidates(pf_stub, scan)[:max(room, 0)]:
        if cash < POSITION:
            break
        qty = int(POSITION // c["price"])
        if qty < 1:
            continue
        stop = c["price"] * (1 + STOP_PCT / 100)
        target = c["price"] * (1 + TARGET_PCT / 100)
        r = api.buy_bracket(c["symbol"], qty, stop, target)
        print("  BUY %s x%d -> %s" % (c["symbol"], qty,
                                      (r or {}).get("status", "rejected")))
        cash -= c["price"] * qty


# --------------------------------------------------------------------- main

def mark_to_market(pf, prices):
    held = 0.0
    for p in pf["positions"]:
        px = prices.get(p["symbol"], p["entry_price"])
        # Stamp the mark on the position so the dashboard can show live P&L
        # without refetching every price in the browser.
        p["last_price"] = round(px, 4)
        p["pnl_pct"] = round((px - p["entry_price"]) / p["entry_price"] * 100, 2)
        p["pnl"] = round((px - p["entry_price"]) * p["qty"], 2)
        held += px * p["qty"]
    return pf["cash"] + held


def summarise(pf, equity):
    closed = pf["closed"]
    wins = [c for c in closed if c["pnl"] > 0]
    pcts = sorted(c["pnl_pct"] for c in closed)
    reasons = {}
    for c in closed:
        k = c["reason"].split(",")[0].split(" %")[0].strip()
        reasons[k] = reasons.get(k, 0) + 1
    return {
        "mode": pf.get("mode", MODE),
        "start_capital": pf["start_capital"],
        "equity": round(equity, 2),
        "total_return_pct": round(
            (equity - pf["start_capital"]) / pf["start_capital"] * 100, 2),
        "cash": round(pf["cash"], 2),
        "open_positions": len(pf["positions"]),
        "trades_closed": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "median_trade_pct": pcts[len(pcts) // 2] if pcts else 0,
        "best_pct": pcts[-1] if pcts else 0,
        "worst_pct": pcts[0] if pcts else 0,
        "total_pnl": round(sum(c["pnl"] for c in closed), 2),
        "exit_reasons": reasons,
        "day_trades_5d": day_trades_used(pf),
    }


def main():
    scan_path = os.path.join(DATA, "scan.json")
    if not os.path.exists(scan_path):
        raise SystemExit("no data/scan.json - run scan_hype.py first")
    with open(scan_path) as f:
        scan = json.load(f)
    scan_map = price_from_scan(scan)
    t = now()

    if MODE == "alpaca":
        print("Alpaca mode")
        run_alpaca(scan, scan_map)
        return

    print("Simulator mode (no broker, no money)")
    pf = load_portfolio()
    pf["mode"] = "sim"

    prices = resolve_prices([p["symbol"] for p in pf["positions"]], scan_map)

    # Exits first, so the cash is available for today's entries.
    for pos in list(pf["positions"]):
        px = prices.get(pos["symbol"])
        if not px:
            continue
        pos["high_water"] = max(pos.get("high_water", pos["entry_price"]), px)
        why = exit_reason(pos, px, scan_map.get(pos["symbol"]), t)
        if why:
            sim_sell(pf, pos, px, why, t)

    if market_open(t):
        used = day_trades_used(pf)
        room = MAX_POSITIONS - len(pf["positions"])
        for c in entry_candidates(pf, scan)[:max(room, 0)]:
            if pf["cash"] < POSITION:
                break
            # Under $25k the PDT rule caps same-day round trips at 3 per
            # 5 business days. Leave the 4th unused rather than risk a
            # restriction on a real account running the same code.
            if used >= 3 and would_be_day_trade(pf, c["symbol"], t):
                note(pf, "skipped %s - would be a 4th day trade" % c["symbol"])
                continue
            sim_buy(pf, c, t)
    else:
        print("  market closed - holding")

    prices.update({p["symbol"]: scan_map[p["symbol"]]["price"]
                   for p in pf["positions"] if p["symbol"] in scan_map})
    equity = mark_to_market(pf, prices)
    pf["equity"].append([t.isoformat(timespec="seconds"), round(equity, 2)])
    pf["equity"] = pf["equity"][-500:]
    pf["closed"] = pf["closed"][-300:]
    pf["summary"] = summarise(pf, equity)
    save_portfolio(pf)

    s = pf["summary"]
    print("Equity $%.2f (%+.2f%%) · %d open · %d closed · win rate %.0f%%"
          % (s["equity"], s["total_return_pct"], s["open_positions"],
             s["trades_closed"], s["win_rate"]))


if __name__ == "__main__":
    main()
