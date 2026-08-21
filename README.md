# tennisalert

Automated monitors that email you the moment something changes:

- `monitor.py` — USTA Chicago coaching programs (tennis classes).
- `ticket_monitor.py` — Beventi "The Love Hypothesis" screening ticket pages (high priority, near-real-time).

## Ticket monitor (beventi.co) — the urgent one

Watches these two pages for ANY change and emails immediately:

- https://beventi.co/tickets/o3kurinmey
- https://beventi.co/event/tlc-x-amazon-mgm-studios-early-screening-the-love-hypothesis-2026-us-758029431764

The screening ticket itself is **free** (not paid) — the page just marks it "Sold out" since it's a free RSVP-style ticket with a 2-per-person limit. There's no dollar price to track; the important signal is whether "Sold out" disappears.

Every change (countdown timers/tracking noise are filtered out first) triggers an email. If the "Sold out" text disappears or "Get Tickets/Buy Tickets" text appears, the subject line is flagged 🚨 urgent.

### Why it's near-instant and free forever

It runs locally on this Mac via `launchd` (`scripts/com.tennisalert.ticketmonitor.plist`), polling every **30 seconds**, all the time the laptop is on — no GitHub Actions minutes, no cloud cost, no quota to run out of. A lightweight backup copy also runs on GitHub Actions every 5 minutes (`.github/workflows/ticket_monitor.yml`) in case the laptop is asleep/off.

Manage the local job:

```bash
# check status
launchctl list | grep tennisalert

# stop it
launchctl unload ~/Library/LaunchAgents/com.tennisalert.ticketmonitor.plist

# start it again
launchctl load ~/Library/LaunchAgents/com.tennisalert.ticketmonitor.plist

# logs
tail -f state/ticket_monitor.log
```

Force an immediate test email:

```bash
python ticket_monitor.py --force
```

## Why GitHub Actions minutes ran out (tennis monitor)

The old workflow reinstalled a full Playwright + Chromium browser (with system deps) on **every** run, every 15 minutes — that's what burned through the free minutes quota, not the schedule itself. Playwright is only a last-resort fallback; the primary fetch path is a plain API call that needs no browser. The workflow now skips the Playwright install by default (only enable it manually via `workflow_dispatch` if the API path ever breaks), and the schedule was relaxed to every 30 minutes since tennis is lower priority right now. This alone should keep it comfortably inside the free tier.

If you still want truly unlimited/free Actions minutes regardless of what the workflow does, make the repo public — GitHub Actions is unlimited and free for public repositories (secrets stay hidden either way since they're stored as encrypted repo Secrets, never committed).

---

Automated monitor for USTA Chicago coaching programs that emails you when a class matching your criteria appears or changes availability.

## What it does

- Checks https://playtennis.usta.com/USTAChicago/Coaching on a schedule.
- Filters to your rules:
	- Venue: Hamlin Park, Humboldt Park, Katrina Adams Tennis Courts, Revere Park, Smith Park, Welles Park
	- Program Type: Adult
	- Skill Level: Beginner or All-level variants
	- Date window: 2026-08-14 through 2027-07-31
	- Must not be in the past
	- Weekend: any time
	- Weekday: 6:00-6:59 AM or 5:00 PM+
- Detects and emails:
	- `🟢 NEW PROGRAM`
	- `🔥 NEW REGISTRATION OPEN`
	- `🚨 SPOT OPEN`
- Prevents duplicate alerts using saved state in `state/seen_programs.json`.

## Local setup

1. Create a virtual environment and install deps:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

2. Configure email:

```bash
cp .env.example .env
```

Then set your `.env` values (use a Gmail App Password):

- `SMTP_HOST=smtp.gmail.com`
- `SMTP_PORT=587`
- `SMTP_USERNAME=pickingviolet@gmail.com`
- `SMTP_PASSWORD=...`
- `EMAIL_FROM=pickingviolet@gmail.com`
- `EMAIL_TO=pickingviolet@gmail.com`

3. Run once:

```bash
python monitor.py
```

4. Test send for all currently matching programs:

```bash
python monitor.py --send-all-now
```

## GitHub Actions automation

The workflow `.github/workflows/monitor.yml` runs every ~15 minutes and on manual trigger.

Add these repo secrets:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `EMAIL_FROM`
- `EMAIL_TO`

When state changes, the workflow commits `state/seen_programs.json` so future runs can detect new/changed listings.

## Notes

- The monitor uses API-endpoint probing first, then falls back to Playwright if needed.
- Scheduled runs in GitHub Actions can be delayed slightly by GitHub load.