# Hype Scout

Scans Reddit and 4chan for stocks whose mention count is **accelerating**, checks
whether trading volume agrees, and places each name on the hype curve — from
"nobody has noticed yet" to "the crowd is leaving".

No API keys. No accounts. No paid data. Runs itself on GitHub Actions.

**Live:** https://yohancepe-gif.github.io/hype-scout/

## What it actually measures

The signal is the *change* in mentions, not the number. A stock with 4,000
mentions that had 4,000 yesterday is a board favourite. A stock going `15 → 400`
is the thing worth looking at.

Every candidate gets a stage:

| Stage | Meaning |
|---|---|
| `EARLY` | Mentions 3×+ in 24h, price hasn't caught up, volume confirms |
| `RUNNING` | Hype and price climbing together — not early, crowd still arriving |
| `FRESH` | Just appeared in live posts, no 24h baseline yet |
| `TALK` | Loud posting, normal volume. People typing, nobody buying |
| `PEAKING` | Price up 25%+, chatter stopped growing |
| `FADING` | Mentions 45%+ off peak — the crowd has moved on |
| `LOUD` / `QUIET` | Context, not candidates |

## Data sources

| Source | What it gives | Notes |
|---|---|---|
| [ApeWisdom](https://apewisdom.io) | Mention counts across ~10 subreddits + 4chan, **plus `mentions_24h_ago`** | The acceleration signal comes free |
| Reddit RSS | Last 100 posts across 8 subs | The `.json` API returns 403 now; `.rss` still serves |
| 4chan `/biz/` | `catalog.json` | Wide open |
| Nasdaq | Price, volume, full listing universe | Also validates that a "ticker" is real |

Two things that took some fighting:

- **Reddit rate-limits anonymous RSS hard.** Fetching 8 subs individually gets
  `429` after the second one. The multireddit form (`r/a+b+c/new.rss`) returns
  all of them in a single request.
- **ApeWisdom publishes a top-100 per board.** A ticker sliding off the bottom
  reports zero mentions, which is *not* the same as nobody talking about it.
  Reading it that way invents a fake `-100% FADING` signal. Absent means
  unknown, and unknown never gets written into the time series.

## Installing it as an app

It's a PWA, so it installs from the browser with no App Store and no signing.

**Mac (Safari):** open the site → menu bar **File → Add to Dock** → Add.
**Mac (Chrome/Edge):** open the site → the **install icon** in the address bar.
**iPhone (Safari):** **Share** → **Add to Home Screen**.

You get its own icon, its own window with no browser chrome, and it keeps
working with no connection — `sw.js` is network-first, so an installed copy
never quietly shows a stale signal as if it were live. When it does fall back to
cache it says **"Offline — last saved scan"** across the top.

Icons are generated, not checked in by hand: `python3 scripts/make_icons.py`.

## The bot

`scripts/trade.py` acts on the `EARLY` signals. One decision engine, two backends:

| Mode | What it does |
|---|---|
| `sim` (default) | No broker, no account, no money. Simulates fills against real prices with a spread/slippage model. |
| `alpaca` | Sends real orders. Paper endpoint unless you explicitly point it elsewhere **and** set `HYPE_TRADER_ALLOW_LIVE=I_UNDERSTAND`. |

Rules: $100 a position, max 5 open, −15% stop, +30% target, sell on
`PEAKING`/`FADING`, time stop at 5 days. Entry needs `EARLY` + score ≥45 +
volume ≥1.5× average — a `TALK` (chatter without volume) never gets bought.

Three constraints that shaped the design:

- **Fractional shares can't have bracket legs at Alpaca.** No fractional means
  no broker-side stop, and a stop that only exists inside an hourly cron isn't a
  stop. So: whole shares only, which caps entries at $100/share.
- **PDT.** Under $25,000 in a margin account, FINRA allows **3 day trades per 5
  business days**. Buy-and-sell-same-day is exactly this strategy, so at $500 of
  capital this is the binding constraint. The bot counts its own and stops at 3.
- **The simulator must not flatter the strategy.** Breached stops fill at the
  *worse* of stop and observed price; sub-$1 names are charged 3% slippage.

Run it:

```bash
python3 scripts/trade.py && python3 scripts/test_trade.py
```

### Pointing it at Alpaca paper

You have to do these bits yourself — I can't create accounts or handle keys:

1. Sign up at [alpaca.markets](https://alpaca.markets) and open the **Paper
   Trading** dashboard (paper accounts are free and come with fake money).
2. Generate an API key pair. Copy both halves; the secret shows once.
3. In this repo: **Settings → Secrets and variables → Actions**
   - *Secrets* tab → add `ALPACA_KEY_ID` and `ALPACA_SECRET_KEY`
   - *Variables* tab → add `HYPE_TRADER_MODE` = `alpaca`
4. Actions → *Hype scan* → **Run workflow** to test it.

To switch to real money you would also have to change `ALPACA_BASE_URL` and set
`HYPE_TRADER_ALLOW_LIVE=I_UNDERSTAND`. The script refuses otherwise. Don't do
that until the Bot tab has 30+ closed trades to look at.

## Files

```
scripts/scan_hype.py   the scanner (stdlib only, Python 3.9+)
scripts/trade.py       the trading engine (sim or Alpaca)
scripts/test_trade.py  replay tests for the engine - run before trusting it
index.html             the phone app - reads data/*.json, no build step
data/scan.json         current candidates
data/history.json      per-ticker mention series + every call and its outcome
data/portfolio.json    the bot's cash, positions, closed trades, equity curve
.github/workflows/     hourly cron, 7am-7pm ET weekdays
```

Run it locally:

```bash
python3 scripts/scan_hype.py
```

## The honest part

`data/history.json` logs every `EARLY` and `RUNNING` flag and tracks it forward
for 21 days — **winners and losers both** — and the app publishes the aggregate.
It also simulates the actual strategy: buy the flag, sell the first time mentions
fall 45% off their peak.

Judge this on the Track record tab, not on the one winner you remember. The thing
that makes a pump profitable for whoever started it is that somebody buys it
later; if you found it on a public leaderboard you are somewhere in that queue,
and mention counts cannot tell you whether you are near the front or the back.

Not financial advice, and I'm not a licensed advisor.
