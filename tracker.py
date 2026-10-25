#!/usr/bin/env python3
"""Scheduled bus-fare history tracker for an authorized JSON source.

This program intentionally does not scrape RedBus or call undocumented private
endpoints. Configure FARE_API_URL only for a source you are authorized to use.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
CSV_PATH = DATA_DIR / "prices.csv"
ALERT_STATE_PATH = DATA_DIR / "alert_state.json"
SAMPLE_PATH = ROOT / "sample_fares.json"
CSV_FIELDS = [
    "snapshot_utc", "source", "origin", "destination", "journey_date",
    "bus_id", "operator", "bus_type", "departure", "arrival", "fare_inr",
    "seats_available", "booking_url",
]


class TrackerError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrackerError(f"Invalid JSON in {path.name}: {exc}") from exc


def get_path(obj: Any, dotted_path: str) -> Any:
    """Read nested dict/list values using e.g. data.buses or results.0.fare."""
    if not dotted_path:
        return obj
    current = obj
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def parse_fare(value: Any) -> float | None:
    """Convert values such as 499, '₹ 1,299', or 'INR 799' to a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    text = str(value).replace(",", "")
    match = re.search(r"\d+(?:\.\d{1,2})?", text)
    if not match:
        return None
    try:
        amount = float(match.group(0))
    except ValueError:
        return None
    return amount if amount >= 0 else None


def format_query_params(config: dict[str, Any]) -> dict[str, str]:
    raw_params = config.get("query_params", {})
    values = {
        "origin": config.get("origin", ""),
        "destination": config.get("destination", ""),
        "journey_date": config.get("journey_date", ""),
    }
    params: dict[str, str] = {}
    for key, template in raw_params.items():
        params[str(key)] = str(template).format(**values)
    return params


def fetch_payload(config: dict[str, Any], sample: bool) -> tuple[Any, str]:
    if sample:
        payload = load_json(SAMPLE_PATH)
        if payload is None:
            raise TrackerError("sample_fares.json is missing")
        return payload, "sample"

    url = os.environ.get("FARE_API_URL", "").strip()
    if not url:
        raise TrackerError(
            "FARE_API_URL is not configured. Set it to an authorized JSON fare-feed URL. "
            "For a local test, run: python tracker.py --sample"
        )

    headers = {"Accept": "application/json"}
    token = os.environ.get("FARE_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    timeout = int(os.environ.get("FARE_API_TIMEOUT_SECONDS", "20"))
    try:
        response = requests.get(
            url,
            params=format_query_params(config),
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise TrackerError(f"Fare source request failed: {exc}") from exc
    except ValueError as exc:
        raise TrackerError(
            "Fare source did not return valid JSON. This connector expects an authorized JSON feed, "
            "not an HTML booking page."
        ) from exc

    return payload, "authorized_json"


def normalize_records(payload: Any, config: dict[str, Any], source_name: str) -> list[dict[str, str]]:
    items_path = str(config.get("items_path", ""))
    items = get_path(payload, items_path)
    if isinstance(items, dict):
        # Some APIs return an object keyed by bus ID; convert values to rows.
        items = list(items.values())
    if not isinstance(items, list):
        raise TrackerError(
            f"Could not find a list of bus results at items_path={items_path!r}. "
            "Update config.json to match the authorized source's response schema."
        )

    field_paths = config.get("fields", {})
    if not isinstance(field_paths, dict):
        raise TrackerError("config.json 'fields' must be an object mapping output names to JSON paths")

    now = utc_now_iso()
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str, str]] = set()

    for item in items:
        if not isinstance(item, dict):
            continue

        def field(name: str, default: Any = "") -> Any:
            path = str(field_paths.get(name, name))
            value = get_path(item, path)
            return default if value is None else value

        fare = parse_fare(field("fare"))
        # Ignore rows that don't include a parseable fare rather than recording false prices.
        if fare is None:
            continue

        bus_id = as_text(field("bus_id"))
        operator = as_text(field("operator"))
        bus_type = as_text(field("bus_type"))
        departure = as_text(field("departure"))
        arrival = as_text(field("arrival"))
        seats = as_text(field("seats_available"))
        booking_url = as_text(field("booking_url"))
        identity = bus_id or "|".join([operator, bus_type, departure, arrival])
        dedupe_key = (identity, operator, bus_type, departure, f"{fare:.2f}")
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        output.append({
            "snapshot_utc": now,
            "source": source_name,
            "origin": as_text(config.get("origin")),
            "destination": as_text(config.get("destination")),
            "journey_date": as_text(config.get("journey_date")),
            "bus_id": bus_id,
            "operator": operator,
            "bus_type": bus_type,
            "departure": departure,
            "arrival": arrival,
            "fare_inr": f"{fare:.2f}",
            "seats_available": seats,
            "booking_url": booking_url,
        })

    return output


