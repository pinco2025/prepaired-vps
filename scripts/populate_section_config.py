#!/usr/bin/env python3
"""
One-time script: populate section_config JSONB on the Supabase tests table.

section_config shape per test:
  {
    "Physics-div1":    { "name": "Physics A",    "pos": 4.0, "neg": 1.0 },
    "Chemistry-div1":  { "name": "Chemistry A",  "pos": 4.0, "neg": 1.0 },
    "Mathematics-div8":{ "name": "Mathematics B", "pos": 4.0, "neg": 2.0 },
    ...
  }

Key = "{questions.subject}-{questions.source_info->>'section_type'}"
Value = section display name + marking scheme from the existing test JSON sections[].

Algorithm:
  1. Fetch all tests from Supabase (testID, url).
  2. For each test, fetch the test JSON from tests.url.
  3. Build a map: section_name → {pos, neg} from the JSON sections[].
  4. For each section, sample one question UUID from that section.
  5. Query Postgres for that question's subject + section_type.
  6. Build the section_config key and PATCH tests.section_config in Supabase.

Prerequisites:
  - Run ALTER TABLE tests ADD COLUMN IF NOT EXISTS section_config JSONB; in Supabase SQL editor first.
  - Set DATABASE_URL, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY in backend/.env

Run from the backend/ directory:
    python scripts/populate_section_config.py
    python scripts/populate_section_config.py --dry-run
    python scripts/populate_section_config.py --test-id AIPT-01  # single test
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv()


def get_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        sys.exit(f"ERROR: {key} is not set in environment.")
    return val


async def fetch_tests_from_supabase(
    sb_url: str, sb_key: str, test_id_filter: Optional[str] = None
) -> list[Dict[str, Any]]:
    params: Dict[str, str] = {"select": "testID,url,section_config"}
    if test_id_filter:
        params["testID"] = f"eq.{test_id_filter}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{sb_url}/rest/v1/tests",
            params=params,
            headers={
                "apikey": sb_key,
                "Authorization": f"Bearer {sb_key}",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def patch_section_config(
    sb_url: str, sb_key: str, test_id: str, section_config: Dict[str, Any]
) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{sb_url}/rest/v1/tests",
            params={"testID": f"eq.{test_id}"},
            headers={
                "apikey": sb_key,
                "Authorization": f"Bearer {sb_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            content=json.dumps({"section_config": section_config}),
        )
        resp.raise_for_status()


async def run(test_id_filter: Optional[str], dry_run: bool) -> None:
    db_url = get_env("DATABASE_URL").replace("postgresql+asyncpg://", "postgresql://")
    sb_url = get_env("SUPABASE_URL").rstrip("/")
    sb_key = get_env("SUPABASE_SERVICE_ROLE_KEY")

    print("Fetching tests from Supabase…")
    tests = await fetch_tests_from_supabase(sb_url, sb_key, test_id_filter)
    print(f"Found {len(tests)} test(s).")

    conn = await asyncpg.connect(db_url)
    print("Connected to Postgres.\n")

    try:
        for test in tests:
            test_id = test["testID"]
            url = test.get("url")

            if not url:
                print(f"[SKIP] {test_id}: no url column set.")
                continue

            # Fetch the test JSON
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    ppt_data = resp.json()
            except Exception as e:
                print(f"[ERROR] {test_id}: could not fetch {url}: {e}")
                continue

            # Build section_name → {pos, neg} from sections[]
            json_sections = ppt_data.get("sections", [])
            if not json_sections:
                print(f"[SKIP] {test_id}: no sections[] in JSON.")
                continue

            section_marks: Dict[str, Dict[str, float]] = {}
            for sec in json_sections:
                name = sec.get("name")
                pos = float(sec.get("marksPerQuestion", 0))
                neg = float(sec.get("negativeMarksPerQuestion") or sec.get("negagiveMarksPerQuestion") or 0)
                if name:
                    section_marks[name] = {"pos": pos, "neg": neg}

            # For each section, find one representative question UUID
            section_sample: Dict[str, str] = {}  # section_name → uuid
            for q in ppt_data.get("questions", []):
                sec_name = q.get("section")
                if sec_name and sec_name not in section_sample and q.get("uuid"):
                    section_sample[sec_name] = q["uuid"]

            # Query Postgres to get subject + section_type for each sample UUID
            sample_uuids = list(section_sample.values())
            if not sample_uuids:
                print(f"[SKIP] {test_id}: no questions with uuid found.")
                continue

            rows = await conn.fetch(
                """
                SELECT legacy_id,
                       subject,
                       source_info->>'section_type' AS section_type
                FROM questions
                WHERE legacy_id = ANY($1::text[])
                """,
                sample_uuids,
            )
            uuid_to_meta = {r["legacy_id"]: r for r in rows}

            # Build section_config
            section_config: Dict[str, Any] = {}
            unresolved = []

            for sec_name, sample_uuid in section_sample.items():
                meta = uuid_to_meta.get(sample_uuid)
                marks = section_marks.get(sec_name, {})

                if not meta:
                    unresolved.append(sec_name)
                    continue

                subject = meta["subject"] or ""
                sec_type = meta["section_type"] or ""

                if not subject or not sec_type:
                    print(f"  [WARN] {test_id} / '{sec_name}': uuid={sample_uuid} missing subject='{subject}' or section_type='{sec_type}'. Skipping.")
                    continue

                key = f"{subject}-{sec_type}"
                section_config[key] = {
                    "name": sec_name,
                    "pos": marks.get("pos", 0),
                    "neg": marks.get("neg", 0),
                }

            if unresolved:
                print(f"  [WARN] {test_id}: {len(unresolved)} section(s) had no matching Postgres question: {unresolved}")

            if not section_config:
                print(f"[SKIP] {test_id}: section_config is empty after resolution.")
                continue

            print(f"[{'DRY-RUN' if dry_run else 'UPDATE'}] {test_id}: {len(section_config)} section(s)")
            for k, v in section_config.items():
                print(f"  {k!r:30s} → {v}")

            if not dry_run:
                await patch_section_config(sb_url, sb_key, test_id, section_config)
                print(f"  ✓ Patched tests.section_config for {test_id}")

            print()

    finally:
        await conn.close()

    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate section_config JSONB on the Supabase tests table."
    )
    parser.add_argument(
        "--test-id",
        default=None,
        help="Restrict to a single test (testID). Omit to process all tests.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without modifying Supabase.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.test_id, args.dry_run))


if __name__ == "__main__":
    main()
