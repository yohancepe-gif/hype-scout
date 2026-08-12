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

## Files

```
scripts/scan_hype.py   the scanner (stdlib only, Python 3.9+)
index.html             the phone app - reads data/scan.json, no build step
data/scan.json         current candidates
data/history.json      per-ticker mention series + every call and its outcome
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
