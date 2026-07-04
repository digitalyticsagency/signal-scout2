#!/usr/bin/env python3
"""
Signal — Speaking Opportunity Scout (autonomous weekly runner)

Runs on a schedule (see .github/workflows/weekly-scan.yml), asks Claude to
web-search for speaking opportunities, diffs the results against what was
found last time, and writes the full current list to a Google Sheet.

State (which opportunities we've already seen) is kept in seen_ids.json,
which this script rewrites and the workflow commits back to the repo —
that's what makes "new since last time" possible on a stateless runner.
"""

import json
import os
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "seen_ids.json"
RESULTS_PATH = HERE / "docs" / "results.json"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

MODEL = "claude-sonnet-5"


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and edit it.")
    return json.loads(CONFIG_PATH.read_text())


def load_seen_ids():
    if STATE_PATH.exists():
        return set(json.loads(STATE_PATH.read_text()))
    return set()


def save_seen_ids(ids):
    STATE_PATH.write_text(json.dumps(sorted(ids), indent=2))


def item_id(item):
    base = f"{item.get('url','')}|{item.get('title','')}|{item.get('organization','')}"
    return "op_" + hashlib.sha256(base.encode()).hexdigest()[:16]


def extract_json(text):
    """Find the first well-formed top-level JSON array in text.

    Naive first-'['/last-']' matching breaks if the model's reply contains
    any other bracket pair before the real array — most commonly a markdown
    link like '[Source](url)' in a preamble sentence. This version tracks
    bracket depth and string state so it only ever returns a candidate that
    is actually valid JSON, skipping over false matches and continuing to
    search until it finds a real one (or runs out of text).
    """
    depth = 0
    start = None
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        start = None  # false match (e.g. "[Source]") — keep scanning
                        continue
    return None


def run_search(topics, region):
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY environment variable is not set.")

    today_str = datetime.now().strftime("%A, %B %d, %Y")
    topics_str = ", ".join(topics)

    system_prompt = f"""You are a research assistant helping a professional speaker find real, currently relevant speaking opportunities using web search.
Search for: upcoming conferences, association events, corporate speaker programs, calls-for-speakers/calls-for-proposals, and guest-podcast opportunities relevant to the topics: {topics_str}. Market: {region}. Today's date is {today_str}. Only include things that are upcoming or currently accepting applications — do not include past events.
Search thoroughly — run multiple distinct searches covering different topic/region combinations rather than one broad search, so you surface as many genuine, distinct opportunities as you can find. Do not artificially cap the count; include every real result your searches turn up, whether that's 5 or 50.
After you finish searching, respond with ONLY a JSON array (no markdown fences, no preamble, no commentary before or after) of objects, each with exactly these fields:
title, organization, type (one of "Conference", "Corporate Program", "Association Event", "Podcast", "CFP/Call for Speakers", "Other"), region ("Australia" or "USA"), date, deadline, url, fit_reason (one sentence, specific to why a trust-based sales coach fits this).
If you cannot find real results, return an empty array []. Do not invent URLs or events — only include what your searches actually surfaced."""

    max_attempts = 4
    for attempt in range(1, max_attempts + 1):
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 16000,
                "system": system_prompt,
                "messages": [
                    {
                        "role": "user",
                        "content": f"Find current speaking opportunities for a trust-based sales coach. Topics: {topics_str}. Market: {region}.",
                    }
                ],
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            },
            timeout=180,
        )

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == max_attempts:
                resp.raise_for_status()
            retry_after = resp.headers.get("retry-after")
            wait = float(retry_after) if retry_after else (2 ** attempt) * 5
            print(f"Got {resp.status_code} from Anthropic API (attempt {attempt}/{max_attempts}) — waiting {wait:.0f}s before retry.", file=sys.stderr)
            time.sleep(wait)
            continue

        resp.raise_for_status()
        break
    data = resp.json()

    if data.get("stop_reason") == "max_tokens":
        print("Warning: response was cut off at the token limit — JSON may be truncated.", file=sys.stderr)

    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    if not text_blocks:
        return []

    items = extract_json(text_blocks[-1])
    if items is None:
        items = extract_json("\n".join(text_blocks))
    if items is None:
        joined = "\n".join(text_blocks)
        print("Warning: could not parse a JSON array from Claude's response.", file=sys.stderr)
        print(f"Response length: {len(joined)} chars. Last 300 chars:", file=sys.stderr)
        print(joined[-300:], file=sys.stderr)
        return []

    out = []
    for it in items:
        if not it.get("title"):
            continue
        it["id"] = item_id(it)
        out.append(it)
    return out


def save_results(enriched_items, region, topics):
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "region": region,
        "topics": topics,
        "opportunities": enriched_items,
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2))


def update_google_sheet(enriched_items):
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_SHEET_ID:
        print("Google Sheets credentials not set — skipping sheet update.", file=sys.stderr)
        return
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        print("gspread/google-auth not installed — skipping sheet update.", file=sys.stderr)
        return

    try:
        creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1

        header = ["Date Added", "New", "Title", "Organization", "Type", "Region", "Event Date", "Apply By", "URL", "Why It Fits"]
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = [header]
        for it in enriched_items:
            rows.append([
                today_str,
                "Yes" if it.get("isNew") else "",
                it.get("title", ""),
                it.get("organization", ""),
                it.get("type", ""),
                it.get("region", ""),
                it.get("date", ""),
                it.get("deadline", ""),
                it.get("url", ""),
                it.get("fit_reason", ""),
            ])

        sheet.clear()
        sheet.update(rows, "A1")
        print(f"Google Sheet updated with {len(enriched_items)} rows")
    except Exception as err:
        print(f"Google Sheet update failed: {err}", file=sys.stderr)


def main():
    config = load_config()
    topics = config["topics"]
    region = config.get("region", "Both Australia and the USA")

    seen_ids = load_seen_ids()
    is_first_run = len(seen_ids) == 0

    print(f"Scanning for: {', '.join(topics)} | region: {region}")
    items = run_search(topics, region)
    print(f"Claude returned {len(items)} item(s)")

    new_item_ids = {it["id"] for it in items if it["id"] not in seen_ids} if not is_first_run else set()
    seen_ids.update(it["id"] for it in items)
    save_seen_ids(seen_ids)

    enriched_items = [{**it, "isNew": it["id"] in new_item_ids} for it in items]
    save_results(enriched_items, region, topics)
    update_google_sheet(enriched_items)

    if is_first_run:
        print(f"First run — saved {len(items)} as baseline.")
    elif new_item_ids:
        print(f"{len(new_item_ids)} new opportunit{'y' if len(new_item_ids)==1 else 'ies'} this run.")
    else:
        print("No new opportunities since last scan.")


if __name__ == "__main__":
    main()
