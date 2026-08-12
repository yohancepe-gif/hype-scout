"""Hype Scout - find stocks whose social mentions are accelerating, and track
where each one sits on the hype curve.

Runs hourly via GitHub Actions. Stdlib only - no dependencies, no API keys.

Sources (all keyless, all verified reachable):
  ApeWisdom   aggregated Reddit mention counts across ~10 subs + 4chan /biz/,
              with mentions_24h_ago -> acceleration comes free
  Reddit RSS  the .json API is 403 now, but the .rss feeds still serve. Gives
              the last ~25 posts per sub, fresher than any aggregator
  4chan /biz/ catalog.json, wide open
  Nasdaq      price / volume / universe, so a "ticker" has to be a real listing

Writes:
  data/scan.json     current candidates with a stage on the hype curve
  data/history.json  per-ticker mention time series + every call we've made
                     and what happened to it afterwards

The mention time series is the whole point. One snapshot tells you something is
loud. A series tells you whether it is getting louder or going quiet - and going
quiet while the price is still up is the only "get out" signal this thing has.
"""
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]

# ApeWisdom filters. Order matters only for display.
APE_FILTERS = [
    "all-stocks", "wallstreetbets", "stocks", "pennystocks", "Shortsqueeze",
    "options", "wallstreetbetsELITE", "SPACs", "StockMarket", "investing",
    "4chan",
]

# Subreddits pulled live over RSS. These skew towards the small-cap end on
# purpose - that is where a hundred dollars of hype actually moves a price.
RSS_SUBS = [
    "wallstreetbets", "pennystocks", "stocks", "Shortsqueeze", "Daytrading",
    "RobinHoodPennyStocks", "smallstreetbets", "StockMarket",
]

MIN_PRICE = 0.10
MAX_PRICE = 1000.00
MIN_DOLLAR_VOLUME = 500_000     # you have to be able to get back out
MIN_MENTIONS = 10               # below this it is one guy, not a crowd
FINAL_LIST = 30
POSITION = 100.0                # the size actually being traded

# Acceleration smoothing. Without this, 1 mention -> 17 reads as "17x" and
# every dead ticker on the board looks like a rocket. Adding K to both sides
# means a surge has to clear a real floor before the ratio moves.
ACCEL_K = 5.0

# Words that look like tickers and are not. The universe check catches most
# junk, but these are all real listings AND real English, so they need naming.
STOPWORDS = set("""
A I IT ON ALL ARE FOR CEO CFO USA YOLO DD WSB IMO ATH IV CPI FED AI EV PM AM ET
OTM ITM FD PT EOD TLDR GDP IRS SEC ETF IPO NYSE US UK EU OP EDIT TA RH HODL MOON
PUMP BUY SELL CALL PUTS PUT LOL WTF IMHO TIL PSA NGL IRL FYI BTW NOW NEW OUT UP
DOWN BIG RED GO GOOD BAD LOSS GAIN CASH HOLD FOMO EPS ROI YTD EOY QQQ ANY ONE TWO
THE AND BUT NOT YOU MY WE HE SHE HIS HER OUR SO IF AT AS BE OR TO OF IN IS WAS
CAN GET SEE HAS HAD WHO WHY HOW WHAT WHEN OK NO YES EX PER VS AKA ASAP NASDAQ
LFG WAGMI NGMI GG EOW EOM AH PMI FOMC QE ATM DCA HYSA ROTH IRA LLC INC CO LTD
BOT APE DFV GUH IBKR ETC MAX MIN AVG TOP LOW HIGH OPEN LONG SHORT SIZE RISK PLAY
""".split())


def _here():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DATA = os.path.join(_here(), "data")


# ---------------------------------------------------------------- fetching

def _open(url, timeout, retries, accept):
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": accept,
                      "Accept-Language": "en-US,en;q=0.9"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == retries - 1:
                print("  ! failed %s (%s)" % (url[:78], e))
                return None
            time.sleep(2 * (attempt + 1))
    return None


def get_json(url, timeout=45, retries=3):
    raw = _open(url, timeout, retries, "application/json")
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        print("  ! bad json %s (%s)" % (url[:78], e))
        return None


