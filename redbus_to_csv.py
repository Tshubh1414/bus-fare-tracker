"""One-time visible-page extractor for a RedBus search URL.

Uses normal Selenium browser automation. It does not bypass CAPTCHAs,
access controls, or private endpoints. Check RedBus's current terms and get
permission before automating collection or reusing site content.
"""

from __future__ import annotations

import csv
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait

URL = (
    "https://www.redbus.in/bus-tickets/bangalore-to-nashik"
    "?fromCityName=Bengaluru&toCityName=Nashik&fromCityId=122"
    "&toCityId=444&onward=30-Oct-2026&ref=modifyDate"
)
OUT_CSV = Path("redbus_bangalore_to_nashik_2026-10-30.csv")
HEADLESS = os.getenv("HEADLESS", "false").strip().lower() in {"1", "true", "yes"}

# Selectors used by common versions of RedBus's visible results page. The site
# may change its markup, so the program saves a screenshot if none are found.
CARD_SELECTORS = (
    "li[class*='tupleWrapper']",
    "div[class*='tupleWrapper']",
    "li.clearfix.row-sec",
    "[class*='bus-card']",
    "[data-testid*='bus-card']",
)

FIELD_SELECTORS = {
    "operator": (
        "[class*='travelsName']", "[class*='operatorName']",
        "[class*='travels']", "[class*='busName']", ".bus-name", "h3", "h4",
    ),
    "bus_type": (
        "[class*='busType']", "[class*='bus-type']", "[class*='busTypeName']",
    ),
    "departure": (
        "[class*='boardingTime']", "[class*='dp-time']",
        "[class*='departureTime']", "[class*='depart-time']",
    ),
    "arrival": (
        "[class*='droppingTime']", "[class*='bp-time']",
        "[class*='arrivalTime']", "[class*='arrival-time']",
    ),
    "duration": (
        "[class*='duration___']", "[class*='duration']", ".dur",
    ),
    "fare_text": (
        "[class*='finalFare']", "[class*='tupleFare']",
        "[class*='fare']", "[class*='price']",
    ),
    "rating": (
        "[class*='ratingTag']", "[class*='rating___']", "[class*='rating']",
    ),
    "seats_text": (
        "[class*='totalSeats']", "[class*='seatsWrap']",
        "[class*='seat-left']", "[class*='availableSeat']",
    ),
}


