"""
Pure question-selection logic for dynamic test generation.

This module has NO side effects (no DB writes, no Supabase calls).
It reads globally_open questions from Postgres and assembles a manifest
describing which question IDs go into which section.

Quota distribution algorithm (per subject, per div):
  1. Query eligible (chapter, question_id) pairs from the globally_open pool.
  2. Discard chapters with zero available questions for this div.
  3. Apply chapter weights from the blueprint (default 1.0 for unlisted chapters).
  4. Distribute the quota across chapters using the largest-remainder method
     (proportional to weights, capped by pool size, each picked chapter gets ≥ min_per_chapter).
  5. If eligible chapters < min_chapters, log a warning and continue with what's available.
  6. If total available < quota, partially fill and log a warning.
  7. Within each chapter slot, apply cluster-first / year-spread selection:
       a) Group questions by cluster_assignment (NULL → singleton per question).
       b) Shuffle cluster order and intra-cluster lists with the seeded RNG.
       c) Round-robin across clusters; within each cluster, pick the question
          whose year is least represented in the subject paper so far (tiebreak
          by shuffle order, so deterministic for a given seed).
       d) year_counts is maintained subject-wide across all divs.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blueprints import DivQuota, ExamBlueprint, SubjectBlueprint

logger = logging.getLogger(__name__)

# ── Div → raw section_type alias lists ───────────────────────────────────────
# Inversion of the _normalise_div alias map in question_service.py.
# Used to build the SQL IN(...) filter so we hit the JSONB index instead of
# pulling every row and filtering in Python.
_DIV_TO_RAW: Dict[str, List[str]] = {
    "div1": ["div1", "d1", "section_a", "sec_a", "sectiona", "mcq", "single",
             "single_correct", "sc", "sca", "singlechoice", "single_choice"],
    "div2": ["div2", "d2", "section_b", "sec_b", "sectionb", "integer", "int",
             "integer_type", "integertype"],
    "div3": ["div3", "d3", "decimal", "numerical", "section_c", "sec_c", "dec", "numeric"],
    "div4": ["div4", "d4", "matrix", "matrix_match", "matrix_matching", "matching",
             "matrixmatch", "match"],
    "div5": ["div5", "d5", "paragraph", "comprehension", "para", "passage", "reading", "rc"],
    "div8": ["div8", "d8", "multi_correct", "multicorrect", "multiple_correct",
             "multi", "mc", "msq", "multiple", "multiple_choice", "multiplechoice"],
}


# ── Output types ──────────────────────────────────────────────────────────────

@dataclass
class SectionManifest:
    subject: str
    div: str
    section_name: str
    question_ids: List[str]


@dataclass
class GeneratedManifest:
    exam: str
    blueprint_version: str
    seed: int
    sections: List[SectionManifest]
    warnings: List[str] = field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _section_name(subject_label: str, div: str) -> str:
    suffix_map = {"div1": "Section A", "div2": "Section B"}
    return f"{subject_label} - {suffix_map.get(div, div.upper())}"


def _largest_remainder(
    weights: List[float],
    total: int,
    caps: List[int],
    min_per: int,
) -> List[int]:
    """
    Distribute `total` integer slots across N buckets proportionally to `weights`,
    capped by `caps[i]`, each receiving at least `min_per` if cap allows.

    Returns a list of integer allocations summing to min(total, sum(caps)).
    """
    n = len(weights)
    if n == 0 or total == 0:
        return []

    # Clamp total to what the pool actually has
    available_total = min(total, sum(caps))
    if available_total == 0:
        return [0] * n

    # Enforce minimums first (where cap allows)
    alloc = [min(min_per, caps[i]) for i in range(n)]
    remaining = available_total - sum(alloc)
    if remaining < 0:
        # Can't even satisfy minimums — scale down proportionally
        alloc = [0] * n
        remaining = available_total

    # Distribute remainder proportionally
    weight_sum = sum(weights)
    if weight_sum == 0:
        weights = [1.0] * n
        weight_sum = float(n)

    exact = [
        weights[i] / weight_sum * remaining
        for i in range(n)
    ]
    floored = [int(math.floor(x)) for x in exact]
    remainders = [(exact[i] - floored[i], i) for i in range(n)]

    # Apply cap on floored values first
    floored = [min(floored[i], caps[i] - alloc[i]) for i in range(n)]

    extra = remaining - sum(floored)
    # Give extra slots to highest-remainder buckets (that still have cap room)
    remainders.sort(reverse=True)
    for _, i in remainders:
        if extra <= 0:
            break
        headroom = caps[i] - alloc[i] - floored[i]
        if headroom > 0:
            give = min(extra, headroom)
            floored[i] += give
            extra -= give

    return [alloc[i] + floored[i] for i in range(n)]


async def _fetch_chapter_pool(
    db: AsyncSession,
    subject: str,
    div: str,
    *,
    chapter_whitelist: Optional[List[str]] = None,
) -> Dict[str, List[tuple]]:
    """
    Return {chapter_code: [(qid, cluster_assignment, year), ...]} for globally_open
    verified questions matching (subject, div).

    When chapter_whitelist is provided, only those chapter codes are included.
    """
    aliases = _DIV_TO_RAW.get(div, [div])
    alias_placeholders = ", ".join(f":alias_{i}" for i in range(len(aliases)))
    params: Dict[str, object] = {"subject": subject}
    for i, alias in enumerate(aliases):
        params[f"alias_{i}"] = alias

    chapter_filter = ""
    if chapter_whitelist:
        ch_placeholders = ", ".join(f":ch_{i}" for i in range(len(chapter_whitelist)))
        chapter_filter = f"AND chapter IN ({ch_placeholders})"
        for i, ch in enumerate(chapter_whitelist):
            params[f"ch_{i}"] = ch

    sql = text(f"""
        SELECT chapter, id, cluster_assignment, year
        FROM questions
        WHERE subject = :subject
          AND globally_open IS TRUE
          AND verification_status = 'verified'
          AND source_info->>'section_type' IN ({alias_placeholders})
          AND chapter IS NOT NULL
          {chapter_filter}
    """)
    result = await db.execute(sql, params)
    rows = result.fetchall()

    chapter_pool: Dict[str, List[tuple]] = {}
    for chapter, qid, cluster, year in rows:
        chapter_pool.setdefault(chapter, []).append((qid, cluster, year))
    return chapter_pool


def _pick_questions(
    chapter_pool: Dict[str, List[tuple]],
    quota: DivQuota,
    weights: Dict[str, float],
    rng: random.Random,
    year_counts: Counter,
) -> tuple[List[str], List[str]]:
    """
    Select `quota.count` question IDs using cluster-first, year-spread-tiebreak
    selection within each chapter, with largest-remainder chapter allocation.

    year_counts is a subject-wide Counter({year: n}) shared across all divs of
    the same subject; it is mutated in-place as questions are picked.

    Returns (selected_ids, warnings).
    """
    warnings: List[str] = []
    eligible = {ch: list(entries) for ch, entries in chapter_pool.items() if entries}

    if len(eligible) < quota.min_chapters:
        warnings.append(
            f"div={quota.div}: only {len(eligible)} eligible chapters, "
            f"wanted ≥{quota.min_chapters}"
        )

    if not eligible:
        warnings.append(f"div={quota.div}: no questions available — slot will be empty")
        return [], warnings

    chapters = list(eligible.keys())
    w = [weights.get(ch, 1.0) for ch in chapters]
    caps = [len(eligible[ch]) for ch in chapters]

    allocs = _largest_remainder(w, quota.count, caps, quota.min_per_chapter)

    selected: List[str] = []

    for ch, alloc in zip(chapters, allocs):
        if alloc <= 0:
            continue

        # Group by cluster; NULL cluster → singleton bucket per question
        buckets: Dict[str, List[tuple]] = {}
        for entry in eligible[ch]:
            qid, cluster, _year = entry
            key = cluster if cluster is not None else f"_null_{qid}"
            buckets.setdefault(key, []).append(entry)

        # Seeded-shuffle cluster order and intra-cluster lists
        cluster_order = list(buckets.keys())
        rng.shuffle(cluster_order)
        for cid in cluster_order:
            rng.shuffle(buckets[cid])

        # Round-robin across clusters; within a cluster prefer the least-seen year
        picked_ch: List[str] = []
        i = 0
        while len(picked_ch) < alloc:
            if all(not buckets[c] for c in cluster_order):
                break
            cid = cluster_order[i % len(cluster_order)]
            if buckets[cid]:
                # Pick the question whose year is least represented subject-wide;
                # ties broken by shuffle order (stable min → first in list)
                best = min(buckets[cid], key=lambda e: year_counts[e[2]])
                buckets[cid].remove(best)
                qid, _cluster, year = best
                picked_ch.append(qid)
                year_counts[year] += 1
            i += 1

        selected.extend(picked_ch)

    if len(selected) < quota.count:
        warnings.append(
            f"div={quota.div}: pool exhausted — got {len(selected)}, wanted {quota.count}"
        )

    return selected, warnings


# ── Public API ────────────────────────────────────────────────────────────────

_SUBJECT_LABELS = {
    "physics": "Physics",
    "chemistry": "Chemistry",
    "mathematics": "Mathematics",
    "zoology": "Zoology",
    "botany": "Botany",
}

# Section render order mirrors _JEEM_SUBJECTS / _JEEM_SECTIONS in question_service.py
_JEEM_SECTION_ORDER = [
    ("physics",     "div1"),
    ("physics",     "div2"),
    ("chemistry",   "div1"),
    ("chemistry",   "div2"),
    ("mathematics", "div1"),
    ("mathematics", "div2"),
]


async def generate(
    db: AsyncSession,
    blueprint: ExamBlueprint,
    *,
    seed: Optional[int] = None,
    chapter_whitelists: Optional[Dict[str, List[str]]] = None,
) -> GeneratedManifest:
    """
    Assemble a question manifest for the given blueprint.

    Steps:
      1. For each (subject, div_quota), fetch the globally_open question pool from Postgres.
      2. Apply chapter weights + largest-remainder distribution.
      3. Random-sample within each chapter slot using the seeded RNG.
      4. Return a GeneratedManifest (no DB writes).

    chapter_whitelists: optional {subject_lower: [chapter_code, ...]} to hard-restrict
    the SQL pool to only those chapters (used for custom single-subject tests).
    """
    if seed is None:
        seed = int(time.time() * 1000) & 0xFFFF_FFFF
    rng = random.Random(seed)

    all_warnings: List[str] = []

    # Pre-fetch pools per (subject, div) in one pass per combination
    subj_div_pool: Dict[tuple, Dict[str, List[tuple]]] = {}
    for sb in blueprint.subjects:
        whitelist = (chapter_whitelists or {}).get(sb.subject)
        for quota in sb.quotas:
            key = (sb.subject, quota.div)
            subj_div_pool[key] = await _fetch_chapter_pool(
                db, sb.subject, quota.div, chapter_whitelist=whitelist
            )

    # Build section manifests in blueprint-defined order
    sections_map: Dict[tuple, SectionManifest] = {}
    for sb in blueprint.subjects:
        label = _SUBJECT_LABELS.get(sb.subject, sb.subject.capitalize())
        year_counts: Counter = Counter()  # shared across divs for this subject
        for quota in sb.quotas:
            key = (sb.subject, quota.div)
            pool = subj_div_pool[key]
            ids, warns = _pick_questions(pool, quota, sb.chapter_weights, rng, year_counts)
            for w in warns:
                all_warnings.append(f"[{sb.subject}] {w}")
                logger.warning("test_generator [%s/%s]: %s", sb.subject, quota.div, w)

            sections_map[key] = SectionManifest(
                subject=sb.subject,
                div=quota.div,
                section_name=_section_name(label, quota.div),
                question_ids=ids,
            )

    # Order sections for JEEM; for other exams extend this logic
    if blueprint.exam == "JEEM":
        ordered_sections = [
            sections_map[(subj, div)]
            for subj, div in _JEEM_SECTION_ORDER
            if (subj, div) in sections_map
        ]
    else:
        ordered_sections = list(sections_map.values())

    # Integrity assertions — these are generator bugs, not pool shortfalls
    all_ids = [qid for s in ordered_sections for qid in s.question_ids]
    if len(set(all_ids)) < len(all_ids):
        dupes = len(all_ids) - len(set(all_ids))
        raise RuntimeError(f"generate: {dupes} duplicate question ID(s) in manifest")
    for sb in blueprint.subjects:
        for quota in sb.quotas:
            actual = sum(
                len(s.question_ids)
                for s in ordered_sections
                if s.subject == sb.subject and s.div == quota.div
            )
            if actual > quota.count:
                raise RuntimeError(
                    f"generate: {sb.subject}/{quota.div} has {actual} questions, quota is {quota.count}"
                )

    return GeneratedManifest(
        exam=blueprint.exam,
        blueprint_version=blueprint.version,
        seed=seed,
        sections=ordered_sections,
        warnings=all_warnings,
    )
