#!/usr/bin/env python3
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass, asdict
from datetime import UTC, date, datetime, time
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dateutil import parser as date_parser

BASE_URL = "https://playtennis.usta.com/USTAChicago/Coaching"
UNIFIED_SEARCH_URL = "https://prd-usta-kube.clubspark.pro/unified-search-api/api/Search/classic-coaching/Query"
STATE_DIR = Path("state")
STATE_PATH = STATE_DIR / "seen_programs.json"

ALLOWED_VENUES = {
    "hamlin park",
    "humboldt park",
    "katrina adams tennis courts",
    "revere park",
    "smith park",
    "welles park",
}

START_DATE = date(2026, 8, 14)
END_DATE = date(2027, 7, 31)


@dataclass
class Program:
    program_id: str
    title: str
    venue: str
    program_type: str
    skill_level: str
    start_dt: datetime
    end_dt: Optional[datetime]
    days_of_week: List[str]
    registration_status: str
    spots_available: Optional[int]
    price: Optional[str]
    link: str


@dataclass
class AlertEvent:
    kind: str  # NEW_PROGRAM | NEW_REGISTRATION_OPEN | SPOT_OPEN
    program: Program


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_date_time(value: str) -> Optional[datetime]:
    value = normalize_text(value)
    if not value:
        return None
    try:
        return date_parser.parse(value, fuzzy=True)
    except Exception:
        return None


def venue_allowed(venue: str) -> bool:
    v = normalize_text(venue).lower()
    v = re.sub(r"\([^)]*\)", "", v)
    v = normalize_text(v)
    for allowed in ALLOWED_VENUES:
        if v == allowed:
            return True
        if allowed in v:
            return True
    return False


def skill_allowed(skill: str) -> bool:
    s = normalize_text(skill).lower()
    return "beginner" in s or "all" in s


def program_type_allowed(program_type: str) -> bool:
    return "adult" in normalize_text(program_type).lower()


def time_window_allowed(start_dt: datetime) -> bool:
    weekday = start_dt.weekday()  # 0 Mon .. 6 Sun
    if weekday >= 5:
        return True
    t = start_dt.time()
    return (time(6, 0) <= t < time(7, 0)) or (t >= time(17, 0))


def date_window_allowed(start_dt: datetime) -> bool:
    d = start_dt.date()
    today = date.today()
    if d < START_DATE or d > END_DATE:
        return False
    if d < today:
        return False
    return True


def qualifies(program: Program) -> bool:
    return (
        venue_allowed(program.venue)
        and program_type_allowed(program.program_type)
        and skill_allowed(program.skill_level)
        and date_window_allowed(program.start_dt)
        and time_window_allowed(program.start_dt)
    )


def parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = normalize_text(value)
    match = re.search(r"-?\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _extract_venue_from_title(title: str) -> str:
    m = re.search(r"\bat\s+(.+?)(?:\s+Ages\b|$)", title, flags=re.I)
    if m:
        return normalize_text(m.group(1))
    return ""


def _parse_card_start_datetime(date_text: str, time_text: str) -> Optional[datetime]:
    date_text = normalize_text(date_text)
    time_text = normalize_text(time_text)
    if not date_text:
        return None

    # Example: "Tue, Aug 4 - Tue, Sep 8, 2026"
    # Keep the first date and append year from the full date range if needed.
    date_first = date_text.split("-")[0].strip().rstrip(",")
    year_match = re.search(r"(20\d{2})", date_text)
    if year_match and not re.search(r"20\d{2}", date_first):
        date_first = f"{date_first}, {year_match.group(1)}"

    # Example: "4:00 pm - 5:30 pm"
    time_first = time_text.split("-")[0].strip() if time_text else "12:00 am"

    return parse_date_time(f"{date_first} {time_first}")


def _guess_link_from_blob(blob: Dict[str, Any]) -> str:
    for k in ["url", "link", "courseUrl", "registrationUrl", "href"]:
        val = blob.get(k)
        if isinstance(val, str) and val.startswith("http"):
            return val
        if isinstance(val, str) and val.startswith("/"):
            return f"https://playtennis.usta.com{val}"
    return BASE_URL


def fetch_programs_via_unified_api(timeout: int = 60) -> List[Program]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://playtennis.usta.com",
            "Referer": BASE_URL,
        }
    )

    size = 100

    def payload(offset: int) -> Dict[str, Any]:
        return {
            "options": {
                "size": size,
                "from": offset,
                "sortKey": "distance",
                "latitude": 41.8781,
                "longitude": -87.6298,
            },
            "filters": [
                {"key": "organisation-id", "items": [{"value": "4be8033a-280d-46fd-b954-995df6a05d57"}]},
                {"key": "region-id", "items": []},
                {"key": "subCategory", "items": [{"value": 0}]},
                {"key": "location-id", "items": [], "operator": "Or"},
                {
                    "key": "date-range",
                    "items": [
                        {
                            "minDate": "2026-08-14T00:00:00.000Z",
                            "maxDate": "2027-07-31T23:59:59.999Z",
                        }
                    ],
                    "operator": "Or",
                },
                {"key": "skill-level", "items": [], "operator": "Or"},
                {"key": "programme-type", "items": [], "operator": "Or"},
                {"key": "day-of-week", "items": [], "operator": "Or"},
                {"key": "activity", "items": [], "operator": "Or"},
                {"key": "time-of-day", "items": [], "operator": "Or"},
            ],
        }

    first = session.post(UNIFIED_SEARCH_URL, json=payload(0), timeout=timeout)
    first.raise_for_status()
    first_obj = first.json()

    total = int(first_obj.get("total", 0) or 0)
    wrappers: List[Dict[str, Any]] = list(first_obj.get("searchResults") or [])

    for offset in range(size, total, size):
        r = session.post(UNIFIED_SEARCH_URL, json=payload(offset), timeout=timeout)
        if r.status_code != 200:
            continue
        obj = r.json()
        wrappers.extend(list(obj.get("searchResults") or []))

    programs: List[Program] = []
    for wrapper in wrappers:
        item = wrapper.get("item") if isinstance(wrapper, dict) else None
        if not isinstance(item, dict):
            continue

        pid = normalize_text(item.get("id") or "")
        title = normalize_text(item.get("name") or "Untitled Program")
        venue = normalize_text((item.get("location") or {}).get("name") or _extract_venue_from_title(title))

        programme = item.get("programme") or {}
        program_type = normalize_text(programme.get("type") or "")

        levels = item.get("levels") or []
        if isinstance(levels, list) and levels:
            level_names = [normalize_text(x.get("name")) for x in levels if isinstance(x, dict)]
            skill_level = ", ".join([x for x in level_names if x])
        else:
            skill_level = ""

        start_dt = parse_date_time(str(item.get("startDateTime") or ""))
        end_dt = parse_date_time(str(item.get("endDateTime") or ""))
        if not start_dt:
            continue

        next_dt = parse_date_time(str(item.get("nextDateTime") or ""))
        if next_dt and next_dt > start_dt:
            start_dt = next_dt

        has_spaces = (programme.get("hasSpacesAvailable") if isinstance(programme, dict) else None)
        remaining = item.get("remainingAttendeeCapacity")
        spots_available = int(remaining) if isinstance(remaining, int) else parse_int(remaining)

        if has_spaces is True or (spots_available is not None and spots_available > 0):
            registration_status = "Open"
        elif has_spaces is False or (spots_available is not None and spots_available <= 0):
            registration_status = "Full"
        else:
            registration_status = "Unknown"

        offers = item.get("offers") or []
        price = None
        if isinstance(offers, list) and offers:
            first_offer = offers[0] if isinstance(offers[0], dict) else {}
            p = first_offer.get("price")
            if p is not None:
                price = f"${p}"

        session_url = normalize_text(item.get("url") or "")
        if session_url.startswith("http"):
            link = session_url
        elif pid:
            link = f"https://playtennis.usta.com/USTAChicago/Coaching/Session/{pid}"
        else:
            link = BASE_URL

        days_of_week = [start_dt.strftime("%A")]

        programs.append(
            Program(
                program_id=pid or f"{title}|{venue}|{start_dt.isoformat()}",
                title=title,
                venue=venue,
                program_type=program_type,
                skill_level=skill_level,
                start_dt=start_dt,
                end_dt=end_dt,
                days_of_week=days_of_week,
                registration_status=registration_status,
                spots_available=spots_available,
                price=price,
                link=link,
            )
        )

    dedup: Dict[str, Program] = {}
    for p in programs:
        dedup[p.program_id] = p
    return list(dedup.values())


