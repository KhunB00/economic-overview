# Scheduling (superseded)

> **You don't need any of this.** The Economic Overview now rebuilds itself every
> hour on GitHub's servers — see `.github/workflows/briefing.yml`. Your Mac is
> not involved, which is why it keeps updating while you're travelling.
>
> This file is kept only for the case where you want a *local* scheduled build
> as well. The launchd job described below is **not installed**.

---

# Run the briefing automatically every hour (macOS)

Your Mac uses `launchd` for scheduled jobs. The file below is already written
for you at `com.worldbriefing.daily.plist` in this folder.

## Install it

```bash
cp ~/world-briefing/com.worldbriefing.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.worldbriefing.daily.plist
```

That's it. The briefing rebuilds **every hour on the hour, 06:00 to 23:00**
Bangkok time, so headlines stay current through the day. It rests overnight,
when markets are shut and nothing would change.

If your Mac is asleep at one of those times, macOS runs the job as soon as you
next wake it, so you always get a fresh page rather than a stale one.

## Check it worked

```bash
launchctl list | grep worldbriefing
```

A line starting with `-` and `0` means it's installed and last ran cleanly.
Logs go to `briefing.log` and `briefing.err` in this folder.

## Run it right now, without waiting

```bash
launchctl start com.worldbriefing.daily
```

## Change the time

Edit `~/Library/LaunchAgents/com.worldbriefing.daily.plist`. It contains one
`<dict>` block per scheduled hour — delete blocks to run less often, or change
the `Minute` values to shift when in the hour it runs. Then reload:

```bash
launchctl unload ~/Library/LaunchAgents/com.worldbriefing.daily.plist
launchctl load ~/Library/LaunchAgents/com.worldbriefing.daily.plist
```

## Stop it

```bash
launchctl unload ~/Library/LaunchAgents/com.worldbriefing.daily.plist
rm ~/Library/LaunchAgents/com.worldbriefing.daily.plist
```

## Open the page each morning

The scheduled job builds the page but does not open a browser window. Bookmark
this in your browser and the bookmark always shows the latest build:

```
file:///Users/khunboo/world-briefing/world_briefing.html
```

If you'd rather it pop open automatically, add `<string>--open</string>` to the
`ProgramArguments` list in the plist, after the script path.

## Why this folder is not in Documents

macOS privacy protection (TCC) blocks background jobs from reading `~/Documents`,
`~/Desktop` and `~/Downloads`. When *you* run the script it works, because you
have granted yourself access interactively. When `launchd` runs it unattended at
07:30, it does not — and the job fails before Python even starts, with
`PermissionError: ... pyvenv.cfg`.

So the real folder lives at `~/world-briefing`, which is not restricted. There is
a shortcut at `~/Documents/world-briefing` pointing to it, so you can still find
it where you expect.

If you ever move this folder again, note that the virtual environment stores its
own absolute path and must be rebuilt, not moved:

```bash
rm -rf .venv && python3 -m venv .venv && ./.venv/bin/python -m pip install feedparser requests
```

Then update the paths in `com.worldbriefing.daily.plist` and reload it.
