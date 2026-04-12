#!/usr/bin/env python3
"""
Import JEE Advanced PYQ questions + solutions and create APYQ test records.

Steps:
  1. Insert questions + solutions into PostgreSQL (ON CONFLICT DO NOTHING)
  2. Patch used_in[] on each question with its derived test_id
  3. Upsert APYQ-* test records into the Supabase tests table

Run from the backend/ directory:
    python scripts/populate_advpyq.py --csv-dir /path/to/dir/with/ADVPYQ_csvs/

Requires in .env:
    DATABASE_URL
    SUPABASE_URL
    SUPABASE_SERVICE_ROLE_KEY
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv()

CHAPTERS_JSON = Path(__file__).parent.parent / "chapters.json"
BATCH_SIZE = 200

# ── JEE Advanced section_config (shared by all 4 papers) ─────────────────────
# Keys follow "{subject}-{div}" pattern used by the scoring engine.
# div1=Single Correct (+3/-1), div2=Integer (+4/0), div3=Decimal (+4/0),
# div4=Match Column (+3/-1), div5=Comprehension (+3/-1), div8=Multi Correct (+4/-2)

JEEA_SECTION_CONFIG = {
    "physics-div1":    {"name": "Physics — Single Correct",  "pos": 3, "neg": -1},
    "chemistry-div1":  {"name": "Chemistry — Single Correct","pos": 3, "neg": -1},
    "mathematics-div1":{"name": "Maths — Single Correct",    "pos": 3, "neg": -1},
    "physics-div2":    {"name": "Physics — Integer",         "pos": 4, "neg": 0},
    "chemistry-div2":  {"name": "Chemistry — Integer",       "pos": 4, "neg": 0},
    "mathematics-div2":{"name": "Maths — Integer",           "pos": 4, "neg": 0},
    "physics-div3":    {"name": "Physics — Numerical",       "pos": 4, "neg": 0},
    "chemistry-div3":  {"name": "Chemistry — Numerical",     "pos": 4, "neg": 0},
    "mathematics-div3":{"name": "Maths — Numerical",         "pos": 4, "neg": 0},
    "physics-div4":    {"name": "Physics — Match Column",    "pos": 3, "neg": -1},
    "chemistry-div4":  {"name": "Chemistry — Match Column",  "pos": 3, "neg": -1},
    "mathematics-div4":{"name": "Maths — Match Column",      "pos": 3, "neg": -1},
    "physics-div5":    {"name": "Physics — Comprehension",   "pos": 3, "neg": -1},
    "chemistry-div5":  {"name": "Chemistry — Comprehension", "pos": 3, "neg": -1},
    "mathematics-div5":{"name": "Maths — Comprehension",     "pos": 3, "neg": -1},
    "physics-div8":    {"name": "Physics — Multi Correct",   "pos": 4, "neg": -2},
    "chemistry-div8":  {"name": "Chemistry — Multi Correct", "pos": 4, "neg": -2},
    "mathematics-div8":{"name": "Maths — Multi Correct",     "pos": 4, "neg": -2},
}

# ── Subject mapping from chapters.json ───────────────────────────────────────

def build_subject_map(chapters_file: Path) -> dict[str, str | None]:
    with chapters_file.open(encoding="utf-8") as f:
        data = json.load(f)
    mapping: dict[str, str | None] = {}
    for subject, chapters in data.items():
        subj = subject.lower()
        if subj in ("botany", "zoology"):
            subj = "biology"
        for ch in chapters:
            mapping[ch["code"]] = subj
    return mapping


# ── test_id derivation ────────────────────────────────────────────────────────

def derive_test_id(additional_param: str | None) -> str | None:
    """'2025_1' → 'APYQ-2025-1', '2024_2' → 'APYQ-2024-2', else None."""
    if not additional_param:
        return None
    parts = additional_param.split("_")
    if len(parts) != 2:
        return None
    year, paper = parts
    if not (year.isdigit() and paper.isdigit()):
        return None
    return f"APYQ-{year}-{paper}"


# ── Row transformers ──────────────────────────────────────────────────────────

def _parse_ts(raw: str | None):
    if not raw:
        return None
    from datetime import datetime
    raw = raw.replace("Z", "+00:00")
    return datetime.fromisoformat(raw)


def transform_question(row: dict, subject_map: dict) -> tuple:
    attrs = json.loads(row["attributes"] or "{}")
    meta  = json.loads(row["metadata"]   or "{}")
    flags = json.loads(row["flags"]      or "{}")
    stats = json.loads(row["stats"]      or "{}")
    question_json = json.loads(row["question"] or "{}")
    options_json  = json.loads(row["options"]  or "{}")

    source_info = {
        "source_code":  attrs.get("additional_param") or None,
        "source_q_no":  attrs.get("extra_param")      or None,
        "difficulty":   attrs.get("difficulty")        or None,
        "section_type": meta.get("question_type")      or None,
        "legacy_table": meta.get("legacy_table")       or None,
    }

    chapter = row["chapter"] or None
    subject = subject_map.get(chapter) if chapter else None

    year_raw = row["year"]
    year = int(year_raw) if year_raw and year_raw.strip().isdigit() else None

    return (
        row["id"],
        row["legacy_id"] or None,
        json.dumps(question_json),
        json.dumps(options_json),
        row["answer"],
        row["type"] or None,
        year,
        chapter,
        subject,
        row["cluster_assignment"] or None,
        json.dumps(source_info),
        json.dumps(flags),
        json.dumps(stats),
        row["links"] or None,
        row["verification_status"] or "pending",
        False,
        [],
        _parse_ts(row.get("created_at")),
        _parse_ts(row.get("updated_at")),
    )


def transform_solution(row: dict) -> tuple:
    return (
        row["id"],
        row["explanation"] or "",
        row["solution_image_url"] or None,
        row["metadata"] or "{}",
    )


# ── Insert helpers ────────────────────────────────────────────────────────────

QUESTION_INSERT = """
    INSERT INTO questions (
        id, legacy_id, question, options, answer, type, year,
        chapter, subject, cluster_assignment, source_info, flags,
        stats, links, verification_status, globally_open, used_in,
        created_at, updated_at
    ) VALUES (
        $1, $2, $3::jsonb, $4::jsonb, $5, $6, $7,
        $8, $9, $10, $11::jsonb, $12::jsonb,
        $13::jsonb, $14, $15, $16, $17,
        $18, $19
    )
    ON CONFLICT (id) DO NOTHING
