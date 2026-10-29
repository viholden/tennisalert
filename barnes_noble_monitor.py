#!/usr/bin/env python3
"""Monitor Barnes & Noble online and selected store inventory."""
import json
import os
import sys
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

from playwright.sync_api import sync_playwright

from monitor import load_env_file, send_email

PRODUCT_URL = "https://www.barnesandnoble.com/w/dragon-blind-box-peeker-charms-sonix/1150479440"
STATE_PATH = Path("state/barnes_noble_inventory_state.json")
SEARCH_ZIPS = ["60601", "90503"]

TARGET_STORES = {
    "State Street": "Chicago, IL 60601",
    "Diversey Parkway": "Chicago, IL 60614",
    "Wicker Park": "Chicago, IL 60647",
    "Hyde Park": "Chicago, IL 60615",
    "Oak Park": "Oak Park, IL 60301",
    "Village Crossing": "Skokie, IL 60077",
    "Del Amo Fashion Center": "Torrance, CA 90503",
    "Manhattan Beach": "Manhattan Beach, CA 90266",
    "Marina del Rey": "Marina del Rey, CA 90292",
    "Long Beach": "Long Beach, CA 90803",
    "Long Beach Towne Center": "Long Beach, CA 90808",
    "Santa Monica": "Santa Monica, CA 90401",
}


@dataclass(frozen=True)
class StoreStatus:
    name: str
    address: str
    in_stock: bool
    pickup_available: bool
    observed_text: str


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


def normalize(value: str) -> str:
    return " ".join(value.split()).strip()


def parse_store_card(card: Any, store_name: str) -> StoreStatus:
    text = normalize(card.inner_text())
    lowered = text.lower()
    address = TARGET_STORES[store_name]
    return StoreStatus(
        name=store_name,
        address=address,
        in_stock="in stock in store" in lowered,
        pickup_available=(
            "available for pickup" in lowered
            and "unavailable for pickup" not in lowered
        ),
        observed_text=text,
    )


def check_inventory(timeout_seconds: int = 120) -> tuple[bool, List[StoreStatus]]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
        page.wait_for_timeout(6000)

        body_text = page.locator("body").inner_text().lower()
        online_available = "out of stock online" not in body_text

        page.get_by_text("Find in Stores", exact=True).click()
        page.wait_for_timeout(1200)
        search = page.locator('input[placeholder="Zip Code, City, State, or Store Name"]')
        search.wait_for(state="visible", timeout=timeout_seconds * 1000)

        results: Dict[str, StoreStatus] = {}
        for zip_code in SEARCH_ZIPS:
            search.fill(zip_code)
            page.get_by_text("Search", exact=True).last.click()
            page.wait_for_timeout(5000)

            cards = page.locator(".store-drawer__location")
            for index in range(cards.count()):
                card = cards.nth(index)
                title = card.locator(".store-drawer__location-title")
                if not title.count():
                    continue
                name = normalize(title.inner_text())
                if name in TARGET_STORES:
                    results[name] = parse_store_card(card, name)

        browser.close()

    missing = set(TARGET_STORES) - set(results)
    if missing:
        raise RuntimeError(f"Barnes & Noble store search did not return: {', '.join(sorted(missing))}")

    return online_available, [results[name] for name in TARGET_STORES]


def status_key(online_available: bool, stores: Iterable[StoreStatus]) -> Dict[str, Any]:
    return {
        "online_available": online_available,
        "stores": {
            store.name: {
                "in_stock": store.in_stock,
                "pickup_available": store.pickup_available,
                "address": store.address,
            }
            for store in stores
        },
    }


def run() -> int:
    load_env_file()
    timeout = int(os.getenv("CHECK_TIMEOUT_SECONDS", "120"))
    force = "--send-all-now" in sys.argv

    try:
        online_available, stores = check_inventory(timeout)
    except Exception as exc:
        print(f"[ERROR] Barnes & Noble check failed: {exc}")
        return 1

    current = status_key(online_available, stores)
    previous = load_state()
    previous_status = {
        "online_available": previous.get("online_available", False),
        "stores": previous.get("stores", {}),
    }

    online_restocked = online_available and not previous_status.get("online_available", False)
    store_restocked = [
        store
        for store in stores
        if store.in_stock and not previous_status.get("stores", {}).get(store.name, {}).get("in_stock", False)
    ]
    pickup_opened = [
        store
        for store in stores
        if store.pickup_available
        and not previous_status.get("stores", {}).get(store.name, {}).get("pickup_available", False)
    ]

    save_state(
        {
            **current,
            "checked_at": datetime.now(UTC).isoformat(),
            "product_url": PRODUCT_URL,
            "target_stores": TARGET_STORES,
        }
    )

    if not (force or online_restocked or store_restocked or pickup_opened):
        in_stock_count = sum(store.in_stock for store in stores)
        print(f"No new Barnes & Noble availability. Online: {online_available}; stores in stock: {in_stock_count}.")
        return 0

    lines = [
        "Dragon Blind Box Peeker Charms availability changed or was requested.",
        f"Online: {'AVAILABLE' if online_available else 'out of stock'}",
        "",
    ]
    if online_restocked:
        lines.append("NEW ONLINE RESTOCK")
    if store_restocked:
        lines.append("NEW IN-STORE STOCK: " + ", ".join(store.name for store in store_restocked))
    if pickup_opened:
        lines.append("NEW PICKUP AVAILABILITY: " + ", ".join(store.name for store in pickup_opened))
    if force:
        lines.append("CURRENT STORE STATUS")

    lines.extend(
        f"- {store.name}: {'IN STOCK' if store.in_stock else 'out of stock'}; "
        f"{'pickup available' if store.pickup_available else 'pickup unavailable'}; {store.address}"
        for store in stores
    )
    lines.extend(["", f"BUY / CHECK STORES: {PRODUCT_URL}", f"Checked: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])

    send_email("Barnes & Noble restock alert: Dragon Blind Box Peeker Charms", "\n".join(lines))
    print(f"Barnes & Noble alert sent. Online: {online_available}; stores checked: {len(stores)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