def append_csv(records: list[dict[str, str]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0
    with CSV_PATH.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerows(records)


def load_alert_state() -> dict[str, Any]:
    state = load_json(ALERT_STATE_PATH, default={})
    if not isinstance(state, dict):
        raise TrackerError("data/alert_state.json must contain a JSON object")
    return state


def save_alert_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ALERT_STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def send_telegram(message: str) -> bool:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not bot_token or not chat_id:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise TrackerError(f"Telegram rejected the message: {body}")
    except requests.RequestException as exc:
        raise TrackerError(f"Telegram notification failed: {exc}") from exc
    return True


def check_alerts(records: list[dict[str, str]], config: dict[str, Any]) -> None:
    target = parse_fare(config.get("target_price_inr"))
    if target is None:
        return

    state = load_alert_state()
    changed = False
    for row in records:
        fare = parse_fare(row["fare_inr"])
        if fare is None:
            continue
        bus_key = row["bus_id"] or "|".join([
            row["operator"], row["bus_type"], row["departure"], row["arrival"],
        ])
        old = state.get(bus_key, {})
        was_below = bool(old.get("below_target", False)) if isinstance(old, dict) else False
        is_below = fare <= target

        alert_sent_now = False
        if is_below and not was_below:
            message = (
                f"🚌 Bus fare target reached\n"
                f"Route: {row['origin']} → {row['destination']}\n"
                f"Date: {row['journey_date']}\n"
                f"Operator: {row['operator'] or 'Unknown'}\n"
                f"Bus: {row['bus_type'] or 'Not specified'}\n"
                f"Departure: {row['departure'] or 'Unknown'}\n"
                f"Fare: ₹{fare:.2f} (target ₹{target:.2f})\n"
                f"Booking: {row['booking_url'] or 'Open the source page to verify'}"
            )
            alert_sent_now = send_telegram(message)
            if alert_sent_now:
                print(f"Telegram alert sent for {bus_key}")
            else:
                print("Telegram not configured; target match logged but no alert sent.")

        # If Telegram isn't configured, leave the bus eligible for an alert later.
        # Once notified, suppress repeats until the fare rises above the target.
        state[bus_key] = {
            "below_target": is_below if (was_below or alert_sent_now or not is_below) else False,
            "last_fare_inr": fare,
            "last_seen_utc": row["snapshot_utc"],
        }
        changed = True

    if changed:
        save_alert_state(state)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record bus fare snapshots from an authorized JSON feed")
    parser.add_argument("--sample", action="store_true", help="Use the local sample data (no network request)")
    args = parser.parse_args()

    try:
        config = load_json(CONFIG_PATH)
        if not isinstance(config, dict):
            raise TrackerError("config.json is missing or is not a JSON object")
        if not config.get("origin") or not config.get("destination") or not config.get("journey_date"):
            raise TrackerError("Set origin, destination, and journey_date in config.json")

        payload, source_name = fetch_payload(config, args.sample)
        records = normalize_records(payload, config, source_name)
        if not records:
            print("The source returned no usable bus records with valid fares; no rows were saved.")
            return 0

        append_csv(records)
        check_alerts(records, config)
        print(f"Saved {len(records)} bus fare record(s) at {records[0]['snapshot_utc']} UTC to {CSV_PATH.relative_to(ROOT)}")
        for row in records[:10]:
            print(f"  ₹{row['fare_inr']} | {row['operator']} | {row['departure']} | {row['bus_type']}")
        if len(records) > 10:
            print(f"  ... plus {len(records) - 10} more")
        return 0
    except TrackerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