def _extract_programs_from_json_like(items: List[Dict[str, Any]]) -> List[Program]:
    results: List[Program] = []
    for raw in items:
        pid = normalize_text(
            raw.get("id")
            or raw.get("programId")
            or raw.get("courseId")
            or raw.get("eventId")
            or raw.get("guid")
            or ""
        )
        title = normalize_text(raw.get("title") or raw.get("name") or raw.get("programName") or "Untitled Program")
        venue = normalize_text(raw.get("venue") or raw.get("venueName") or raw.get("location") or "")
        program_type = normalize_text(raw.get("programType") or raw.get("type") or raw.get("audience") or "")
        skill_level = normalize_text(raw.get("skillLevel") or raw.get("level") or raw.get("ability") or "")

        start_raw = raw.get("startDate") or raw.get("start") or raw.get("startDateTime") or raw.get("firstSession")
        end_raw = raw.get("endDate") or raw.get("end") or raw.get("endDateTime")
        start_dt = parse_date_time(str(start_raw)) if start_raw is not None else None
        end_dt = parse_date_time(str(end_raw)) if end_raw is not None else None

        if not start_dt:
            continue

        if not pid:
            pid = f"{title}|{venue}|{start_dt.isoformat()}"

        days: List[str] = []
        for k in ["days", "dayNames", "weekdays"]:
            val = raw.get(k)
            if isinstance(val, list):
                days = [normalize_text(x) for x in val if normalize_text(x)]
                break
            if isinstance(val, str):
                days = [normalize_text(x) for x in re.split(r"[,/]| and ", val) if normalize_text(x)]
                break

        registration_status = normalize_text(
            raw.get("registrationStatus")
            or raw.get("status")
            or raw.get("availability")
            or "unknown"
        )
        spots_available = parse_int(raw.get("spotsAvailable") or raw.get("spaces") or raw.get("capacityRemaining"))
        price = normalize_text(raw.get("price") or raw.get("cost") or raw.get("fee") or "") or None
        link = _guess_link_from_blob(raw)

        results.append(
            Program(
                program_id=pid,
                title=title,
                venue=venue,
                program_type=program_type,
                skill_level=skill_level,
                start_dt=start_dt,
                end_dt=end_dt,
                days_of_week=days,
                registration_status=registration_status,
                spots_available=spots_available,
                price=price,
                link=link,
            )
        )
    return results


def fetch_programs_via_endpoint_probe(timeout: int = 60) -> List[Program]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        }
    )

    page = session.get(BASE_URL, timeout=timeout)
    page.raise_for_status()
    html = page.text

    # Pull JS script URLs and probe them for JSON endpoints.
    script_srcs = re.findall(r"<script[^>]+src=[\"']([^\"']+)[\"']", html, flags=re.I)
    script_urls: List[str] = []
    for src in script_srcs:
        if src.startswith("http"):
            script_urls.append(src)
        elif src.startswith("//"):
            script_urls.append(f"https:{src}")
        elif src.startswith("/"):
            script_urls.append(f"https://playtennis.usta.com{src}")

    candidate_endpoints: List[str] = []
    endpoint_pattern = re.compile(r"https?://[^\"'\s]+|/[^\"'\s]+")
    endpoint_keywords = re.compile(r"program|coaching|course|search|api|aggregator|json|event|booking", re.I)

    for script_url in script_urls[:40]:
        try:
            resp = session.get(script_url, timeout=timeout)
            if resp.status_code != 200:
                continue
            text = resp.text
            for m in endpoint_pattern.findall(text):
                if not endpoint_keywords.search(m):
                    continue
                if m.startswith("http"):
                    full = m
                elif m.startswith("/"):
                    full = f"https://playtennis.usta.com{m}"
                else:
                    continue
                if full not in candidate_endpoints:
                    candidate_endpoints.append(full)
        except Exception:
            continue

    programs: List[Program] = []
    for endpoint in candidate_endpoints[:80]:
        try:
            r = session.get(endpoint, timeout=timeout)
            content_type = normalize_text(r.headers.get("content-type", "")).lower()
            if "json" not in content_type:
                continue
            payload = r.json()
            items: List[Dict[str, Any]] = []
            if isinstance(payload, list):
                items = [x for x in payload if isinstance(x, dict)]
            elif isinstance(payload, dict):
                for key in ["data", "items", "results", "programs", "courses", "events"]:
                    val = payload.get(key)
                    if isinstance(val, list):
                        items = [x for x in val if isinstance(x, dict)]
                        if items:
                            break
            extracted = _extract_programs_from_json_like(items)
            if extracted:
                programs.extend(extracted)
        except Exception:
            continue

    dedup: Dict[str, Program] = {}
    for p in programs:
        dedup[p.program_id] = p
    return list(dedup.values())