def get_text(url, timeout=30, retries=2):
    raw = _open(url, timeout, retries, "text/html,application/xml")
    return raw.decode("utf-8", "replace") if raw else None


def num(s):
    """'$1,234.50' -> 1234.5 ; '-4.2%' -> -4.2 ; junk -> 0.0"""
    s = re.sub(r"[^0-9.\-]", "", str(s or ""))
    if s in ("", "-", ".", "-."):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- universe

def is_tradeable_common(row):
    """Drop warrants, units, rights, preferreds - they don't behave like shares."""
    sym = (row.get("symbol") or "").strip().upper()
    name = (row.get("name") or "").lower()
    if any(w in name for w in ("warrant", " unit", "right", "preferred",
                               "depositary")):
        return False
    if re.search(r"[.\-/](W|U|R|P)[A-Z]?$", sym):
        return False
    if len(sym) == 5 and sym[-1] in ("W", "U", "R"):
        return False
    return True


def fetch_universe():
    """Every listed US ticker with price + volume. Doubles as the ticker
    validator: if a word isn't in here, it isn't a stock."""
    out = {}
    for ex in EXCHANGES:
        d = get_json("https://api.nasdaq.com/api/screener/stocks"
                     "?tableonly=false&download=true&exchange=%s" % ex)
        rows = (d or {}).get("data", {}).get("rows") or []
        print("  %s: %d tickers" % (ex, len(rows)))
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if not sym or sym in out or not is_tradeable_common(r):
                continue
            price = num(r.get("lastsale"))
            vol = num(r.get("volume"))
            out[sym] = {
                "symbol": sym,
                "name": (r.get("name") or "").replace(" Common Stock", "").strip(),
                "exchange": ex,
                "sector": r.get("sector") or "",
                "price": price,
                "pct_change": num(r.get("pctchange")),
                "volume": vol,
                "dollar_volume": price * vol,
                "market_cap": num(r.get("marketCap")),
            }
        time.sleep(1)
    return out


# ------------------------------------------------------------ social feeds

def fetch_apewisdom():
    """{ticker: {mentions, mentions_24h_ago, rank, rank_24h_ago, upvotes,
                 subs: {filter: mentions}}}"""
    agg = {}
    ok = 0
    for filt in APE_FILTERS:
        d = get_json("https://apewisdom.io/api/v1.0/filter/%s/page/1" % filt,
                     timeout=30, retries=2)
        results = (d or {}).get("results") or []
        if results:
            ok += 1
        for r in results:
            sym = (r.get("ticker") or "").strip().upper()
            if not sym:
                continue
            e = agg.setdefault(sym, {"mentions": 0, "mentions_24h_ago": 0,
                                     "upvotes": 0, "rank": 999,
                                     "rank_24h_ago": 999, "subs": {},
                                     "name": r.get("name") or ""})
            m = int(r.get("mentions") or 0)
            if filt == "all-stocks":
                # The aggregate row is authoritative for totals; per-sub rows
                # only tell us where the noise is coming from.
                e["mentions"] = m
                e["mentions_24h_ago"] = int(r.get("mentions_24h_ago") or 0)
                e["upvotes"] = int(r.get("upvotes") or 0)
                e["rank"] = int(r.get("rank") or 999)
                e["rank_24h_ago"] = int(r.get("rank_24h_ago") or 999)
            else:
                e["subs"][filt] = m
                if not e["mentions"]:
                    e["mentions"] = m
                    e["mentions_24h_ago"] = int(r.get("mentions_24h_ago") or 0)
        time.sleep(0.4)
    print("  apewisdom: %d/%d feeds, %d tickers" % (ok, len(APE_FILTERS), len(agg)))
    return agg


CASHTAG = re.compile(r"\$([A-Za-z]{1,5})\b")
BARE = re.compile(r"\b([A-Z]{2,5})\b")


