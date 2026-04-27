#!/usr/bin/env python3
"""
Import JEE Mains PYQ questions + solutions from CSV/questions.csv and CSV/solutions.csv.

Run from the backend/ directory:
    python scripts/populate_jmpyq.py
    python scripts/populate_jmpyq.py --csv-dir /path/to/other/dir

Requires DATABASE_URL in .env (same as the FastAPI app).
Safe to re-run — uses ON CONFLICT (id) DO NOTHING for both tables.

Column mapping (questions):
  CSV column          → DB column
  ─────────────────────────────────────────────────────────────────────
  id                  → id                  (UUID v7)
  legacy_id           → legacy_id
  question  (JSONB)   → question
  options   (JSONB)   → options
  answer              → answer
  type                → type
  year                → year
  chapter             → chapter
  cluster_assignment  → cluster_assignment  (already populated in CSV)
  attributes (JSONB):
    .difficulty       → source_info.difficulty
    .additional_param → source_info.source_code
    .extra_param      → source_info.source_q_no
  metadata (JSONB):
    .question_type    → source_info.section_type
    .legacy_table     → source_info.legacy_table
  flags     (JSONB)   → flags
  stats     (JSONB)   → stats
  links               → links
  verification_status → verification_status
  created_at          → created_at
  updated_at          → updated_at
  [derived]           → subject             (chapters.json lookup)
  [default]           → globally_open = False
  [default]           → used_in = []

Column mapping (solutions):
  CSV column          → DB column
  ─────────────────────────────────────────────────────────────────────
  id                  → id  (FK → questions.id)
  explanation         → explanation
  solution_image_url  → solution_image_url
  metadata (JSONB)    → additional_data
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

CHAPTERS_JSON = Path(__file__).parent.parent / "chapters.json"
BATCH_SIZE = 500

DEFAULT_CSV_DIR = Path(__file__).parent.parent / "CSV"


# ── Subject map ────────────────────────────────────────────────────────────────

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

    overrides: dict[str, str | None] = {
        "AOI2":   "physics",
        "ATM":    "physics",
        "CEL":    "biology",
        "CMS":    "physics",
        "DAB":    None,
        "ENV":    "biology",
        "EQL":    "chemistry",
        "GEO":    "mathematics",
        "GPP":    "biology",
        "HAD":    "chemistry",
        "HYD":    "chemistry",
        "KTN":    "physics",
        "LPG":    None,
        "MAT":    "mathematics",
        "MAT02":  "mathematics",
        "MNN":    None,
        "MRS":    None,
        "PLY":    "mathematics",
        "PMI":    "mathematics",
        "POT":    "physics",
        "QEN":    None,
        "QEN1":   "mathematics",
        "ROTA":   "physics",
        "SAS":    "mathematics",
        "SBE":    "biology",
        "SEF":    None,
        "SFC":    "chemistry",
        "SLS":    None,
        "STM":    "mathematics",
        "TIP":    None,
        "WPE":    "physics",
        "Waves":  "physics",
        "MAPPING_PENDING": None,
        "ERROR":  None,
    }
    mapping.update(overrides)
    return mapping


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _null_empty(val) -> str | None:
    return val if val else None


def _normalise_image_url(url) -> str | None:
    return url if url else None


# ── Row transformers ───────────────────────────────────────────────────────────

def transform_question(row: dict, subject_map: dict) -> tuple:
    attrs         = json.loads(row.get("attributes") or "{}")
    meta          = json.loads(row.get("metadata")   or "{}")
    flags         = json.loads(row.get("flags")       or "{}")
    stats         = json.loads(row.get("stats")       or "{}")
    question_json = json.loads(row.get("question")    or "{}")
    options_json  = json.loads(row.get("options")     or "{}")

    # Normalise empty image_url strings → null
    question_json["image_url"] = _normalise_image_url(question_json.get("image_url"))
    for key in ("A", "B", "C", "D"):
        if key in options_json and isinstance(options_json[key], dict):
            options_json[key]["image_url"] = _normalise_image_url(
                options_json[key].get("image_url")
            )

    source_info = {
        "source_code":  _null_empty(attrs.get("additional_param")),
        "source_q_no":  _null_empty(attrs.get("extra_param")),
        "difficulty":   _null_empty(attrs.get("difficulty")),
        "section_type": _null_empty(meta.get("question_type")),
        "legacy_table": _null_empty(meta.get("legacy_table")),
    }

    chapter = _null_empty(row.get("chapter"))
    subject = subject_map.get(chapter) if chapter else None

    year_raw = row.get("year", "")
    year = int(year_raw) if year_raw and year_raw.strip().isdigit() else None

    return (
        row["id"],                                          # $1  id
        _null_empty(row.get("legacy_id")),                  # $2  legacy_id
        json.dumps(question_json),                          # $3  question JSONB
        json.dumps(options_json),                           # $4  options  JSONB
        row["answer"],                                      # $5  answer
        _null_empty(row.get("type")),                       # $6  type
        year,                                               # $7  year
        chapter,                                            # $8  chapter
        subject,                                            # $9  subject
        _null_empty(row.get("cluster_assignment")),         # $10 cluster_assignment
        json.dumps(source_info),                            # $11 source_info JSONB
        json.dumps(flags),                                  # $12 flags JSONB
        json.dumps(stats),                                  # $13 stats JSONB
        _null_empty(row.get("links")),                      # $14 links
        row.get("verification_status") or "pending",        # $15 verification_status
        False,                                              # $16 globally_open
        [],                                                 # $17 used_in TEXT[]
        _parse_ts(row.get("created_at")),                   # $18 created_at
        _parse_ts(row.get("updated_at")),                   # $19 updated_at
    )


def transform_solution(row: dict) -> tuple:
    return (
        row["id"],                                          # $1  id (FK → questions.id)
        row.get("explanation") or "",                       # $2  explanation
        _null_empty(row.get("solution_image_url")),         # $3  solution_image_url
        row.get("metadata") or "{}",                        # $4  additional_data JSONB
    )


# ── SQL ────────────────────────────────────────────────────────────────────────

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
    INSERT INTO solutions (id, explanation, solution_image_url, additional_data)
    VALUES ($1, $2, $3, $4::jsonb)
    ON CONFLICT (id) DO NOTHING
"""


