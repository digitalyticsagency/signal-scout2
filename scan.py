#!/usr/bin/env python3
"""
Signal — Speaking Opportunity Scout (autonomous weekly runner)

Runs on a schedule (see .github/workflows/weekly-scan.yml), asks Claude to
web-search for speaking opportunities, diffs the results against what was
found last time, and pushes anything new to Slack.

State (which opportunities we've already seen) is kept in seen_ids.json,
which this script rewrites and the workflow commits back to the repo —
that's what makes "new since last time" possible on a stateless runner.
"""

import json
import os
import sys
import hashlib
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "seen_ids.json"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")

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
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def run_search(topics, region):
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY environment variable is not set.")

    today_str = datetime.now().strftime("%A, %B %d, %Y")
    topics_str = ", ".join(topics)

    system_prompt = f"""You are a research assistant helping a professional speaker find real, currently relevant speaking opportunities using web search.
Search for: upcoming conferences, association events, corporate speaker programs, calls-for-speakers/calls-for-proposals, and guest-podcast opportunities relevant to the topics: {topics_str}. Market: {region}. Today's date is {today_str}. Only include things that are upcoming or currently accepting applications — do not include past events.
After you finish searching, respond with ONLY a JSON array (no markdown fences, no preamble, no commentary before or after) of up to 8 objects, each with exactly these fields:
title, organization, type (one of "Conference", "Corporate Program", "Association Event", "Podcast", "CFP/Call for Speakers", "Other"), region ("Australia" or "USA"), date, deadline, url, fit_reason (one sentence, specific to why a trust-based sales coach fits this).
If you cannot find real results, return an empty array []. Do not invent URLs or events — only include what your searches actually surfaced."""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 4096,
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
    resp.raise_for_status()
    data = resp.json()

    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    if not text_blocks:
        return []

    items = extract_json(text_blocks[-1])
    if items is None:
        items = extract_json("\n".join(text_blocks))
    if items is None:
        print("Warning: could not parse a JSON array from Claude's response.", file=sys.stderr)
        return []

    out = []
    for it in items:
        if not it.get("title"):
            continue
        it["id"] = item_id(it)
        out.append(it)
    return out


def send_slack(new_items, region):
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL not set — skipping Slack.", file=sys.stderr)
        return
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📡 Signal: {len(new_items)} new speaking opportunit{'y' if len(new_items)==1 else 'ies'}"}},
    ]
    for it in new_items[:20]:
        text = (
            f"*<{it.get('url','')}|{it.get('title','Untitled')}>*\n"
            f"{it.get('organization','Unknown org')} · {it.get('type','')} · {region}\n"
            f"Event: {it.get('date','TBD')}  |  Apply by: {it.get('deadline','Not specified')}\n"
            f"_{it.get('fit_reason','')}_"
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
        blocks.append({"type": "divider"})
    resp = requests.post(SLACK_WEBHOOK_URL, json={"blocks": blocks}, timeout=30)
    if resp.status_code != 200:
        print(f"Slack post failed: {resp.status_code} {resp.text}", file=sys.stderr)
    else:
        print("Slack message sent")


def main():
    config = load_config()
    topics = config["topics"]
    region = config.get("region", "Both Australia and the USA")

    seen_ids = load_seen_ids()
    is_first_run = len(seen_ids) == 0

    print(f"Scanning for: {', '.join(topics)} | region: {region}")
    items = run_search(topics, region)
    print(f"Claude returned {len(items)} item(s)")

    new_items = [it for it in items if it["id"] not in seen_ids]
    seen_ids.update(it["id"] for it in items)
    save_seen_ids(seen_ids)

    if is_first_run:
        print(f"First run — saved {len(items)} as baseline, no notifications sent.")
        return

    if not new_items:
        print("No new opportunities since last scan.")
        return

    print(f"{len(new_items)} new opportunit(y/ies) — notifying.")
    send_slack(new_items, region)


if __name__ == "__main__":
    main()