def extract_tickers(text, universe, cashtagged=None):
    """Cashtags count on sight. Bare all-caps words only count if they are a
    real listing and not an English word - otherwise every 'CEO' is a ticker.

    Symbols written with an explicit $ are recorded in `cashtagged`: that is
    proof a human meant the stock and not the word, which is what rescues
    tickers like ALL or SO from the stopword list later.
    """
    found = {}
    for m in CASHTAG.findall(text):
        s = m.upper()
        if s in universe:
            found[s] = found.get(s, 0) + 2      # explicit cashtag, weight it
            if cashtagged is not None:
                cashtagged.add(s)
    for m in BARE.findall(text):
        if m in universe and m not in STOPWORDS:
            found[m] = found.get(m, 0) + 1
    return found


def fetch_reddit_rss(universe, cashtagged):
    """The .json endpoints 403 now; the .rss feeds still serve.

    One request, not eight: reddit's multireddit syntax (r/a+b+c) returns the
    newest posts across every sub in a single call. Hitting the subs one at a
    time gets 429'd after the second one - anonymous RSS is rate limited hard.
    This is the layer that sees a pump before any aggregator does.
    """
    counts, posts = {}, {}
    xml = get_text("https://www.reddit.com/r/%s/new.rss?limit=100"
                   % "+".join(RSS_SUBS))
    if not xml:
        print("  reddit rss: unavailable")
        return counts, posts, cashtagged

    entries = re.findall(r"<entry>(.*?)</entry>", xml, re.S)
    for ent in entries:
        title = re.search(r"<title>(.*?)</title>", ent, re.S)
        body = re.search(r'<content type="html">(.*?)</content>', ent, re.S)
        cat = re.search(r'<category[^>]*term="([^"]+)"', ent)
        sub = cat.group(1) if cat else "reddit"
        text = html.unescape(title.group(1) if title else "")
        blob = text + " " + re.sub(r"<[^>]+>", " ",
                                   html.unescape(body.group(1)) if body else "")
        for sym, n in extract_tickers(blob, universe, cashtagged).items():
            counts[sym] = counts.get(sym, 0) + n
            posts.setdefault(sym, [])
            if text and len(posts[sym]) < 3:
                posts[sym].append({"sub": sub, "title": text[:140]})
    print("  reddit rss: %d posts, %d tickers" % (len(entries), len(counts)))
    return counts, posts, cashtagged


def fetch_4chan(universe, cashtagged):
    """/biz/ is mostly crypto, but stock pumps show up there first sometimes."""
    counts = {}
    pages = get_json("https://a.4cdn.org/biz/catalog.json", timeout=30, retries=2)
    if not pages:
        return counts
    for page in pages:
        for th in page.get("threads", []):
            blob = html.unescape(re.sub(r"<[^>]+>", " ",
                                        (th.get("sub") or "") + " " +
                                        (th.get("com") or "")))
            for sym, n in extract_tickers(blob, universe, cashtagged).items():
                counts[sym] = counts.get(sym, 0) + n
    print("  4chan /biz/: %d tickers" % len(counts))
    return counts


# ------------------------------------------------------- price confirmation

def fetch_avg_volume(sym):
    """30-day average volume, so we can say whether the talk shows up in the
    tape. Cached per calendar day - it barely moves and the calls add up."""
    today = date.today()
    d = get_json("https://api.nasdaq.com/api/quote/%s/historical"
                 "?assetclass=stocks&fromdate=%s&todate=%s&limit=40"
                 % (sym, (today - timedelta(days=45)).isoformat(),
                    today.isoformat()), timeout=25, retries=2)
    rows = (d or {}).get("data", {}).get("tradesTable", {}).get("rows") or []
    vols, closes = [], []
    for b in rows[1:31]:
        v, c = num(b.get("volume")), num(b.get("close"))
        if v > 0:
            vols.append(v)
        if c > 0:
            closes.append(c)
    if not vols:
        return None
    return {"avg_volume_30d": sum(vols) / len(vols),
            "high_30d": max(closes) if closes else 0,
            "as_of": today.isoformat()}


# ------------------------------------------------------------------ scoring