def first_text(card: Any, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        try:
            for element in card.find_elements(By.CSS_SELECTOR, selector):
                text = (element.text or "").strip()
                if text:
                    return text
        except WebDriverException:
            continue
    return ""


def amount_in_rupees(text: str) -> str:
    if not text:
        return ""
    # Prefer a number explicitly shown with a currency marker.
    match = re.search(r"(?:₹|INR\s*|Rs\.?\s*)([0-9][0-9,]*(?:\.\d{1,2})?)", text, re.I)
    if match:
        return match.group(1).replace(",", "")
    # Some result cards put the symbol in a sibling element; accept a simple
    # amount only when the field text is short and contains digits.
    cleaned = text.strip()
    if len(cleaned) <= 24:
        match = re.search(r"\b([0-9][0-9,]*(?:\.\d{1,2})?)\b", cleaned)
        if match:
            return match.group(1).replace(",", "")
    return ""


def find_cards(driver: webdriver.Chrome) -> list[Any]:
    # Pick the selector returning the largest set, then deduplicate by card text.
    best: list[Any] = []
    for selector in CARD_SELECTORS:
        try:
            found = driver.find_elements(By.CSS_SELECTOR, selector)
            if len(found) > len(best):
                best = found
        except WebDriverException:
            pass
    unique: list[Any] = []
    seen: set[str] = set()
    for card in best:
        text = (card.text or "").strip()
        if text and text not in seen:
            seen.add(text)
            unique.append(card)
    return unique


def visible_page_has_block_notice(driver: webdriver.Chrome) -> bool:
    text = (driver.title + " " + driver.find_element(By.TAG_NAME, "body").text).lower()
    markers = (
        "verify you are human", "captcha", "access denied",
        "unusual traffic", "security check", "robot or human",
    )
    return any(marker in text for marker in markers)


def parse_card(card: Any, source_url: str, scraped_at: str) -> dict[str, str]:
    blob = (card.text or "").strip()
    lines = [line.strip() for line in blob.splitlines() if line.strip()]

    operator = first_text(card, FIELD_SELECTORS["operator"])
    if not operator and lines:
        operator = lines[0]

    bus_type = first_text(card, FIELD_SELECTORS["bus_type"])
    departure = first_text(card, FIELD_SELECTORS["departure"])
    arrival = first_text(card, FIELD_SELECTORS["arrival"])
    duration = first_text(card, FIELD_SELECTORS["duration"])
    fare_text = first_text(card, FIELD_SELECTORS["fare_text"])
    rating = first_text(card, FIELD_SELECTORS["rating"])
    seats_text = first_text(card, FIELD_SELECTORS["seats_text"])

    times = re.findall(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)?\b", blob, flags=re.I)
    if not departure and times:
        departure = times[0]
    if not arrival and len(times) > 1:
        arrival = times[1]

    if not duration:
        match = re.search(r"\b\d+\s*h(?:r)?\s*\d*\s*m(?:in)?\b|\b\d+:\d+\s*hrs?\b", blob, re.I)
        duration = match.group(0) if match else ""

    if not fare_text:
        for line in lines:
            if re.search(r"₹|\bINR\b|\bRs\.?\s*\d", line, re.I):
                fare_text = line
                break

    if not seats_text:
        match = re.search(r"\b\d+\s+seats?\b[^\n]*", blob, re.I)
        seats_text = match.group(0) if match else ""

    fare_inr = amount_in_rupees(fare_text)
    seat_match = re.search(r"(\d+)\s+seats?\b", seats_text, re.I)
    seats_available = seat_match.group(1) if seat_match else ""
    rating_match = re.search(r"\b([0-5](?:\.\d+)?)\b", rating)
    rating_value = rating_match.group(1) if rating_match else ""

    return {
        "scraped_at": scraped_at,
        "origin": "Bengaluru",
        "destination": "Nashik",
        "journey_date": "2026-10-30",
        "operator": operator,
        "bus_type": bus_type,
        "departure": departure,
        "arrival": arrival,
        "duration": duration,
        "fare_inr": fare_inr,
        "fare_text": fare_text,
        "rating": rating_value,
        "seats_available": seats_available,
        "source_url": source_url,
        "card_text": " | ".join(lines),
    }


def main() -> int:
    options = Options()
    if HEADLESS:
        options.add_argument("--headless=new")
    # On GitHub Actions, use the exact Chrome/ChromeDriver paths provided by
    # browser-actions/setup-chrome, rather than the runner's system Chrome.
    chrome_bin = os.getenv("CHROME_BIN", "").strip()
    chromedriver_bin = os.getenv("CHROMEDRIVER_BIN", "").strip()
    if chrome_bin:
        options.binary_location = chrome_bin

    options.add_argument("--window-size=1440,1000")
    options.add_argument("--disable-gpu")
    if sys.platform.startswith("linux"):
        options.add_argument("--no-sandbox")
    # Intentionally do not use stealth plugins, proxy rotation, CAPTCHA solving,
    # or access-control workarounds.

    print(f"Opening: {URL}")
    driver = None
    try:
        if chromedriver_bin:
            driver = webdriver.Chrome(
                service=Service(executable_path=chromedriver_bin), options=options
            )
        else:
            driver = webdriver.Chrome(options=options)  # Selenium Manager for local use
        driver.set_page_load_timeout(60)
        driver.get(URL)
        WebDriverWait(driver, 35).until(
            lambda d: d.execute_script("return document.readyState") in {"interactive", "complete"}
        )
        time.sleep(5)  # allow client-side results to render

        if visible_page_has_block_notice(driver):
            driver.save_screenshot("redbus_block_or_challenge.png")
            print("RedBus showed a security/challenge page. Stopping without bypassing it.")
            print("Screenshot saved: redbus_block_or_challenge.png")
            return 2

        # A small number of ordinary page scrolls can prompt lazy-loaded visible
        # result cards to render. Stop when the count no longer changes.
        previous = -1
        stable_rounds = 0
        for _ in range(5):
            count = len(find_cards(driver))
            if count == previous:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 2:
                break
            previous = count
            driver.execute_script("window.scrollBy(0, Math.max(600, window.innerHeight));")
            time.sleep(1.2)

        cards = find_cards(driver)
        if not cards:
            driver.save_screenshot("redbus_no_results.png")
            Path("redbus_page_title.txt").write_text(
                f"Title: {driver.title}\nURL: {driver.current_url}\n",
                encoding="utf-8",
            )
            print("No bus cards matched the current page markup.")
            print("Saved redbus_no_results.png and redbus_page_title.txt for diagnosis.")
            print("RedBus may have changed its page structure or not loaded results.")
            return 1

        scraped_at = datetime.now().astimezone().isoformat(timespec="seconds")
        rows = [parse_card(card, driver.current_url, scraped_at) for card in cards]
        rows = [row for row in rows if row["operator"] or row["fare_inr"] or row["departure"]]
        if not rows:
            print("Cards were found, but no usable rows could be parsed. Check selector names.")
            driver.save_screenshot("redbus_parse_failed.png")
            return 1

        fields = [
            "scraped_at", "origin", "destination", "journey_date", "operator",
            "bus_type", "departure", "arrival", "duration", "fare_inr",
            "fare_text", "rating", "seats_available", "source_url", "card_text",
        ]
        # Preserve historical snapshots across repeated GitHub Actions runs.
        has_history = OUT_CSV.exists() and OUT_CSV.stat().st_size > 0
        # Use utf-8-sig only for a new file, so a BOM is not added on every append.
        encoding = "utf-8" if has_history else "utf-8-sig"
        with OUT_CSV.open("a", newline="", encoding=encoding) as file:
            writer = csv.DictWriter(file, fieldnames=fields)
            if not has_history:
                writer.writeheader()
            writer.writerows(rows)

        print(f"Appended {len(rows)} bus row(s) to: {OUT_CSV.resolve()}")
        print("Review fare_inr and card_text against the page before relying on the data.")
        return 0

    except TimeoutException:
        print("Timed out waiting for the page to load. Try opening the URL manually in Chrome.")
        if driver:
            driver.save_screenshot("redbus_timeout.png")
        return 1
    except WebDriverException as exc:
        print(f"Browser error: {exc}")
        print("Make sure Google Chrome is installed and can open normally.")
        return 1
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
