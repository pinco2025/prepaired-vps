"""
Blueprint audit CLI — checks that the globally_open question pool meets the
minimum thresholds defined in an exam blueprint before the test generator is
used in production.

Usage:
    python -m app.tools.audit_blueprint jeem

Exit codes:
    0 — all invariants satisfied
    1 — one or more shortfalls detected (check stderr for details)
"""

from __future__ import annotations

import asyncio
import sys
from typing import Dict, List, Tuple

from sqlalchemy import text

from app.core.blueprints import ExamBlueprint
from app.core.blueprints.jeem import BLUEPRINTS
from app.core.database import AsyncSessionLocal
from app.services.test_generator import _DIV_TO_RAW


async def _count_pool(
    exam: str,
    blueprint: ExamBlueprint,
) -> Tuple[bool, List[str]]:
    """
    For every (subject, div_quota) in the blueprint, query the DB and report:
      - per-chapter counts vs min_pool_per_chapter_div
      - eligible-chapter count vs min_chapters
    Returns (ok, report_lines).
    """
    ok = True
    lines: List[str] = []
    lines.append(f"\n=== {exam} blueprint v{blueprint.version} audit ===\n")

    async with AsyncSessionLocal() as db:
        for sb in blueprint.subjects:
            lines.append(f"── {sb.subject.upper()} ──")
            for quota in sb.quotas:
                aliases = _DIV_TO_RAW.get(quota.div, [quota.div])
                alias_placeholders = ", ".join(f"'{a}'" for a in aliases)

                sql = text(f"""
                    SELECT chapter, COUNT(*) AS cnt
                    FROM questions
                    WHERE subject = :subject
                      AND globally_open IS TRUE
                      AND verification_status = 'verified'
                      AND source_info->>'section_type' IN ({alias_placeholders})
                      AND chapter IS NOT NULL
                    GROUP BY chapter
                    ORDER BY chapter
                """)
                result = await db.execute(sql, {"subject": sb.subject})
                rows = result.fetchall()

                chapter_counts: Dict[str, int] = {r.chapter: r.cnt for r in rows}
                eligible_chapters = list(chapter_counts.keys())

                # Report per-chapter shortfalls
                short_chapters = [
                    (ch, cnt)
                    for ch, cnt in chapter_counts.items()
                    if cnt < blueprint.min_pool_per_chapter_div
                ]
                lines.append(
                    f"  {quota.div}: {len(eligible_chapters)} eligible chapters, "
                    f"need ≥{quota.min_chapters}   "
                    f"(min_pool_per_chapter={blueprint.min_pool_per_chapter_div})"
                )

                if len(eligible_chapters) < quota.min_chapters:
                    deficit = quota.min_chapters - len(eligible_chapters)
                    lines.append(
                        f"  [FAIL] chapter count shortfall: "
                        f"{len(eligible_chapters)} < {quota.min_chapters} (need {deficit} more)"
                    )
                    ok = False
                else:
                    lines.append(f"  [OK]   chapter count: {len(eligible_chapters)} ≥ {quota.min_chapters}")

                if short_chapters:
                    ok = False
                    lines.append(f"  [FAIL] chapters below min_pool ({blueprint.min_pool_per_chapter_div}):")
                    for ch, cnt in short_chapters:
                        lines.append(f"         {ch}: {cnt} questions")
                else:
                    lines.append(f"  [OK]   all chapters have ≥{blueprint.min_pool_per_chapter_div} questions")

                # Total pool size
                total = sum(chapter_counts.values())
                lines.append(f"         total pool: {total} questions, need {quota.count}")
                if total < quota.count:
                    lines.append(f"  [FAIL] total pool {total} < required {quota.count}")
                    ok = False

            lines.append("")

    return ok, lines


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m app.tools.audit_blueprint <exam>", file=sys.stderr)
        print("  e.g. python -m app.tools.audit_blueprint jeem", file=sys.stderr)
        sys.exit(2)

    exam = sys.argv[1].upper()
    blueprint = BLUEPRINTS.get(exam)
    if blueprint is None:
        print(f"Unknown exam '{exam}'. Available: {list(BLUEPRINTS.keys())}", file=sys.stderr)
        sys.exit(2)

    ok, lines = asyncio.run(_count_pool(exam, blueprint))
    for line in lines:
        print(line)

    if ok:
        print("All invariants satisfied.")
        sys.exit(0)
    else:
        print("\nShortfalls detected — fix the DB pool before enabling for production.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
