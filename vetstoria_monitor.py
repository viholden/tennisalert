#!/usr/bin/env python3
"""Monitor Vetstoria vaccine appointment availability and email slot changes."""
import json
import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List

from playwright.sync_api import sync_playwright

from monitor import load_env_file, send_email

BOOKING_URL = "https://us.vetstoria.com/booking/69efcd14cfbca/?r=6&step=2"
STATE_PATH = Path("state/vetstoria_seen_state.json")


@dataclass(frozen=True)
class Slot:
    date: str
    time: str
    clinician: str


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def dismiss_cookie_banner(page: Any) -> None:
    banner = page.locator(".cookie-policy-acceptance-banner")
    if banner.count() and banner.first.is_visible():
        banner.first.evaluate("element => element.remove()")


def check_slots(timeout_seconds: int = 90) -> List[Slot]:
    captured: List[Dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        def on_response(response: Any) -> None:
            if "/api/workflows/v2/bookings/" not in response.url:
                return
            try:
                payload = response.json()
                if isinstance(payload, dict) and "available_slots" in payload:
                    captured.append(payload)
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        page.wait_for_timeout(1800)

        page.get_by_text("I am a returning client", exact=True).click()
        page.get_by_text("1", exact=True).click()
        page.get_by_text("Cat", exact=True).click()
        page.locator("#appointment_selection .ui-select__label").click()
        page.get_by_text("Vaccinations", exact=True).click()
        page.locator("#appointment_selection .ui-select__display-value").get_by_text(
            "Vaccinations", exact=True
        ).wait_for(state="visible", timeout=timeout_seconds * 1000)
        dismiss_cookie_banner(page)
        page.get_by_role("button", name="Next").click(force=True)
        notice = page.get_by_text(
            "I have read, understood and accept the above terms and conditions.",
            exact=False,
        )
        try:
            notice.wait_for(state="visible", timeout=15000)
        except Exception:
            if "step=3" not in page.url:
                page.get_by_role("button", name="Next").click(force=True)
            notice.wait_for(state="visible", timeout=timeout_seconds * 1000)
        notice.click()
        dismiss_cookie_banner(page)
        page.get_by_role("button", name="Next").click(force=True)
        page.wait_for_timeout(5000)

        available_date_labels: List[str] = []
        date_links = page.locator('.item-link[aria-label*=" is available"]')
        for index in range(date_links.count()):
            label = date_links.nth(index).get_attribute("aria-label")
            if label and label not in available_date_labels:
                available_date_labels.append(label)

        for label in available_date_labels:
            date_link = page.locator(f'.item-link[aria-label="{label}"]')
            if date_link.count():
                date_link.click(force=True)
                page.wait_for_timeout(7000)

        browser.close()

    slots: List[Slot] = []
    for payload in captured:
        for calendar in payload.get("available_slots", []):
            clinician = str(calendar.get("name") or "No preference")
            for day, day_slots in (calendar.get("slots") or {}).items():
                for entry in day_slots or []:
                    if isinstance(entry, dict) and entry.get("slot"):
                        slots.append(
                            Slot(
                                date=str(day),
                                time=str(entry["slot"]),
                                clinician=clinician,
                            )
                        )

    return sorted(set(slots), key=lambda slot: (slot.date, slot.time, slot.clinician))


def slot_key(slot: Slot) -> str:
    return f"{slot.date}|{slot.time}|{slot.clinician}"


def format_slot(slot: Slot) -> str:
    return f"{slot.date} at {slot.time} ({slot.clinician})"


def run() -> int:
    load_env_file()
    timeout = int(os.getenv("CHECK_TIMEOUT_SECONDS", "90"))
    force = "--send-all-now" in sys.argv

    try:
        slots = check_slots(timeout)
    except Exception as exc:
        print(f"[ERROR] Vetstoria check failed: {exc}")
        return 1

    current_keys = {slot_key(slot) for slot in slots}
    previous = load_state()
    previous_keys = set(previous.get("slots", []))
    new_keys = current_keys - previous_keys

    save_state(
        {
            "slots": sorted(current_keys),
            "checked_at": datetime.now(UTC).isoformat(),
            "booking_url": BOOKING_URL,
            "criteria": {
                "client": "returning",
                "pets": 1,
                "species": "cat",
                "appointment_type": "Vaccinations",
            },
        }
    )

    should_alert = force or bool(new_keys)
    if not should_alert:
        print(f"No new Vetstoria slots. Current available slots: {len(slots)}.")
        return 0

    subject = "Vet appointment slot available: vaccinations for your cat"
    if force:
        intro = "Current Vetstoria slots matching your request:"
    elif previous_keys:
        intro = "A new Vetstoria slot opened or changed:"
    else:
        intro = "Vetstoria currently has matching vaccine appointment slots:"

    body = (
        f"{intro}\n\n"
        "Criteria: returning client, 1 pet, cat, Vaccinations.\n\n"
        + "\n".join(f"- {format_slot(slot)}" for slot in slots)
        + f"\n\nBOOK NOW: {BOOKING_URL}\n"
        f"Checked: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    send_email(subject, body)
    print(f"{subject} ({len(slots)} slots)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
