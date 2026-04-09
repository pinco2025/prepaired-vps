#!/usr/bin/env python3
"""
Add SET-ADV-01 into the used_in array for a list of question UIDs.

Usage:
  python update_adv_set.py adv_01_ids.json
"""

import argparse
import asyncio
import json
import os
import sys

import asyncpg
from dotenv import load_dotenv

load_dotenv()

async def tag_questions_by_id(conn, set_id: str, question_ids: list[str]):
    if not question_ids:
        return 0, []

    # Use COALESCE to handle NULL used_in arrays safely
    result = await conn.fetch(
        """
        UPDATE questions
           SET used_in = array_append(COALESCE(used_in, ARRAY[]::text[]), $1)
         WHERE id::text = ANY($2::text[])
           AND (used_in IS NULL OR NOT ($1 = ANY(used_in)))
        RETURNING id
        """,
        set_id,
        question_ids,
    )
    
    updated_ids = {str(r["id"]) for r in result}
    unmatched = [qid for qid in question_ids if qid not in updated_ids]
    return len(result), unmatched

async def main(input_file: str):
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("ERROR: DATABASE_URL not set in .env")
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    
    if not os.path.exists(input_file):
        sys.exit(f"ERROR: File not found: {input_file}")
        
    with open(input_file, "r", encoding="utf-8") as f:
        question_ids = json.load(f)
        
    if not isinstance(question_ids, list):
        sys.exit("ERROR: Input file must contain a JSON array of IDs.")
        
    print(f"Connecting to database...")
    conn = await asyncpg.connect(db_url)
    print(f"Connected.\n")
    
    set_id = "SET-ADV-01"
    
    print(f"Adding '{set_id}' to {len(question_ids)} questions...")
    updated, unmatched = await tag_questions_by_id(conn, set_id, question_ids)
    
    print(f"Successfully tagged: {updated:,}")
    if unmatched:
        print(f"Unmatched or Already tagged: {len(unmatched):,}")
        
    await conn.close()
    print("Done.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add SET-ADV-01 to used_in using IDs from JSON")
    parser.add_argument("input_file", help="Path to JSON file containing array of UUID strings")
    args = parser.parse_args()
    asyncio.run(main(args.input_file))
