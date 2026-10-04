# World Economy Briefing

A daily macro dashboard built as one static HTML page. Markets, policy, economic
data and headlines — scannable in about five minutes, with depth underneath when
something catches your eye.

## Run it

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

## Turn on the US data section

Three of the most useful rows — the **real yield**, **breakeven inflation** and
**credit spreads** — plus the whole US data board need a free key from FRED (the
St. Louis Fed's public database). It takes two minutes, never expires, and asks
for nothing but an email address.

1. Go to https://fred.stlouisfed.org/docs/api/api_key.html
2. Create an account, click **Request API Key**
3. Save it next to the script:

```bash
echo "YOUR_KEY_HERE" > ~/world-briefing/fred_key.txt
```

The key lives only in that file, never inside the script, so the script stays
safe to copy or share. The page tells you it's missing until you add it.

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

If that line degrades, run `--check-data` and send the output to Claude. Expect
this maybe two to four times a year.

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
