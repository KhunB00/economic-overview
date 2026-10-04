#!/usr/bin/env python3
"""
Decide whether the briefing has quietly degraded.

The dashboard never crashes on purpose — it falls back to cached values and
publishes anyway. That is right for the reader, but it means a slow rot would
go unnoticed. This runs after publishing: the page always goes out, and if the
numbers behind it look unhealthy the job fails, which makes GitHub email you.

Thresholds are deliberately loose. One dead feed is normal; five is a signal.
"""
import json
import sys
from pathlib import Path

MIN_FEED_FRACTION = 0.75     # at least 3/4 of news feeds working
MIN_PRICE_FRACTION = 0.85    # at least 85% of instruments live (not cached)
MIN_STORIES = 20             # a near-empty briefing is a broken briefing

health_file = Path(__file__).parent / "health.json"
if not health_file.exists():
    print("health.json missing — the build did not finish properly")
    sys.exit(1)

h = json.loads(health_file.read_text())
problems = []

def ratio(ok, total):
    return (ok / total) if total else 1.0

feeds = ratio(h["feeds_ok"], h["feeds_total"])
prices = ratio(h["prices_live"], h["prices_total"])

if feeds < MIN_FEED_FRACTION:
    problems.append(
        f"news feeds: only {h['feeds_ok']}/{h['feeds_total']} working "
        f"({feeds:.0%}); failed: {', '.join(h['failed_feeds']) or 'unknown'}")
if prices < MIN_PRICE_FRACTION:
    problems.append(
        f"prices: only {h['prices_live']}/{h['prices_total']} live "
        f"({prices:.0%}) — the rest came from cache")
if h["stories"] < MIN_STORIES:
    problems.append(f"stories: only {h['stories']} after dedupe")
if h["fred_total"] and h["fred_ok"] == 0:
    problems.append("FRED: no series returned — key may be invalid or expired")
if h["cftc_ok"] == 0:
    problems.append("CFTC: positioning data unavailable")

summary = (f"feeds {h['feeds_ok']}/{h['feeds_total']} · "
           f"prices {h['prices_live']}/{h['prices_total']} · "
           f"FRED {h['fred_ok']}/{h['fred_total']} · "
           f"CFTC {h['cftc_ok']}/{h['cftc_total']} · "
           f"{h['stories']} stories · {h['calendar_events']} calendar events")

if problems:
    print("BRIEFING DEGRADED — the page still published, but:")
    for p in problems:
        print(f"  - {p}")
    print(f"\nFull health: {summary}")
    print("\nTo investigate, run locally:")
    print("  cd ~/world-briefing && ./.venv/bin/python world_briefing.py --check-data")
    sys.exit(1)

print(f"Healthy: {summary}")