def hype_score(c):
    """0-100, weighted towards ACCELERATION and whether the tape confirms it.

    A stock with 4000 mentions that had 4000 yesterday is not a pump, it is a
    permanent resident. A stock going 15 -> 400 is the thing being looked for.
    Volume gets the second-biggest weight because talk without buying is the
    single most common way this kind of scan is wrong.
    """
    accel = min(c["accel"] / 8.0, 1.0) * 30
    loud = min(c["mentions"] / 250.0, 1.0) * 12
    climb = min(max(c["rank_jump"], 0) / 60.0, 1.0) * 13
    tape = min(c["rel_volume"] / 5.0, 1.0) * 25 if c["rel_volume"] else 0
    breadth = min(c["source_count"] / 4.0, 1.0) * 10
    fresh = min(c["live_mentions"] / 12.0, 1.0) * 5
    # Size tilt: $100 of retail enthusiasm can move a $200M company. It cannot
    # move a $200B one, no matter how many people post about it.
    cap = c["market_cap"]
    small = 5 if (0 < cap < 2e9) else (2 if 0 < cap < 2e10 else 0)
    return round(accel + loud + climb + tape + breadth + fresh + small)


def classify(c, series):
    """Where on the hype curve is this, right now?

    series: [[iso_ts, mentions, price], ...] oldest first, this ticker only.
    """
    mentions = c["mentions"]
    peak = max([s[1] for s in series] + [mentions]) or 1
    off_peak = (mentions - peak) / peak * 100

    recent = [s[1] for s in series[-4:]]
    falling = len(recent) >= 2 and recent[-1] < recent[0] * 0.8

    # The aggregator only publishes a top-100 per board. A ticker that slides
    # off the bottom of that list reports zero mentions, which is not the same
    # as nobody talking about it - and reading it that way invents a -100%
    # "FADING" signal out of a truncated list. Absent means unknown.
    if not c["in_apewisdom"] or mentions < MIN_MENTIONS:
        if c["live_mentions"] >= 6:
            return ("FRESH",
                    "Just appeared in live Reddit/4chan posts (%d hits) with no "
                    "24h baseline to compare against yet. Earliest possible "
                    "signal, and the least confirmed one."
                    % c["live_mentions"], 0.0, peak)
        return ("QUIET", "On the board, not doing much.", 0.0, peak)

    # "Has this already run?" has two answers and the bigger one wins. A name
    # we started tracking an hour ago shows ~0% since flagged even if it is up
    # 16% on the day; a name we have held for a week can be flat today and
    # still be up 200% since we called it. Taking the max stops either blind
    # spot from reading as "hasn't moved yet".
    move = c["pct_change"]
    if len(series) >= 2 and c["first_price"] and c["pct_since_first_seen"] is not None:
        move = max(move, c["pct_since_first_seen"])

    accel, rv = c["accel"], c["rel_volume"]
    surging = accel >= 3 and mentions >= 15

    if len(series) >= 3 and off_peak < -45 and peak >= 15:
        stage = "FADING"
        note = ("Mentions are %.0f%% off their peak of %d. Whatever this was, "
                "the crowd has moved on." % (abs(off_peak), peak))
    elif move >= 25 and (falling or off_peak < -20 or accel < 1.5):
        stage = "PEAKING"
        note = ("Price is up %.0f%% and the chatter has stopped growing. "
                "Latecomers are the only buyers left here." % move)
    elif surging and 0 < rv < 1.2:
        stage = "TALK"
        note = ("Mentions %.0fx but volume is normal (%.1fx). People are "
                "posting, nobody is buying. This is the most common false "
                "alarm on this list." % (accel, rv))
    elif surging and move < 15:
        stage = "EARLY"
        note = ("Mentions %.0fx in 24h and the price has not caught up yet. "
                "This is the only stage where you are not the exit liquidity "
                "- and most of these still go nowhere." % accel)
    elif accel >= 1.8 and move >= 15 and not falling:
        stage = "RUNNING"
        note = ("Hype and price climbing together, %+.0f%% so far. You are not "
                "early, but the crowd is still arriving." % move)
    elif mentions >= 30:
        stage = "LOUD"
        note = ("Constantly discussed but not accelerating - a board favourite "
                "rather than a fresh move.")
    else:
        stage = "QUIET"
        note = "On the board, not doing much."
    return stage, note, round(off_peak, 1), peak