# ── Insert helpers ─────────────────────────────────────────────────────────────

async def insert_batched(conn, sql: str, records: list, label: str) -> int:
    total = len(records)
    done  = 0
    for i in range(0, total, BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        async with conn.transaction():
            await conn.executemany(sql, batch)
        done += len(batch)
        print(f"  {done:,}/{total:,} {label}", end="\r")
    print()
    return total


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(csv_dir: Path) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("ERROR: DATABASE_URL not set in environment / .env")

    # asyncpg needs postgresql:// not postgresql+asyncpg://
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    q_path = csv_dir / "questions.csv"
    s_path = csv_dir / "solutions.csv"
    for p in (q_path, s_path):
        if not p.exists():
            sys.exit(f"ERROR: {p} not found.")

    if not CHAPTERS_JSON.exists():
        sys.exit(f"ERROR: {CHAPTERS_JSON} not found.")

    subject_map = build_subject_map(CHAPTERS_JSON)

    # ── Load questions ─────────────────────────────────────────────────────────
    print(f"Loading {q_path} …")
    q_records: list[tuple] = []
    inserted_ids: set[str] = set()
    unmapped_chapters: set[str] = set()

    with q_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            record = transform_question(row, subject_map)
            chapter = row.get("chapter")
            if chapter and record[8] is None:   # subject is index 8
                unmapped_chapters.add(chapter)
            q_records.append(record)
            inserted_ids.add(row["id"])

    print(f"  Loaded {len(q_records):,} question rows")
    if unmapped_chapters:
        print(
            f"  WARNING: {len(unmapped_chapters)} chapter code(s) have no subject mapping "
            f"(will be NULL): {sorted(unmapped_chapters)}"
        )

    # ── Load solutions — filter to only those with a matching question ─────────
    # The solutions export may contain rows for all questions in the DB.
    # Only import solutions whose question ID is in this CSV batch to avoid FK errors.
    print(f"\nLoading {s_path} …")
    s_records: list[tuple] = []
    s_skipped = 0

    with s_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row["id"] in inserted_ids:
                s_records.append(transform_solution(row))
            else:
                s_skipped += 1

    print(f"  Loaded  {len(s_records):,} solution rows (matched to questions in this CSV)")
    if s_skipped:
        print(f"  Skipped {s_skipped:,} solution rows (no matching question in this CSV batch)")

    # ── Connect & insert ───────────────────────────────────────────────────────
    print(f"\nConnecting to database …")
    conn = await asyncpg.connect(db_url)
    print("Connected.\n")

    print("Inserting questions …")
    await insert_batched(conn, QUESTION_INSERT, q_records, "questions")

    print("Inserting solutions …")
    await insert_batched(conn, SOLUTION_INSERT, s_records, "solutions")

    # ── Validation ─────────────────────────────────────────────────────────────
    db_q = await conn.fetchval("SELECT COUNT(*) FROM questions")
    db_s = await conn.fetchval("SELECT COUNT(*) FROM solutions")

    print("\n── Validation ──────────────────────────────────────────────")
    print(f"  questions table : {db_q:,} rows total")
    print(f"  solutions table : {db_s:,} rows total")
    print(f"  CSV batch       : {len(q_records):,} questions, {len(s_records):,} solutions")

    await conn.close()
    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import JMPYQ questions + solutions into PostgreSQL from CSV exports."
    )
    parser.add_argument(
        "--csv-dir",
        default=str(DEFAULT_CSV_DIR),
        help=f"Directory containing questions.csv and solutions.csv (default: {DEFAULT_CSV_DIR})",
    )
    args = parser.parse_args()
    asyncio.run(main(Path(args.csv_dir)))
