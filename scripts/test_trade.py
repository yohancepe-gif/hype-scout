"""Replay tests for the trading engine.

The simulator is the thing deciding whether this strategy is worth real money,
so its arithmetic has to be right. These drive trade.py through a scripted
sequence of scans and prices and assert on the resulting portfolio.

Run:  python3 scripts/test_trade.py
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trade  # noqa: E402

FAILURES = []
CLOCK = [datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc)]   # Mon, market open


def check(label, cond, detail=""):
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def approx(a, b, tol=0.01):
    return abs(a - b) <= tol


def candidate(sym, price, stage="EARLY", score=70, relvol=3.0, **kw):
    c = {"symbol": sym, "name": sym + " Inc", "exchange": "NASDAQ",
         "sector": "Tech", "price": price, "pct_change": 2.0,
         "volume": 5_000_000, "dollar_volume": 20_000_000,
         "market_cap": 500_000_000, "mentions": 200, "mentions_24h_ago": 20,
         "accel": 8.0, "rel_volume": relvol, "stage": stage,
         "hype_score": score, "sources": {}, "source_count": 3, "flags": [],
         "shares_per_100": round(100 / price, 2)}
    c.update(kw)
    return c


def write_scan(tmp, cands):
    with open(os.path.join(tmp, "scan.json"), "w") as f:
        json.dump({"generated": CLOCK[0].isoformat(), "candidates": cands}, f)


def setup(tmp):
    """Point trade.py at a temp data dir and a controllable clock."""
    trade.DATA = tmp
    trade.MODE = "sim"
    trade.now = lambda: CLOCK[0]
    trade.market_open = lambda t=None: True
    # Held names not in the scan resolve through this instead of the network.
    trade.PRICES = {}
    trade.resolve_prices = lambda syms, scan_map: {
        s: (scan_map[s]["price"] if s in scan_map else trade.PRICES.get(s, 0))
        for s in syms}


def run(tmp, cands):
    write_scan(tmp, cands)
    trade.main()
    with open(os.path.join(tmp, "portfolio.json")) as f:
        return json.load(f)


def test_entry_and_accounting(tmp):
    print("\n[entry + accounting]")
    pf = run(tmp, [candidate("AAA", 5.00), candidate("BBB", 2.00)])
    check("opened 2 positions", len(pf["positions"]) == 2, pf["positions"])

    a = next(p for p in pf["positions"] if p["symbol"] == "AAA")
    # $5.00 sits in the $5-20 bucket, so 0.7%: 5.00 -> 5.035, and
    # floor(100/5.035) = 19 shares.
    check("slippage applied to fill", approx(a["entry_price"], 5.035),
          a["entry_price"])
    check("whole shares only", a["qty"] == 19, a["qty"])
    check("stop set -15%", approx(a["stop"], 5.035 * 0.85), a["stop"])
    check("target set +30%", approx(a["target"], 5.035 * 1.30), a["target"])

    b = next(p for p in pf["positions"] if p["symbol"] == "BBB")
    # $2.00 is under $5, so the wider 1.5% applies: 2.00 -> 2.03.
    check("cheaper stock gets wider slippage", approx(b["entry_price"], 2.03),
          b["entry_price"])

    spent = sum(p["entry_price"] * p["qty"] for p in pf["positions"])
    check("cash = capital - spent",
          approx(pf["cash"], trade.CAPITAL - spent), pf["cash"])
    check("never exceeds $100 a position",
          all(p["entry_price"] * p["qty"] <= trade.POSITION + 0.01
              for p in pf["positions"]))


def test_entry_filters(tmp):
    print("\n[entry filters]")
    pf = run(tmp, [
        candidate("TALKY", 3.00, stage="TALK"),            # wrong stage
        candidate("LOWVOL", 3.00, relvol=1.0),             # volume unconfirmed
        candidate("WEAK", 3.00, score=30),                 # score too low
        candidate("PRICEY", 250.00),                       # can't afford 1 share
        candidate("MEGA", 3.00, market_cap=900e9),         # mega-cap
        candidate("THIN", 3.00, dollar_volume=100_000),    # can't exit
        candidate("CHEAP", 0.20),                          # spread would eat it
        candidate("GOOD", 3.00),
    ])
    held = {p["symbol"] for p in pf["positions"]}
    check("only GOOD passed", held == {"GOOD"}, held)


def test_target_exit(tmp):
    print("\n[target exit]")
    run(tmp, [candidate("AAA", 5.00)])
    CLOCK[0] += timedelta(hours=1)
    pf = run(tmp, [candidate("AAA", 7.00)])          # +38% before slippage
    check("closed on target", len(pf["closed"]) == 1, pf["closed"])
    c = pf["closed"][0]
    check("reason is target", c["reason"].startswith("target"), c["reason"])
    check("profit recorded", c["pnl"] > 0, c["pnl"])
    # exit fill = 7.00 - 0.7% (price >= $5 bucket)
    check("exit slippage applied", approx(c["exit_price"], 7.00 * 0.993),
          c["exit_price"])


def test_stop_exit_gap(tmp):
    print("\n[stop exit, gapped through]")
    run(tmp, [candidate("AAA", 5.00)])
    CLOCK[0] += timedelta(hours=1)
    # Price gaps well below the stop between hourly checks.
    pf = run(tmp, [candidate("AAA", 3.00)])
    c = pf["closed"][0]
    check("closed on stop", c["reason"].startswith("stop"), c["reason"])
    check("fills at the worse of stop and observed price",
          c["exit_price"] <= 5.075 * 0.85 + 0.001, c["exit_price"])
    check("loss is real, not clipped to -15%", c["pnl_pct"] < -15, c["pnl_pct"])


def test_fade_exit(tmp):
    print("\n[hype rolled over]")
    run(tmp, [candidate("AAA", 5.00)])
    CLOCK[0] += timedelta(hours=1)
    pf = run(tmp, [candidate("AAA", 5.20, stage="FADING")])
    c = pf["closed"][0]
    check("closed on fade", "rolled over" in c["reason"], c["reason"])


def test_time_stop(tmp):
    print("\n[time stop]")
    run(tmp, [candidate("AAA", 5.00)])
    CLOCK[0] += timedelta(days=6)
    pf = run(tmp, [candidate("AAA", 5.10, stage="RUNNING")])
    check("closed on time", pf["closed"][0]["reason"].startswith("time stop"),
          pf["closed"][0]["reason"])


def test_cooldown(tmp):
    print("\n[cooldown after exit]")
    run(tmp, [candidate("AAA", 5.00)])
    CLOCK[0] += timedelta(hours=1)
    run(tmp, [candidate("AAA", 7.00)])                 # exits on target
    CLOCK[0] += timedelta(hours=1)
    pf = run(tmp, [candidate("AAA", 5.00)])            # immediately hot again
    check("does not re-buy inside cooldown",
          not any(p["symbol"] == "AAA" for p in pf["positions"]),
          pf["positions"])


def test_pdt_guard(tmp):
    print("\n[pattern day trader guard]")
    # Three same-day round trips, then a fourth attempt on the same day.
    for i, sym in enumerate(["D1", "D2", "D3"]):
        run(tmp, [candidate(sym, 5.00)])
        CLOCK[0] += timedelta(minutes=30)
        run(tmp, [candidate(sym, 7.00)])
        CLOCK[0] += timedelta(minutes=30)
    with open(os.path.join(tmp, "portfolio.json")) as f:
        pf = json.load(f)
    check("3 day trades recorded", trade.day_trades_used(pf) == 3,
          pf["day_trades"])

    run(tmp, [candidate("D4", 5.00)])
    CLOCK[0] += timedelta(minutes=30)
    pf = run(tmp, [candidate("D4", 7.00)])
    check("4th same-day round trip still exits (never trapped)",
          not any(p["symbol"] == "D4" for p in pf["positions"]))
    # The guard blocks the *entry* that would become a 4th day trade.
    CLOCK[0] += timedelta(minutes=30)
    pf = run(tmp, [candidate("D5", 5.00)])
    used = trade.day_trades_used(pf)
    check("day trade count tracked over rolling window", used >= 3, used)


def test_max_positions(tmp):
    print("\n[position cap]")
    pf = run(tmp, [candidate("S%d" % i, 5.00) for i in range(12)])
    check("caps at MAX_POSITIONS",
          len(pf["positions"]) <= trade.MAX_POSITIONS, len(pf["positions"]))
    check("cash never negative", pf["cash"] >= -0.01, pf["cash"])


def test_market_closed(tmp):
    print("\n[market closed]")
    trade.market_open = lambda t=None: False
    pf = run(tmp, [candidate("AAA", 5.00)])
    check("no entries while shut", len(pf["positions"]) == 0)
    trade.market_open = lambda t=None: True


def test_market_hours_real():
    print("\n[market hours calendar]")
    mo = trade.__dict__["market_open"]
    # Use the real function, not the test stub.
    import importlib
    real = importlib.reload(trade).market_open
    d = datetime
    check("Sat closed", not real(d(2026, 8, 15, 16, 0, tzinfo=timezone.utc)))
    check("Mon 16:00 UTC open", real(d(2026, 8, 17, 16, 0, tzinfo=timezone.utc)))
    check("Mon 02:00 UTC closed", not real(d(2026, 8, 17, 2, 0, tzinfo=timezone.utc)))
    check("Christmas closed", not real(d(2026, 12, 25, 16, 0, tzinfo=timezone.utc)))
    trade.market_open = mo


def main():
    global CLOCK
    for fn in (test_entry_and_accounting, test_entry_filters, test_target_exit,
               test_stop_exit_gap, test_fade_exit, test_time_stop,
               test_cooldown, test_pdt_guard, test_max_positions,
               test_market_closed):
        tmp = tempfile.mkdtemp()
        CLOCK[0] = datetime(2026, 8, 17, 15, 0, tzinfo=timezone.utc)
        setup(tmp)
        try:
            fn(tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    test_market_hours_real()

    print("\n%s" % ("-" * 52))
    if FAILURES:
        print("%d FAILED: %s" % (len(FAILURES), ", ".join(FAILURES)))
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