def build_flags(c):
    f = []
    if c["price"] < 1.0:
        f.append(("Sub-$1 - the bid/ask spread alone can cost 5-15% round trip",
                  "high"))
    if c["dollar_volume"] < 2_000_000:
        f.append(("Thin - under $2M traded today, easy to get stuck in", "high"))
    if c["rel_volume"] and c["rel_volume"] < 1.3 and c["accel"] >= 3:
        f.append(("Loud on Reddit but volume is normal - talk without buying",
                  "high"))
    if c["stage"] in ("PEAKING", "FADING"):
        f.append(("Hype curve is past its peak - this is the exit half of the "
                  "move, not the entry half", "high"))
    if c["pct_change"] > 40:
        f.append(("Already up %.0f%% today - you would be buying the spike"
                  % c["pct_change"], "high"))
    if c["shares_per_100"] < 1:
        f.append(("$100 buys %.2f shares - needs a broker that does fractionals"
                  % c["shares_per_100"], "med"))
    if c["market_cap"] and c["market_cap"] < 50_000_000:
        f.append(("Nano-cap - a single seller can move this 20%", "med"))
    if c["market_cap"] > 50e9:
        f.append(("Mega-cap - retail chatter doesn't move a company this big; "
                  "it's here because it's popular, not because it's squeezing",
                  "med"))
    if c["high_30d"] and c["price"] > c["high_30d"] * 1.8:
        f.append(("Already 80%+ above its 30-day range", "med"))
    if c["source_count"] >= 4:
        f.append(("Showing up on %d separate boards at once - either real news "
                  "or a coordinated push" % c["source_count"], "med"))
    return [{"text": t, "level": lv} for t, lv in f]


# ------------------------------------------------------------------ history

