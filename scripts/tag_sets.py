#!/usr/bin/env python3
"""
Populate the used_in TEXT[] column for each question by fetching set definitions
from GitHub (or local JSON files) and matching legacy question IDs to DB rows.

This is Phase 2 of the data migration, run after populate_from_csv.py.

── How used_in works ──────────────────────────────────────────────────────────
The backend filters questions with:
    WHERE used_in @> ARRAY['condensed']
So every question that belongs to a set must have that set's ID in its used_in
array. A question can belong to multiple sets — arrays handle that naturally.

── Setup ──────────────────────────────────────────────────────────────────────
1. Fill in backend/scripts/sets_sources.json (copy from sets_sources.example.json)
   with the GitHub raw URLs for each set and subject.
2. Run:
       cd backend
       python scripts/tag_sets.py --sources scripts/sets_sources.json

Requires DATABASE_URL in environment. Safe to re-run (idempotent via
NOT (set_id = ANY(used_in)) guard).
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Optional

import asyncpg
from dotenv import load_dotenv

load_dotenv()

# XOR key — same as api/questions.js
OBFUSCATION_KEY = "pRePaIrEd2026sEcReT"


# ── Decrypt ────────────────────────────────────────────────────────────────────

def xor_decrypt(encrypted_b64: str) -> str:
    """Port of the JS decryptData() in api/questions.js."""
    decoded = base64.b64decode(encrypted_b64).decode("utf-8")
    key = OBFUSCATION_KEY
    return "".join(
        chr(ord(c) ^ ord(key[i % len(key)]))
        for i, c in enumerate(decoded)
    )


# ── Fetch ──────────────────────────────────────────────────────────────────────

def fetch_url(url: str) -> str:
    """Synchronous HTTP GET — fine for a one-shot migration script."""
    req = urllib.request.Request(url, headers={"User-Agent": "prepaired-migration/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def github_raw(url: str) -> str:
    """Convert a github.com URL to raw.githubusercontent.com."""
    return (
        url.replace("https://github.com/", "https://raw.githubusercontent.com/")
           .replace("/tree/", "/")
           .replace("/blob/", "/")
    )


def load_question_data(source: dict) -> dict:
    """
    Fetch (and optionally decrypt) a question set from a URL or local file.
    Returns the parsed JSON dict.

    source keys:
        url        — HTTP(S) URL or local file path
        encrypted  — bool, default false
        local      — bool, use local file instead of HTTP (default false)
    """
    raw_url = source.get("url", "")
    is_local = source.get("local", False)
    encrypted = source.get("encrypted", False)

    if is_local:
        path = Path(raw_url)
        if not path.exists():
            raise FileNotFoundError(f"Local file not found: {path}")
        text = path.read_text(encoding="utf-8")
    else:
        url = github_raw(raw_url) if "github.com" in raw_url else raw_url
        text = fetch_url(url)

    if encrypted:
        text = xor_decrypt(text)

    return json.loads(text)


# ── Extract legacy IDs from a parsed question JSON ────────────────────────────

def extract_legacy_ids(data: dict, source: dict) -> list[str]:
    """
    Extract the list of legacy question IDs from a parsed question JSON.

    The id_field is the key inside each question object that holds the legacy ID
    (typically "id" or "uuid").
    The questions_key is where the question array lives (typically "questions").

    For neet-phy format the outer object has no "questions" wrapper —
    set questions_key to "" to treat the root as the array.
    """
    q_key  = source.get("questions_key", "questions")
    id_fld = source.get("id_field",       "id")

    if q_key:
        questions = data.get(q_key, [])
    else:
        # Root is the array (or a dict we need to get values from)
        questions = data if isinstance(data, list) else list(data.values())

    ids = []
    for q in questions:
        if isinstance(q, dict):
            val = q.get(id_fld)
            if val:
                ids.append(str(val))
    return ids


# ── DB update ─────────────────────────────────────────────────────────────────

async def tag_questions(conn, set_id: str, legacy_ids: list[str]) -> tuple[int, list[str]]:
    """
    Add set_id to used_in for all questions whose legacy_id is in legacy_ids.
    Skips rows that already have the set_id (idempotent).

    Returns (updated_count, unmatched_legacy_ids).
    """
    if not legacy_ids:
        return 0, []

    # Update only rows not yet tagged
    result = await conn.fetch(
        """
        UPDATE questions
           SET used_in = array_append(used_in, $1)
         WHERE legacy_id = ANY($2::text[])
           AND NOT ($1 = ANY(used_in))
        RETURNING legacy_id
        """,
        set_id,
        legacy_ids,
    )

    updated_legacy_ids = {r["legacy_id"] for r in result}
    unmatched = [lid for lid in legacy_ids if lid not in updated_legacy_ids]
    return len(result), unmatched


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(sources_path: str, dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("ERROR: DATABASE_URL not set.")
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    sources_file = Path(sources_path)
    if not sources_file.exists():
        sys.exit(f"ERROR: Sources file not found: {sources_file}\n"
                 f"       Copy sets_sources.example.json to sets_sources.json and fill it in.")

    with sources_file.open(encoding="utf-8") as f:
        config: dict = json.load(f)

    print(f"Connecting to database…")
    conn = await asyncpg.connect(db_url)
    print(f"Connected.  dry_run={dry_run}\n")

    total_updated = 0
    all_unmatched: dict[str, list[str]] = {}

    for set_id, set_config in config.items():
        if set_id.startswith("_"):
            continue
        print(f"── Set: {set_id} {'─'*(40-len(set_id))}")

        # A set may have multiple subjects, each with its own URL
        subjects: dict[str, dict] = set_config.get("subjects", {})
        if not subjects and "url" in set_config:
            # Single-URL set (e.g. super-30 / PYQ all subjects in one file)
            subjects = {"_all": set_config}

        set_legacy_ids: list[str] = []

        for subj_label, source in subjects.items():
            print(f"  Fetching {subj_label}…", end=" ", flush=True)
            try:
                data = load_question_data(source)
                ids  = extract_legacy_ids(data, source)
                print(f"{len(ids):,} IDs")
                set_legacy_ids.extend(ids)
            except Exception as e:
                print(f"FAILED — {e}")
                continue

        # Deduplicate (a question might appear in multiple subject files)
        set_legacy_ids = list(dict.fromkeys(set_legacy_ids))
        print(f"  Total unique IDs for '{set_id}': {len(set_legacy_ids):,}")

        if dry_run:
            print(f"  [dry-run] Would tag {len(set_legacy_ids):,} questions with '{set_id}'")
            continue

        updated, unmatched = await tag_questions(conn, set_id, set_legacy_ids)
        total_updated += updated
        print(f"  Tagged: {updated:,}  |  Unmatched: {len(unmatched):,}")

        if unmatched:
            all_unmatched[set_id] = unmatched
            if len(unmatched) <= 10:
                print(f"  Unmatched IDs: {unmatched}")
            else:
                print(f"  First 10 unmatched: {unmatched[:10]}  …")

        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    if not dry_run:
        print("── Summary ──────────────────────────────────────────────")
        print(f"  Total rows updated : {total_updated:,}")
        if all_unmatched:
            print(f"  Sets with unmatched IDs:")
            for sid, ids in all_unmatched.items():
                print(f"    {sid}: {len(ids):,} unmatched")
            print()
            print("  Unmatched means the legacy_id in the GitHub JSON was not found")
            print("  in the questions table. Possible causes:")
            print("    • Question was in the set JSON but not in the CSV export")
            print("    • legacy_id format mismatch (check id_field in sources config)")
            print()

        # Verify: count questions with non-empty used_in
        tagged = await conn.fetchval(
            "SELECT COUNT(*) FROM questions WHERE array_length(used_in, 1) > 0"
        )
        total  = await conn.fetchval("SELECT COUNT(*) FROM questions")
        print(f"  Questions with used_in populated : {tagged:,} / {total:,}")

    await conn.close()
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Populate used_in arrays by tagging questions to their sets."
    )
    parser.add_argument(
        "--sources",
        default="scripts/sets_sources.json",
        help="Path to the sets sources config JSON (default: scripts/sets_sources.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and count IDs without writing to DB",
    )
    args = parser.parse_args()
    asyncio.run(main(args.sources, args.dry_run))