"""

SOLUTION_INSERT = """
    INSERT INTO solutions (id, explanation, solution_image_url, metadata)
    VALUES ($1, $2, $3, $4::jsonb)
    ON CONFLICT (id) DO NOTHING
"""

PATCH_USED_IN = """
    UPDATE questions
    SET used_in = array(
        SELECT DISTINCT unnest(array_append(used_in, $2::text))
    )
    WHERE id = $1::uuid
      AND NOT ($2::text = ANY(used_in))
"""


async def insert_batched(conn, sql: str, records: list, label: str) -> None:
    total = len(records)
    done = 0
    for i in range(0, total, BATCH_SIZE):
        batch = records[i: i + BATCH_SIZE]
        async with conn.transaction():
            await conn.executemany(sql, batch)
        done += len(batch)
        print(f"  {done:,}/{total:,} {label}", end="\r")
    print()


async def patch_used_in(conn, pairs: list[tuple[str, str]]) -> int:
    updated = 0
    for i in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[i: i + BATCH_SIZE]
        async with conn.transaction():
            for question_id, test_id in batch:
                status = await conn.execute(PATCH_USED_IN, question_id, test_id)
                if status == "UPDATE 1":
                    updated += 1
        print(f"  patched {min(i + BATCH_SIZE, len(pairs)):,}/{len(pairs):,}", end="\r")
    print()
    return updated


# ── Supabase upsert ───────────────────────────────────────────────────────────

def build_test_records(rows: list[dict]) -> list[dict]:
    """Build one test record per unique paper code from the CSV."""
    from collections import Counter
    paper_q_counts: dict[str, int] = {}
    for row in rows:
        attrs = json.loads(row["attributes"] or "{}")
        paper = attrs.get("additional_param")
        if paper:
            paper_q_counts[paper] = paper_q_counts.get(paper, 0) + 1

    records = []
    for paper_code, q_count in sorted(paper_q_counts.items()):
        test_id = derive_test_id(paper_code)
        if not test_id:
            continue
        parts = paper_code.split("_")
        year, paper_num = parts[0], parts[1]
        records.append({
            "testID": test_id,
            "title": f"JEE Advanced {year} Paper {paper_num}",
            "exam": "JEEA",
            "type": "apyq",
            "duration": 180,
            "totalQuestions": q_count,
            "tier": None,
            "section_config": JEEA_SECTION_CONFIG,
        })
    return records


def upsert_tests_to_supabase(records: list[dict]) -> None:
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key  = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not service_key:
        print("  WARNING: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — skipping test upsert.")
        return

    url = f"{supabase_url}/rest/v1/tests"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(url, json=records, headers=headers)

    if not resp.is_success:
        print(f"  ERROR upserting tests: {resp.status_code} {resp.text}")
    else:
        upserted = resp.json() if resp.content else []
        print(f"  Upserted {len(upserted)} test record(s) into Supabase tests table.")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(csv_dir: str) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("ERROR: DATABASE_URL not set.")
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    q_path = Path(csv_dir) / "ADVPYQ_questions.csv"
    s_path = Path(csv_dir) / "ADVPYQ_solutions.csv"
    for p in (q_path, s_path):
        if not p.exists():
            sys.exit(f"ERROR: {p} not found.")

    subject_map = build_subject_map(CHAPTERS_JSON)

    # ── Load CSVs ──────────────────────────────────────────────────────────────
    print(f"Loading {q_path} …")
    q_rows: list[dict] = []
    with q_path.open(encoding="utf-8-sig") as f:
        q_rows = list(csv.DictReader(f))

    print(f"  {len(q_rows):,} question rows")

    unmapped: set[str] = set()
    q_records: list[tuple] = []
    used_in_pairs: list[tuple[str, str]] = []

    for row in q_rows:
        rec = transform_question(row, subject_map)
        if row["chapter"] and rec[8] is None:
            unmapped.add(row["chapter"])
        q_records.append(rec)

        attrs = json.loads(row["attributes"] or "{}")
        test_id = derive_test_id(attrs.get("additional_param"))
        if test_id:
            used_in_pairs.append((row["id"], test_id))

    if unmapped:
        print(f"  WARNING: {len(unmapped)} chapter(s) with no subject mapping "
              f"(will be NULL): {sorted(unmapped)}")

    print(f"Loading {s_path} …")
    with s_path.open(encoding="utf-8-sig") as f:
        s_records = [transform_solution(r) for r in csv.DictReader(f)]
    print(f"  {len(s_records):,} solution rows\n")

    # ── PostgreSQL inserts ─────────────────────────────────────────────────────
    print("Connecting to PostgreSQL …")
    conn = await asyncpg.connect(db_url)
    print("Connected.\n")

    print("Inserting questions …")
    await insert_batched(conn, QUESTION_INSERT, q_records, "questions")

    print("Inserting solutions …")
    await insert_batched(conn, SOLUTION_INSERT, s_records, "solutions")

    print(f"Patching used_in[] on {len(used_in_pairs):,} questions …")
    updated = await patch_used_in(conn, used_in_pairs)
    print(f"  {updated:,} rows updated\n")

    await conn.close()

    # ── Supabase test records ──────────────────────────────────────────────────
    test_records = build_test_records(q_rows)
    print(f"Upserting {len(test_records)} test records to Supabase …")
    for rec in test_records:
        print(f"  {rec['testID']}  {rec['title']}  ({rec['totalQuestions']} questions)")
    upsert_tests_to_supabase(test_records)

    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import ADVPYQ questions/solutions and create APYQ test records."
    )
    parser.add_argument(
        "--csv-dir",
        required=True,
        help="Directory containing ADVPYQ_questions.csv and ADVPYQ_solutions.csv",
    )
    args = parser.parse_args()
    asyncio.run(main(args.csv_dir))