def fetch_programs_via_playwright(timeout: int = 60) -> List[Program]:
    from playwright.sync_api import sync_playwright

    programs_raw: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        context.route(
            "**/*",
            lambda route: route.abort()
            if re.search(r"google-analytics|googletagmanager|adobedtm|cookielaw|doubleclick|facebook", route.request.url, re.I)
            else route.continue_(),
        )
        page = context.new_page()

        def on_response(resp):
            try:
                ctype = normalize_text(resp.headers.get("content-type", "")).lower()
                if "json" not in ctype:
                    return
                url = resp.url
                if not re.search(r"program|coaching|course|search|event|booking|aggregator", url, re.I):
                    return
                payload = resp.json()
                if isinstance(payload, list):
                    programs_raw.extend([x for x in payload if isinstance(x, dict)])
                elif isinstance(payload, dict):
                    for key in ["data", "items", "results", "programs", "courses", "events"]:
                        val = payload.get(key)
                        if isinstance(val, list):
                            programs_raw.extend([x for x in val if isinstance(x, dict)])
            except Exception:
                return

        page.on("response", on_response)
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=max(timeout, 90) * 1000)
        page.wait_for_timeout(5000)

        # Best-effort DOM scrape if network probe did not capture normalized JSON.
        if not programs_raw:
            cards = page.locator("div.csa-search-result-item.csa-search-result-coaching")
            count = min(cards.count(), 200)
            for i in range(count):
                card = cards.nth(i)
                title = normalize_text(card.locator("h3.title a").first.inner_text())
                if not title:
                    continue

                href = card.locator("h3.title a").first.get_attribute("href") or ""
                if href.startswith("http"):
                    link = href
                elif href.startswith("/"):
                    link = f"https://playtennis.usta.com{href}"
                else:
                    link = BASE_URL

                category = normalize_text(card.locator(".result-item-label").first.inner_text())
                level = normalize_text(card.locator(".csa-level span").first.inner_text())
                date_text = normalize_text(card.locator("li.csa-date").first.inner_text())
                time_text = normalize_text(card.locator("li.csa-time").first.inner_text())
                status_text = normalize_text(card.locator(".csa-course-status").first.inner_text())
                price_text = normalize_text(card.locator(".csa-price-value").first.inner_text())

                status_lower = status_text.lower()
                if "full" in status_lower:
                    spots_available = 0
                elif "limited" in status_lower or "open" in status_lower or "available" in status_lower:
                    spots_available = 1
                else:
                    spots_available = None

                start_dt = _parse_card_start_datetime(date_text, time_text)
                if not start_dt:
                    continue

                venue = _extract_venue_from_title(title)

                raw = {
                    "id": link,
                    "title": title,
                    "venue": venue,
                    "programType": category,
                    "skillLevel": level,
                    "startDate": start_dt.isoformat(),
                    "registrationStatus": status_text,
                    "spotsAvailable": spots_available,
                    "price": price_text,
                    "link": link,
                }
                programs_raw.append(raw)

        browser.close()

    return _extract_programs_from_json_like(programs_raw)


def load_state() -> Dict[str, Dict[str, Any]]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_state(state: Dict[str, Dict[str, Any]]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True, default=str), encoding="utf-8")


def classify_events(current: List[Program], previous: Dict[str, Dict[str, Any]]) -> List[AlertEvent]:
    events: List[AlertEvent] = []
    for p in current:
        prev = previous.get(p.program_id)
        if prev is None:
            events.append(AlertEvent(kind="NEW_PROGRAM", program=p))
            continue

        prev_status = normalize_text(prev.get("registration_status", "")).lower()
        new_status = normalize_text(p.registration_status).lower()

        prev_spots = prev.get("spots_available")
        try:
            prev_spots = int(prev_spots) if prev_spots is not None else None
        except Exception:
            prev_spots = None

        new_spots = p.spots_available

        if ("open" not in prev_status) and ("open" in new_status):
            events.append(AlertEvent(kind="NEW_REGISTRATION_OPEN", program=p))
            continue

        if (prev_spots is not None and prev_spots <= 0) and (new_spots is not None and new_spots > 0):
            events.append(AlertEvent(kind="SPOT_OPEN", program=p))
            continue

    return events