def load_history():
    p = os.path.join(DATA, "history.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                h = json.load(f)
                h.setdefault("tickers", {})
                h.setdefault("calls", [])
                return h
        except (json.JSONDecodeError, OSError):
            pass
    return {"tickers": {}, "calls": []}


def prune(history):
    """Keep the file small: 120 snapshots per ticker, drop names unseen for
    14 days, cap the call log."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    for sym in list(history["tickers"]):
        t = history["tickers"][sym]
        t["series"] = t.get("series", [])[-120:]
        if not t["series"] or t["series"][-1][0] < cutoff:
            if not any(c["symbol"] == sym and not c.get("final")
                       for c in history["calls"]):
                del history["tickers"][sym]
    history["calls"] = history["calls"][-400:]
    return history


def update_calls(history, universe, now_iso):
    """Every EARLY/RUNNING flag gets logged once and then followed forever.

    Including the ones that went to zero. A scanner that only shows you its
    winners is just a slower way of losing money.
    """
    for call in history["calls"]:
        if call.get("final"):
            continue
        u = universe.get(call["symbol"])
        if not u or u["price"] <= 0:
            continue
        px, entry = u["price"], call["price"]
        pct = (px - entry) / entry * 100
        call["last_price"] = round(px, 4)
        call["pct_now"] = round(pct, 1)
        call["best"] = round(max(call.get("best", pct), pct), 1)
        call["worst"] = round(min(call.get("worst", pct), pct), 1)
        call["last_checked"] = now_iso

        # Simulated exit: sell the first time the hype curve rolls over. This
        # is the actual strategy being tested, not a buy-and-hold fantasy.
        t = history["tickers"].get(call["symbol"], {})
        series = t.get("series", [])
        if call.get("exit_pct") is None and len(series) >= 3:
            peak = max(s[1] for s in series) or 1
            if series[-1][1] < peak * 0.55:
                call["exit_pct"] = round(pct, 1)
                call["exit_reason"] = "mentions fell 45% off peak"
                call["exit_date"] = now_iso[:10]

        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(call["ts"])).days
        if age >= 21:
            call["final"] = True
    return history


def summarise(history):
    done = [c for c in history["calls"] if c.get("pct_now") is not None]
    if not done:
        return {"tracked": 0}
    now = sorted(c["pct_now"] for c in done)
    exits = [c["exit_pct"] for c in done if c.get("exit_pct") is not None]
    out = {
        "tracked": len(done),
        "pct_up": round(sum(1 for x in now if x > 0) / len(now) * 100, 1),
        "median": round(now[len(now) // 2], 1),
        "average": round(sum(now) / len(now), 1),
        "best": round(now[-1], 1),
        "worst": round(now[0], 1),
        "median_peak_gain": round(
            sorted(c["best"] for c in done)[len(done) // 2], 1),
    }
    if exits:
        ex = sorted(exits)
        out["exit_on_fade"] = {
            "n": len(ex),
            "median": round(ex[len(ex) // 2], 1),
            "pct_up": round(sum(1 for x in ex if x > 0) / len(ex) * 100, 1),
            "total_on_100": round(sum(POSITION * x / 100 for x in ex), 2),
        }
    return out


# --------------------------------------------------------------------- main

def main():
    os.makedirs(DATA, exist_ok=True)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    print("Fetching universe...")
    universe = fetch_universe()
    print("  %d tradeable tickers" % len(universe))
    if len(universe) < 500:
        raise SystemExit("universe fetch failed - refusing to write a bad scan")

    print("Fetching social...")
    ape = fetch_apewisdom()
    cashtagged = set()
    rss, rss_posts, cashtagged = fetch_reddit_rss(universe, cashtagged)
    biz = fetch_4chan(universe, cashtagged)

    history = load_history()

    # Merge every source into one candidate set.
    syms = set(ape) | set(rss) | set(biz)
    rows = []
    for sym in syms:
        u = universe.get(sym)
        if not u:
            continue
        # ALL, SO, DC, ON... are real listings and real English. The upstream
        # aggregator counts the word; only an explicit $SYM proves the stock.
        if sym in STOPWORDS and sym not in cashtagged:
            continue
        if not (MIN_PRICE <= u["price"] <= MAX_PRICE):
            continue
        if u["dollar_volume"] < MIN_DOLLAR_VOLUME:
            continue

        a = ape.get(sym, {})
        # `mentions` is the ApeWisdom 24h aggregate ONLY. The live RSS/4chan
        # counts are tracked separately rather than added in: they already
        # overlap with the aggregate, and their availability varies run to run,
        # which would put fake steps in the time series the whole stage
        # classifier reads from.
        mentions = a.get("mentions", 0)
        prior = a.get("mentions_24h_ago", 0)
        live = rss.get(sym, 0) + biz.get(sym, 0)
        if mentions < MIN_MENTIONS and live < 6:
            continue

        srcs = {}
        for k, v in (a.get("subs") or {}).items():
            srcs[k] = v
        if rss.get(sym):
            srcs["reddit-live"] = rss[sym]
        if biz.get(sym):
            srcs["4chan"] = biz[sym]

        t = history["tickers"].get(sym, {})
        first_price = t.get("first_price")
        pct_since = (round((u["price"] - first_price) / first_price * 100, 1)
                     if first_price else None)

        rows.append({
            "symbol": sym,
            "name": u["name"] or a.get("name", ""),
            "exchange": u["exchange"],
            "sector": u["sector"],
            "price": round(u["price"], 4),
            "pct_change": round(u["pct_change"], 2),
            "volume": int(u["volume"]),
            "dollar_volume": int(u["dollar_volume"]),
            "market_cap": int(u["market_cap"]),
            "mentions": mentions,
            "mentions_24h_ago": prior,
            "in_apewisdom": sym in ape,
            "upvotes": a.get("upvotes", 0),
            "accel": round((mentions + ACCEL_K) / (prior + ACCEL_K), 1),
            "rank": a.get("rank", 999),
            "rank_jump": max(a.get("rank_24h_ago", 999) - a.get("rank", 999), 0)
                         if a.get("rank", 999) < 999 else 0,
            "live_mentions": live,
            "sources": srcs,
            "source_count": len(srcs),
            "posts": rss_posts.get(sym, []),
            "first_seen": t.get("first_seen"),
            "first_price": first_price,
            "pct_since_first_seen": pct_since,
            "shares_per_100": round(POSITION / u["price"], 2) if u["price"] else 0,
            "rel_volume": 0,
            "high_30d": 0,
        })

    # Rank on a provisional score, then only spend price-history calls on the
    # names that are actually in contention.
    for r in rows:
        r["hype_score"] = hype_score(r)
    rows.sort(key=lambda r: -r["hype_score"])
    shortlist = rows[:FINAL_LIST + 15]
    print("Confirming %d names against the tape..." % len(shortlist))

    today = date.today().isoformat()
    for i, r in enumerate(shortlist, 1):
        t = history["tickers"].setdefault(r["symbol"], {})
        cached = t.get("vol_cache")
        if not cached or cached.get("as_of") != today:
            cached = fetch_avg_volume(r["symbol"])
            if cached:
                t["vol_cache"] = cached
            time.sleep(0.3)
        if cached and cached.get("avg_volume_30d"):
            r["rel_volume"] = round(r["volume"] / cached["avg_volume_30d"], 1)
            r["high_30d"] = round(cached.get("high_30d") or 0, 4)
        if i % 15 == 0:
            print("  %d/%d" % (i, len(shortlist)))

    # Record this snapshot, then classify against the full series.
    for r in shortlist:
        t = history["tickers"].setdefault(r["symbol"], {})
        t.setdefault("first_seen", now_iso)
        t.setdefault("first_price", r["price"])
        t.setdefault("series", [])
        # Only record snapshots we actually measured. Writing a 0 for a run
        # where the ticker was simply off the leaderboard would put a fake
        # crash into the series that the stage classifier then believes.
        if (r["in_apewisdom"] and r["mentions"] > 0
                and (not t["series"] or t["series"][-1][0] != now_iso)):
            t["series"].append([now_iso, r["mentions"], r["price"]])
        t["series"] = t["series"][-120:]

        if r["first_seen"] is None:
            r["first_seen"] = t["first_seen"]
            r["first_price"] = t["first_price"]
            r["pct_since_first_seen"] = 0.0

        r["hype_score"] = hype_score(r)
        stage, note, off_peak, peak = classify(r, t["series"])
        r["stage"] = stage
        r["stage_note"] = note
        r["off_mention_peak"] = off_peak
        r["peak_mentions"] = peak
        r["series"] = [[s[0][5:16], s[1]] for s in t["series"][-24:]]
        r["flags"] = build_flags(r)

    order = {"EARLY": 0, "RUNNING": 1, "FRESH": 2, "TALK": 3, "PEAKING": 4,
             "FADING": 5, "LOUD": 6, "QUIET": 7}
    shortlist.sort(key=lambda r: (order.get(r["stage"], 9), -r["hype_score"]))
    final = shortlist[:FINAL_LIST]

    # Log new EARLY / RUNNING flags so they can be graded later.
    logged = {c["symbol"] for c in history["calls"] if not c.get("final")}
    for r in final:
        if r["stage"] in ("EARLY", "RUNNING") and r["symbol"] not in logged:
            history["calls"].append({
                "symbol": r["symbol"], "ts": now_iso, "stage": r["stage"],
                "price": r["price"], "mentions": r["mentions"],
                "hype_score": r["hype_score"],
            })
            logged.add(r["symbol"])

    history = update_calls(history, universe, now_iso)
    history["summary"] = summarise(history)
    history = prune(history)

    with open(os.path.join(DATA, "history.json"), "w") as f:
        json.dump(history, f, separators=(",", ":"))

    with open(os.path.join(DATA, "scan.json"), "w") as f:
        json.dump({
            "generated": now_iso,
            "universe_size": len(universe),
            "sources": {"apewisdom": len(ape), "reddit_live": len(rss),
                        "fourchan": len(biz)},
            "considered": len(rows),
            "candidates": final,
            "track_record": history["summary"],
            "recent_calls": sorted(
                [c for c in history["calls"] if c.get("pct_now") is not None],
                key=lambda c: c["ts"], reverse=True)[:40],
        }, f, indent=1)

    by_stage = {}
    for r in final:
        by_stage[r["stage"]] = by_stage.get(r["stage"], 0) + 1
    print("Wrote %d candidates: %s" % (len(final), by_stage))
    s = history["summary"]
    if s.get("tracked"):
        print("Track record: %d calls, %.1f%% up, median %.1f%%"
              % (s["tracked"], s["pct_up"], s["median"]))


if __name__ == "__main__":
    main()
