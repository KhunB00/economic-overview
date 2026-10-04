# Economic Overview

**Read it here: https://khunb00.github.io/world-briefing/**

Add that to your phone's Home Screen and it behaves like an app.

It rebuilds itself **every hour, 06:00–23:00 Bangkok time, on GitHub's servers** —
your own computer does not need to be on, awake, or even in the same country.

A daily macro dashboard built as one static HTML page. Markets, policy, economic
data and headlines — scannable in about five minutes, with depth underneath when
something catches your eye.

## You don't need to run anything

The page updates itself in the cloud. Just open the link above.

Everything below is for when you want to change how it works.

## Running it on this Mac (optional)

```bash
cd ~/world-briefing
./.venv/bin/python world_briefing.py --open
```

Takes about 20 seconds. Writes `world_briefing.html` in this folder.

| Flag | What it does |
|---|---|
| `--open` | Open the page in your browser when it's built |
| `--demo` | Build from fake data, no internet needed — for checking the layout |
| `--hours 48` | Widen the news window (useful on Mondays, after a quiet weekend) |
| `--check-data` | Print where every single number came from |

## Where it runs

`.github/workflows/briefing.yml` is the hourly runner. GitHub's timer is
best-effort, so builds can land a few minutes past the hour.

**One thing to know:** if nobody touches the repository for 60 days, GitHub
pauses the schedule and emails you. Opening the repo and clicking "Enable
workflow" restarts it. Pushing any change also resets the clock.

To rebuild right now instead of waiting for the hour:

```bash
gh workflow run briefing.yml --repo KhunB00/world-briefing
```

## Turn on the US data section

Three of the most useful rows — the **real yield**, **breakeven inflation** and
**credit spreads** — plus the whole US data board need a free key from FRED (the
St. Louis Fed's public database). It takes two minutes, never expires, and asks
for nothing but an email address.

1. Go to https://fred.stlouisfed.org/docs/api/api_key.html
2. Create an account, click **Request API Key**
3. Save it next to the script:

Because the briefing now runs in the cloud, the key goes into GitHub's encrypted
secrets, not a file:

```bash
gh secret set FRED_API_KEY --repo KhunB00/world-briefing
```

Paste the key when prompted. It is encrypted, never appears on the page, and is
not visible to anyone browsing the repository.

For local runs on this Mac as well:

```bash
echo "YOUR_KEY_HERE" > ~/world-briefing/fred_key.txt
```

That file is git-ignored, so it never leaves your machine.

## How to read the page

**Tier 1 — the 60-second read.** The tape, anything that moved unusually, and
the five stories that matter most.

**Tier 2 — the detail.** Full cross-asset tables with 1-day / 1-week / 1-month /
YTD columns, the oil forward curve, speculative positioning, Thai gold.

**Tier 3 — all headlines**, filterable by theme.

A few things worth knowing:

- **σ flags** (e.g. `2.7σ`) mark a move that is large against that instrument's
  own recent volatility. A 1% move in a calm market is a bigger deal than a 1%
  move in a wild one — this is what separates signal from noise.
- **Yields are shown in basis points**, not percent. 100 bp = 1 percentage point.
- **The oil curve** tells you whether the physical market is tight. Front month
  above later months (backwardation) = buyers paying up for barrels now.
- **A `$` on a link** means it's probably behind a paywall. When several outlets
  cover one story, the page always links the free one first.
- **"seen"** means the story appeared in an earlier briefing.

## When something breaks

It will, occasionally — this runs on free public data sources, and they change.
The page is built so that failures degrade visibly rather than silently:

- A price that fails shows the **last good value, greyed out and labelled**
  ("2 days old") — never a blank, never a wrong number.
- A dead feed is **skipped and named** in the footer.
- The **health line** at the bottom summarises everything:
  `21/22 feeds OK · 46/47 prices live · 1 from cache`

If that line degrades, send it to Claude along with the output of
`--check-data`. Expect this maybe two to four times a year.

### You get told — you don't have to check

Three alarms, covering three different ways this can fail:

**1. The script crashes.** GitHub emails you when a scheduled job fails. On by
default, nothing to set up.

**2. The data quietly rots.** The page never crashes on purpose — it falls back
to cached values and publishes anyway, which is right for you as a reader but
would hide a slow decline. So `health_gate.py` runs *after* publishing: the page
still goes out, but if the numbers look unhealthy the job fails and GitHub
emails you, naming the cause. Thresholds are at the top of that file — currently
75% of feeds, 85% of prices live, 20 stories minimum.

**3. It stops running altogether.** Nothing server-side can detect its own
absence — a job that never runs cannot email you. So the page checks itself: the
build time is embedded, and when you open the page your browser compares it with
the clock. Older than 10 hours shows an amber banner, older than 36 a red one.
This works even when everything else is dead, because it runs in your browser.

The 10-hour threshold allows for the overnight gap, since builds pause between
23:00 and 06:00.

## Files

| File | |
|---|---|
| `world_briefing.py` | The script. All config is at the top, meant to be edited. |
| `world_briefing.html` | The page it builds. Bookmark this. |
| `SCHEDULING.md` | How to run it automatically every hour |
| `market_cache.json` | Last good price for every instrument, plus a rolling local price history |
| `briefing_history.json` | Links already shown, so repeats get marked "seen" |
| `last_snapshot.json` | Recent builds' prices, for the "what changed" box |
| `fred_key.txt` | Your FRED key, if you add one. Not tracked, not shared. |

## Things you'll want to edit

All at the top of `world_briefing.py`:

- **`EVENTS`** — the "Coming up" box. Only the October FOMC meeting is pre-filled;
  add US CPI and jobs dates yourself from the official calendars, so nothing on
  the page is ever a guess.
- **`FEEDS`** — add or remove news sources, grouped by region.
- **`TICKERS`** — add or remove instruments.
- **`THEMES`** — the keywords behind the filter buttons.

## Sources

Prices from Yahoo Finance. Economic data from FRED (Federal Reserve Bank of
St. Louis). Positioning from the CFTC. News by RSS from each publisher.
Nothing is scraped; every headline links back to its source.

Education only, not financial advice.

## If git ever complains about conflicts

The cloud rebuilds hourly and commits its output. If you also run the script on
this Mac, both sides rewrite the same generated files and git sees a conflict.

Those files don't matter — the next build regenerates them. `.gitattributes`
tells git to stop fighting over them, which needs one setting per clone:

```bash
git config merge.keepcurrent.name "keep the checked-out version of generated files"
git config merge.keepcurrent.driver "true"
```

Simplest habit: because the cloud does everything now, you rarely need to run it
here at all. Before editing anything locally, start from what the cloud has:

```bash
cd ~/world-briefing && git fetch && git reset --hard origin/main
```