def format_program_block(program: Program) -> str:
    start_str = program.start_dt.strftime("%A, %b %d %Y %I:%M %p")
    end_str = program.end_dt.strftime("%A, %b %d %Y %I:%M %p") if program.end_dt else "N/A"
    days = ", ".join(program.days_of_week) if program.days_of_week else "N/A"
    spots = str(program.spots_available) if program.spots_available is not None else "Unknown"
    price = program.price or "Unknown"

    return (
        f"Title: {program.title}\n"
        f"Venue: {program.venue}\n"
        f"Program Type: {program.program_type}\n"
        f"Skill Level: {program.skill_level}\n"
        f"Start: {start_str}\n"
        f"End: {end_str}\n"
        f"Days: {days}\n"
        f"Registration Status: {program.registration_status}\n"
        f"Spots Available: {spots}\n"
        f"Price: {price}\n"
        f"Link: {program.link}\n"
    )


def send_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_email = os.getenv("EMAIL_FROM", username)
    to_email = os.getenv("EMAIL_TO", "")

    if not username or not password or not to_email:
        print("[WARN] Email is not configured. Skipping send.")
        print("Set SMTP_USERNAME, SMTP_PASSWORD, EMAIL_TO in .env or Secrets.")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(username, password)
        server.sendmail(from_email, [to_email], msg.as_string())


def run() -> int:
    load_env_file()

    timeout = int(os.getenv("CHECK_TIMEOUT_SECONDS", "60"))
    use_playwright_fallback = os.getenv("USE_PLAYWRIGHT_FALLBACK", "1") not in {"0", "false", "False"}
    force_send_all = "--send-all-now" in sys.argv

    programs: List[Program] = []

    # Strategy: direct unified API first, endpoint probe second, browser fallback last.
    try:
        programs = fetch_programs_via_unified_api(timeout=timeout)
    except Exception as exc:
        print(f"[WARN] Unified API fetch failed: {exc}")

    try:
        if not programs:
            programs = fetch_programs_via_endpoint_probe(timeout=timeout)
    except Exception as exc:
        print(f"[WARN] Endpoint probe failed: {exc}")

    if not programs and use_playwright_fallback:
        try:
            programs = fetch_programs_via_playwright(timeout=timeout)
        except Exception as exc:
            print(f"[ERROR] Playwright fallback failed: {exc}")
            return 1

    if not programs:
        print("[WARN] No programs extracted from source.")
        return 1

    filtered = [p for p in programs if qualifies(p)]
    filtered.sort(key=lambda x: x.start_dt)

    previous = load_state()

    if force_send_all:
        events = [AlertEvent(kind="NEW_PROGRAM", program=p) for p in filtered]
    else:
        events = classify_events(filtered, previous)

    new_state: Dict[str, Dict[str, Any]] = {}
    for p in filtered:
        new_state[p.program_id] = {
            "registration_status": p.registration_status,
            "spots_available": p.spots_available,
            "snapshot": asdict(p),
            "updated_at": datetime.now(UTC).isoformat(),
        }

    save_state(new_state)

    if events:
        grouped: Dict[str, List[Program]] = {"NEW_PROGRAM": [], "NEW_REGISTRATION_OPEN": [], "SPOT_OPEN": []}
        for event in events:
            grouped.setdefault(event.kind, []).append(event.program)

        sections: List[str] = []
        title_map = {
            "NEW_PROGRAM": "🟢 NEW PROGRAM",
            "NEW_REGISTRATION_OPEN": "🔥 NEW REGISTRATION OPEN",
            "SPOT_OPEN": "🚨 SPOT OPEN",
        }

        for key in ["NEW_PROGRAM", "NEW_REGISTRATION_OPEN", "SPOT_OPEN"]:
            items = grouped.get(key, [])
            if not items:
                continue
            blocks = "\n\n".join(format_program_block(p) for p in items)
            sections.append(f"{title_map.get(key, key)}\n\n{blocks}")

        body = (
            f"USTA Chicago alert generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Filters: Adult, Beginner/All, selected parks, {START_DATE} to {END_DATE}, weekday 6-7am or 5pm+, weekend any time.\n\n"
            + "\n\n".join(sections)
        )

        subject = f"Tennis Alert: {len(events)} qualifying update(s)"
        send_email(subject, body)
        print(subject)
    else:
        print(f"No new qualifying updates. Checked {len(filtered)} matching programs.")

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
