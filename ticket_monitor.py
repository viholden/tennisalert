#!/usr/bin/env python3
"""Monitor beventi.co event/ticket pages and email immediately on ANY change.

This is deliberately requests-only (no browser) so it is cheap enough to run
every 20-30 seconds in a tight local loop without burning through any paid
or quota-limited infrastructure.
"""
import difflib
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Dict, Optional

import requests
from bs4 import BeautifulSoup

from monitor import load_env_file, send_email

STATE_DIR = Path("state")
STATE_PATH = STATE_DIR / "ticket_seen_state.json"

URLS = {
    "tickets": "https://beventi.co/tickets/o3kurinmey",
    "event": "https://beventi.co/event/tlc-x-amazon-mgm-studios-early-screening-the-love-hypothesis-2026-us-758029431764",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Phrases that suggest tickets have actually become gettable again.
AVAILABLE_HINTS = ["get tickets", "buy tickets", "reserve your seat", "add to cart"]


def load_state() -> Dict[str, Dict[str, str]]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_state(state: Dict[str, Dict[str, str]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def fetch_clean_text(url: str, timeout: int = 20) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    # Strip volatile noise (countdown timers, long tokens) so it doesn't look
    # like the page changed when nothing meaningful actually did.
    text = re.sub(r"\d+d\d+h\d+m\d+s\s*remaining", "", text, flags=re.I)
    text = re.sub(r"\b[0-9a-fA-F]{16,}\b", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sold_out_count(text: str) -> int:
    return len(re.findall(r"sold\s*out", text, flags=re.I))


def available_signal(text: str) -> bool:
    lowered = text.lower()
    return sold_out_count(text) == 0 and any(hint in lowered for hint in AVAILABLE_HINTS)


def diff_snippet(old_text: str, new_text: str, max_lines: int = 40) -> str:
    old_lines = re.split(r"(?<=[.!?])\s+", old_text)
    new_lines = re.split(r"(?<=[.!?])\s+", new_text)
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=1))
    changed = [line for line in diff if line.startswith("+") or line.startswith("-")]
    changed = [line for line in changed if not line.startswith(("+++", "---"))]
    return "\n".join(changed[:max_lines]) or "(no readable text diff, but the page hash changed)"


def run() -> int:
    load_env_file()

    force = "--force" in sys.argv
    seed_only = "--seed" in sys.argv

    previous = load_state()
    new_state: Dict[str, Dict[str, str]] = {}
    alerts = []

    for key, url in URLS.items():
        try:
            text = fetch_clean_text(url)
        except Exception as exc:
            print(f"[WARN] Fetch failed for {key} ({url}): {exc}")
            continue

        current_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        current_sold_out = sold_out_count(text)
        prev_entry = previous.get(key)

        new_state[key] = {
            "hash": current_hash,
            "sold_out_count": current_sold_out,
            "text": text,
            "updated_at": datetime.now(UTC).isoformat(),
        }

        if prev_entry is None and not force:
            print(f"[INFO] Seeded baseline for {key}, no previous state to compare.")
            continue

        if seed_only:
            continue

        changed = force or (prev_entry is not None and prev_entry.get("hash") != current_hash)
        if not changed:
            print(f"[INFO] No change for {key}.")
            continue

        prev_sold_out = int(prev_entry.get("sold_out_count", 0)) if prev_entry else current_sold_out
        looks_available = available_signal(text) or (prev_sold_out > 0 and current_sold_out == 0)

        alerts.append(
            {
                "key": key,
                "url": url,
                "urgent": looks_available,
                "diff": diff_snippet(prev_entry.get("text", "") if prev_entry else "", text),
            }
        )

    save_state(new_state)

    if not alerts:
        print("No changes detected.")
        return 0

    urgent = any(a["urgent"] for a in alerts)
    subject = (
        "🎟️🚨 TICKET MAY BE AVAILABLE - The Love Hypothesis screening!"
        if urgent
        else "ℹ️ Beventi page change detected - The Love Hypothesis screening"
    )

    body_parts = [f"Checked at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"]
    for alert in alerts:
        body_parts.append(
            f"--- {alert['key']} ---\n"
            f"URL: {alert['url']}\n"
            f"Urgent (looks like a ticket may be available): {'YES' if alert['urgent'] else 'no'}\n"
            f"Changes:\n{alert['diff']}\n"
        )

    body = "\n".join(body_parts)
    send_email(subject, body)
    print(subject)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
