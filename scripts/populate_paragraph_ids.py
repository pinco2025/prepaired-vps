#!/usr/bin/env python3
"""
Discover unique paragraph groups from the legacy `links` complete graph and
populate the paragraphs table + questions.paragraph_id FK.

Algorithm:
1. Fetch all div5 questions that don't yet have a paragraph_id.
2. Parse the `links` TEXT column (comma-separated legacy_ids) to build an
   adjacency graph of co-paragraph questions.
3. Run union-find to find connected components — each component is one paragraph.
4. For each component: INSERT a row into paragraphs, then UPDATE questions.paragraph_id.
5. Div5 questions with null/empty links get their own solo paragraph row.

Run from the backend/ directory:
    python scripts/populate_paragraph_ids.py
    python scripts/populate_paragraph_ids.py --dry-run

Requires DATABASE_URL in environment (or a .env file at backend/.env).
Safe to re-run — idempotent (skips questions that already have paragraph_id set).
"""

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import asyncpg
import uuid6
from dotenv import load_dotenv

load_dotenv()

# Canonical section_type values that identify paragraph questions
DIV5_ALIASES = ("div5", "d5", "paragraph", "comprehension", "para")


# ── Union-Find ─────────────────────────────────────────────────────────────────

class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, x: str) -> str:
        if x not in self.parent:
            self.parent[x] = x
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])  # path compression
        return self.parent[x]

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> Dict[str, List[str]]:
        """Return {root: [members...]} for all components."""
        result: Dict[str, List[str]] = defaultdict(list)
        for node in self.parent:
            result[self.find(node)].append(node)
        return dict(result)


# ── Core logic ─────────────────────────────────────────────────────────────────

def parse_links(links_text: Optional[str]) -> List[str]:
    """Split the comma-separated links string into a list of legacy_ids."""
    if not links_text:
        return []
    return [lid.strip() for lid in links_text.split(",") if lid.strip()]


async def run(dry_run: bool) -> None:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exit("ERROR: DATABASE_URL is not set.")
    # asyncpg uses the plain postgresql:// scheme
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    print("Connecting to database…")
    conn = await asyncpg.connect(db_url)
    print(f"Connected.  dry_run={dry_run}\n")

    try:
        # ── Step 1: Fetch all div5 questions without paragraph_id ────────────
        rows = await conn.fetch(
            """
            SELECT id, legacy_id, links
            FROM questions
            WHERE (source_info->>'section_type') = ANY($1::text[])
              AND paragraph_id IS NULL
            """,
            list(DIV5_ALIASES),
        )
        print(f"Found {len(rows):,} div5 question(s) without paragraph_id.\n")

        if not rows:
            print("Nothing to do.")
            return

        # ── Step 2: Build adjacency via union-find ───────────────────────────
        uf = UnionFind()
        legacy_to_uuid: Dict[str, str] = {}  # legacy_id → questions.id

        for row in rows:
            leg = row["legacy_id"]
            if not leg:
                # No legacy_id — use the UUID as its own key so it still gets a group
                leg = row["id"]
            legacy_to_uuid[leg] = row["id"]
            uf.find(leg)  # ensure it's in the union-find

        # Connect siblings
        missing_refs: Set[str] = set()
        for row in rows:
            leg = row["legacy_id"] or row["id"]
            linked = parse_links(row["links"])
            for sibling_leg in linked:
                if sibling_leg in legacy_to_uuid:
                    uf.union(leg, sibling_leg)
                else:
                    missing_refs.add(sibling_leg)

        if missing_refs:
            print(
                f"Warning: {len(missing_refs):,} legacy_id(s) referenced in links "
                f"were not found in the div5 query (may belong to other div types or "
                f"be unverified). They will be skipped.\n"
            )

        # ── Step 3: Compute components ───────────────────────────────────────
        components = uf.groups()  # {root_legacy_id: [legacy_ids...]}
        print(f"Discovered {len(components):,} paragraph group(s):")
        counts = sorted([len(v) for v in components.values()], reverse=True)
        for size in sorted(set(counts), reverse=True):
            n = counts.count(size)
            print(f"  {n:4,} group(s) with {size} question(s)")
        print()

        if dry_run:
            total_affected = sum(len(v) for v in components.values())
            print(f"[dry-run] Would create {len(components):,} paragraph row(s).")
            print(f"[dry-run] Would set paragraph_id on {total_affected:,} question(s).")
            return

        # ── Step 4: Insert paragraphs + update questions ─────────────────────
        created = 0
        updated = 0

        async with conn.transaction():
            for root, members in components.items():
                para_id = str(uuid6.uuid7())

                # Insert paragraph row
                await conn.execute(
                    "INSERT INTO paragraphs (id) VALUES ($1)",
                    para_id,
                )
                created += 1

                # Map legacy_ids back to question UUIDs
                question_uuids = [
                    legacy_to_uuid[leg]
                    for leg in members
                    if leg in legacy_to_uuid
                ]

                if not question_uuids:
                    continue

                result = await conn.execute(
                    "UPDATE questions SET paragraph_id = $1 WHERE id = ANY($2::uuid[])",
                    para_id,
                    question_uuids,
                )
                # result is e.g. "UPDATE 4"
                updated += int(result.split()[-1])

        print(f"Created  : {created:,} paragraph row(s)")
        print(f"Updated  : {updated:,} question(s) with paragraph_id")

        # ── Verification ─────────────────────────────────────────────────────
        remaining = await conn.fetchval(
            """
            SELECT COUNT(*) FROM questions
            WHERE (source_info->>'section_type') = ANY($1::text[])
              AND paragraph_id IS NULL
            """,
            list(DIV5_ALIASES),
        )
        print(f"\nVerification: {remaining:,} div5 question(s) still without paragraph_id")
        if remaining > 0:
            print("  (These may have an unrecognised section_type alias — check manually)")

    finally:
        await conn.close()
        print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate paragraphs table from legacy links graph."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing to DB",
    )
    args = parser.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()
