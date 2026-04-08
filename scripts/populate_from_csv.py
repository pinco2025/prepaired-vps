#!/usr/bin/env python3
"""
Populate PostgreSQL questions + solutions tables from exported CSV files.

Run from the backend/ directory:
    python scripts/populate_from_csv.py --csv-dir /path/to/drive-download/

Requires DATABASE_URL in environment (same .env as the FastAPI app).
Safe to re-run — uses ON CONFLICT (id) DO NOTHING.
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

CHAPTERS_JSON = Path(__file__).parent.parent / "chapters.json"
BATCH_SIZE = 500


# ── Subject mapping ────────────────────────────────────────────────────────────

def build_subject_map(chapters_file: Path) -> dict[str, str | None]:
    """
    Build chapter_code -> subject from chapters.json.
    Botany/Zoology both collapse to 'biology'.
    Manual overrides fill gaps for codes present in the CSV but absent from the JSON.
    """
    with chapters_file.open(encoding="utf-8") as f:
        data = json.load(f)

    mapping: dict[str, str | None] = {}
    for subject, chapters in data.items():
        subj = subject.lower()
        if subj in ("botany", "zoology"):
            subj = "biology"
        for ch in chapters:
            mapping[ch["code"]] = subj

    # Manual overrides for codes found in the CSV but missing from chapters.json.
    # Set to None if the subject is genuinely unknown — rows will be inserted with
    # subject = NULL and can be patched later with:
    #   UPDATE questions SET subject = 'physics' WHERE chapter = 'ROTA';
    overrides: dict[str, str | None] = {
        "AOI2": "physics",      # variant of AOI (Ray Optics)
        "ATM": "physics",       # Atmosphere / fluid statics
        "CEL": "biology",       # Cell Biology
        "CMS": "physics",       # Center of Mass (variant spelling)
        "DAB": None,
        "ENV": "biology",       # Environment & Ecology
        "EQL": "chemistry",     # Chemical Equilibrium
        "GEO": "mathematics",   # Geometry
        "GPP": "biology",       # Genetics & Population (NEET)
        "HAD": "chemistry",     # Haloalkanes & Derivatives
        "HYD": "chemistry",     # Hydrocarbons
        "KTN": "physics",       # Kinematics (variant of KNT)
        "LPG": None,
        "MAT": "mathematics",
        "MAT02": "mathematics",
        "MNN": None,
        "MRS": None,
        "PLY": "mathematics",   # Polynomials
        "PMI": "mathematics",   # Principle of Mathematical Induction
        "POT": "physics",       # Electric Potential / Gravitational Potential
        "QEN": None,
        "ROTA": "physics",      # Rotation (variant of ROT)
        "SAS": "mathematics",   # Statistics / Sets
        "SBE": "biology",
        "SEF": None,
        "SFC": "chemistry",     # Surface Chemistry
        "SLS": None,
        "STM": "mathematics",   # Straight Lines / Matrices variant
        "TIP": None,
        "WPE": "physics",       # Work, Power & Energy (variant of WEP)
        "Waves": "physics",
        "MAPPING_PENDING": None,
        "ERROR": None,
    }
    mapping.update(overrides)
    return mapping


# ── Row transformers ───────────────────────────────────────────────────────────

def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    return datetime.fromisoformat(raw)


def transform_question(row: dict, subject_map: dict) -> tuple:
    """
    CSV columns → DB columns.

    Column mapping:
      id                → id            (UUID v7, already generated — used as-is)
      legacy_id         → legacy_id     (old short hex ID, e.g. '0361a6e8730f')
      question  (JSONB) → question      ({"text": ..., "image_url": ...})
      options   (JSONB) → options       ({"A": {"text":..., "image_url":...}, ...})
      answer            → answer
      type              → type
      year              → year          (SMALLINT)
      chapter           → chapter
      [derived]         → subject       (via chapters.json lookup)
      cluster_assignment→ cluster_assignment
      attributes(JSONB) → source_info   (field rename — see below)
        .additional_param → source_code  (was tag_1; Source Material Reference)
        .extra_param      → source_q_no  (was tag_4; Question Number)
        .difficulty       → difficulty
      metadata  (JSONB) → source_info (merged):
        .question_type    → section_type (was division, e.g. div1/D1)
        .legacy_table     → legacy_table (origin table name, informational)
      flags     (JSONB) → flags
      stats     (JSONB) → stats
      links             → links
      verification_status→verification_status
      division_override → (dropped — not in schema)
      class_override    → (dropped — not in schema)
      metadata  (JSONB) → (fully consumed above; raw col dropped)
      created_at        → created_at
      updated_at        → updated_at
      [new]             → globally_open  = False  (set manually later)
      [new]             → used_in        = []     (populate per-set after import)
    """
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
        row["id"],                              # $1  id
        row["legacy_id"] or None,               # $2  legacy_id
        json.dumps(question_json),              # $3  question JSONB
        json.dumps(options_json),               # $4  options  JSONB
        row["answer"],                          # $5  answer
        row["type"]   or None,                  # $6  type
        year,                                   # $7  year
        chapter,                                # $8  chapter
        subject,                                # $9  subject
        row["cluster_assignment"] or None,      # $10 cluster_assignment
        json.dumps(source_info),                # $11 source_info JSONB
        json.dumps(flags),                      # $12 flags JSONB
        json.dumps(stats),                      # $13 stats JSONB
        row["links"] or None,                   # $14 links
        row["verification_status"] or "pending",# $15 verification_status
        False,                                  # $16 globally_open
        [],                                     # $17 used_in TEXT[]
        _parse_ts(row.get("created_at")),       # $18 created_at
        _parse_ts(row.get("updated_at")),       # $19 updated_at
    )


def transform_solution(row: dict) -> tuple:
    """
    solutions CSV is a direct 1-to-1 mapping — id FK = same UUID as questions.id.
    """
    return (
        row["id"],                              # $1  id (FK → questions.id)
        row["explanation"] or "",              # $2  explanation
        row["solution_image_url"] or None,     # $3  solution_image_url
        row["metadata"] or "{}",               # $4  metadata JSONB
    )


# ── Insert helpers ─────────────────────────────────────────────────────────────

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


async def insert_batched(conn, sql: str, records: list, label: str) -> int:
    total = len(records)
    done  = 0
    for i in range(0, total, BATCH_SIZE):
        batch = records[i : i + BATCH_SIZE]
        async with conn.transaction():
            await conn.executemany(sql, batch)
        done += len(batch)
        print(f"  {done:,}/{total:,} {label} inserted", end="\r")
    print()  # newline after \r
    return total


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(csv_dir: str) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("ERROR: DATABASE_URL environment variable not set.")

    # asyncpg uses postgresql:// not postgresql+asyncpg://
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    print(f"Connecting to database…")
    conn = await asyncpg.connect(db_url)
    print("Connected.\n")

    subject_map = build_subject_map(CHAPTERS_JSON)

    questions_path = Path(csv_dir) / "questions.csv"
    solutions_path = Path(csv_dir) / "solutions.csv"

    # ── Load & transform questions ─────────────────────────────────────────────
    print(f"Loading {questions_path} …")
    q_records: list[tuple] = []
    unmapped_chapters: set[str] = set()

    with questions_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            record = transform_question(row, subject_map)
            chapter = row["chapter"]
            if chapter and record[8] is None:   # subject is index 8
                unmapped_chapters.add(chapter)
            q_records.append(record)

    print(f"  Loaded {len(q_records):,} question rows")
    if unmapped_chapters:
        print(
            f"  WARNING: {len(unmapped_chapters)} chapter codes have no subject mapping "
            f"and will be inserted with subject = NULL:\n"
            f"    {sorted(unmapped_chapters)}\n"
            f"  Fix after import:\n"
            f"    UPDATE questions SET subject = '<subject>' WHERE chapter = '<CODE>';\n"
        )

    # ── Load & transform solutions ─────────────────────────────────────────────
    print(f"Loading {solutions_path} …")
    s_records: list[tuple] = []

    with solutions_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            s_records.append(transform_solution(row))

    print(f"  Loaded {len(s_records):,} solution rows\n")

    # ── Insert ─────────────────────────────────────────────────────────────────
    print("Inserting questions…")
    await insert_batched(conn, QUESTION_INSERT, q_records, "questions")

    print("Inserting solutions…")
    await insert_batched(conn, SOLUTION_INSERT, s_records, "solutions")

    # ── Validate ───────────────────────────────────────────────────────────────
    db_q = await conn.fetchval("SELECT COUNT(*) FROM questions")
    db_s = await conn.fetchval("SELECT COUNT(*) FROM solutions")

    print("\n── Validation ──────────────────────────────────────────────")
    print(f"  questions table : {db_q:,} rows")
    print(f"  solutions table : {db_s:,} rows")
    print(f"  CSV source      : {len(q_records):,} questions, {len(s_records):,} solutions")

    if db_q < len(q_records):
        skipped = len(q_records) - db_q
        print(f"  NOTE: {skipped:,} question rows skipped (already existed — ON CONFLICT DO NOTHING)")
    if db_s < len(s_records):
        skipped = len(s_records) - db_s
        print(f"  NOTE: {skipped:,} solution rows skipped (already existed — ON CONFLICT DO NOTHING)")

    await conn.close()
    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate questions + solutions tables from CSV export.")
    parser.add_argument(
        "--csv-dir",
        required=True,
        help="Directory containing questions.csv and solutions.csv",
    )
    args = parser.parse_args()
    asyncio.run(main(args.csv_dir))
