# tennisalert

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