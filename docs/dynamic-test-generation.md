# Dynamic Test Generation

## Overview

The dynamic test generation system assembles exam papers on the fly from the `globally_open` question pool in Postgres. It replaces the manual process of authoring a `tests` row and hand-tagging 75 questions via `used_in[]`.

Currently implemented for **JEE Main (JEEM)** only. The architecture is designed so that adding NEET, SET, or JEE Advanced requires only a new blueprint file — the selector, persistence, resolver, and output layer are exam-agnostic.

---

## Architecture

### Core principle: strict table separation

Generated tests live **only** in `dynamic_tests` (Supabase). The `tests` table holds only hand-authored curated tests. No stub rows are written to `tests` when a test is generated. The two table sets are disjoint by construction.

All routing decisions go through a **unified resolver** (`test_resolver.py`). No service inspects storage type directly — callers receive a `TestResolution` and act on `meta.type`.

### ID convention

Generated test IDs are prefixed with `gen_` (e.g. `gen_019600ab-…`). Curated IDs remain unprefixed. The resolver fast-paths generated lookups with a string prefix check — no DB probe needed to determine which table to query.

---

## Request Flow

```
Client                  Backend                          Databases
  │                        │                                  │
  │  POST /tests/generate  │                                  │
  │───────────────────────>│                                  │
  │                        │── generate() ───────────────────>│ Postgres (read questions)
  │                        │<── GeneratedManifest ────────────│
  │                        │── sb_insert(dynamic_tests) ─────>│ Supabase
  │<─── { test_id: "gen_…" }│                                  │
  │                        │                                  │
  │  GET /questions/test/{gen_id}?output_type=JEEM             │
  │───────────────────────>│                                  │
  │                        │── resolve_test(gen_id) ─────────>│ Supabase (dynamic_tests)
  │                        │   [cache hit on subsequent calls — no round-trip]
  │                        │── fetch_jeem_from_resolution()   │
  │                        │── SELECT WHERE id IN (...) ──────│ Postgres (question rows)
  │<─── JEEMTestOut ───────│                                  │
  │                        │                                  │
  │  POST /tests/{gen_id}/start                               │
  │  POST /tests/{student_test_id}/save      (unchanged)      │
  │  POST /tests/{student_test_id}/submit                     │
  │───────────────────────>│── student_tests insert/update ──>│ Supabase
  │                        │                                  │
  │  GET /tests/result/{submission_id}                        │
  │───────────────────────>│── resolve_test(test_id) ────────>│ Supabase (dynamic_tests or tests)
  │<─── TestResultOut ─────│                                  │
```

A generated test is **indistinguishable from a curated test** to every downstream consumer — the client, score_service, and session endpoints all receive the same shapes they always have.

---

## Database Layout

### Postgres (questions DB)

No new tables. The generator reads from the existing `questions` table using:

```sql
WHERE globally_open IS TRUE
  AND verification_status = 'verified'
  AND subject = :subject
  AND source_info->>'section_type' IN (:div_aliases)
  AND chapter IS NOT NULL
```

`globally_open = true` is the only gate into the generator pool.

### Supabase

Only **one** table is written to per generate call.

**`dynamic_tests`** — create this table in Supabase:

```sql
create table dynamic_tests (
  id                text        primary key,    -- "gen_<uuid7>"
  exam              text        not null,
  blueprint_version text        not null,
  title             text        not null,       -- denormalised from blueprint at write time
  duration          int         not null,       -- seconds, denormalised from blueprint
  total_marks       int         not null,       -- denormalised from blueprint
  seed              bigint,
  created_by        text,
  created_at        timestamptz default now(),
  manifest          jsonb       not null
);

create index ix_dynamic_tests_created_by on dynamic_tests(created_by);
```

`title`, `duration`, and `total_marks` are stored at write time so the read path never recomputes them from the blueprint.

The `manifest` column is the authoritative record of which question UUIDs belong to which section, in render order:

```json
{
  "subjects": [
    {
      "subject": "physics",
      "sections": [
        { "div": "div1", "section_name": "Physics - Section A", "question_ids": ["uuid-1", "…"] },
        { "div": "div2", "section_name": "Physics - Section B", "question_ids": ["uuid-21", "…"] }
      ]
    },
    { "subject": "chemistry", "sections": [...] },
    { "subject": "mathematics", "sections": [...] }
  ]
}
```

**`tests`** — untouched. No stub rows are written for generated tests.

---

## API Reference

### `POST /api/v1/tests/generate`

Generates a new test paper and returns its id.

**Auth:** Bearer JWT required.

**Request body:**
```json
{ "exam": "JEEM" }
```

**Response `200`:**
```json
{
  "test_id": "gen_019600ab-...",
  "exam": "JEEM"
}
```

**Error `400`:** unsupported exam value.

---

### `GET /api/v1/questions/test/{test_id}?output_type=JEEM`

Transparently handles both curated and generated tests via the resolver.

For a `gen_` prefixed `test_id`, the backend resolves from `dynamic_tests` (cache hit after first call), fetches question rows from Postgres by UUID list, and returns a `JEEMTestOut` — structurally identical to a curated test response.

**Optional params:** `title`, `duration`, `include_solutions` — same as before.

**Round-trips:** 1 Supabase lookup (or 0 on cache hit) + 1 Postgres query. Never double-fetches.

---

### `GET /api/v1/tests/{test_id}/detail`

Returns the full row from whichever table holds the test — `dynamic_tests` for `gen_` prefixed IDs, `tests` for curated. Uses the resolver; no table-specific logic in the router.

---

### Session lifecycle (unchanged)

After receiving `test_id` from `/generate`, the client uses the exact same flow as for any curated test:

```
POST /api/v1/tests/{test_id}/start          → { id: student_test_id, ... }
POST /api/v1/tests/{student_test_id}/save   → 204
POST /api/v1/tests/{student_test_id}/submit → { submission_id }
GET  /api/v1/tests/result/{submission_id}   → TestResultOut
```

`/start`, `/save`, and `/submit` never read from `tests` or `dynamic_tests` — they operate only on `student_tests`. `get_result` uses `try_resolve_test` to populate `exam` and `type` on the `TestResultOut`.

---

## Code Map

```
app/
├── core/
│   └── blueprints/
│       ├── __init__.py          ← ExamBlueprint, SubjectBlueprint, DivQuota dataclasses
│       └── jeem.py              ← JEEM_BLUEPRINT_V1 constants + BLUEPRINTS registry
│
├── services/
│   ├── test_generator.py        ← pure selection logic (no writes, no Supabase)
│   ├── test_resolver.py         ← unified resolver: resolve_test / try_resolve_test + LRU cache
│   └── generated_test_service.py← creation (dynamic_tests insert) + JEEMTestOut adapter
│
├── api/v1/
│   ├── tests.py                 ← POST /tests/generate; GET /{test_id}/detail (resolver-driven)
│   └── questions.py             ← JEEM branch: resolve_test → route by meta.type
│
└── tools/
    └── audit_blueprint.py       ← CLI: python -m app.tools.audit_blueprint jeem
```

---

## Unified Resolver (test_resolver.py)

The resolver is the **single source of truth** for routing between tables. No other module contains table-routing logic.

```python
@dataclass(frozen=True)
class TestMeta:
    id: str
    exam: str
    type: Literal["curated", "generated"]
    title: str
    duration: int
    total_marks: int

@dataclass(frozen=True)
class TestResolution:
    meta: TestMeta
    raw: dict           # the full dynamic_tests or tests row (includes manifest)

async def resolve_test(test_id: str) -> TestResolution:
    """Raises TestNotFound if missing. One Supabase round-trip per cache miss."""

async def try_resolve_test(test_id: str) -> Optional[TestResolution]:
    """Returns None instead of raising. Used by callers that want graceful fallback."""
```

**Routing logic:**
- `gen_*` → `dynamic_tests` (with LRU cache; manifests are immutable, no TTL)
- anything else → `tests`

**Caching:** an in-process `OrderedDict`-based LRU (maxsize=2048, thread-safe) caches `dynamic_tests` rows by `test_id`. After the first lookup, repeated calls for the same generated test hit zero Supabase round-trips.

Curated `tests` rows are not cached (they can be edited in Supabase).

---

## Blueprint System

A blueprint is a frozen Python dataclass in `app/core/blueprints/`. It is the single source of truth for exam paper structure and metadata.

```python
@dataclass(frozen=True)
class DivQuota:
    div: str             # "div1" | "div2" | ...
    count: int           # total questions for this slot, per subject
    min_per_chapter: int # each selected chapter contributes at least this many
    min_chapters: int    # minimum distinct chapters that must contribute

@dataclass(frozen=True)
class SubjectBlueprint:
    subject: str                        # "physics" | "chemistry" | "mathematics"
    quotas: tuple[DivQuota, ...]
    chapter_weights: dict[str, float]   # chapter_code -> weight; default 1.0

@dataclass(frozen=True)
class ExamBlueprint:
    exam: str
    version: str                     # bump on any change to trigger re-audit
    duration_seconds: int
    title: str
    total_marks: int                 # stored in dynamic_tests at creation time
    subjects: tuple[SubjectBlueprint, ...]
    min_pool_per_chapter_div: int    # audit threshold per (chapter, div) cell
```

**JEEM v1 values:**

| subject | div | count | min_chapters | min_per_chapter |
|---|---|---|---|---|
| physics | div1 (MCQ) | 20 | 8 | 1 |
| physics | div2 (Integer) | 5 | 4 | 1 |
| chemistry | div1 | 20 | 8 | 1 |
| chemistry | div2 | 5 | 4 | 1 |
| mathematics | div1 | 20 | 8 | 1 |
| mathematics | div2 | 5 | 4 | 1 |

Total: **75 questions**, **300 marks** (`total_marks=300`), **3 hours**.  
Audit threshold: **≥5** `globally_open` + `verified` questions per (chapter, div).

**To add chapter weights:**

```python
SubjectBlueprint(
    subject="physics",
    quotas=(...),
    chapter_weights={"ROT": 2.0, "ECF": 1.5, "WEP": 1.8},
)
```

Any chapter not listed defaults to weight `1.0`. Weights are relative — only their ratios matter.

---

## Selection Algorithm (test_generator.py)

The generator runs entirely in memory using a seeded `random.Random` — no ordering in SQL.

### Step 1 — Pool fetch

For each `(subject, div_quota)` pair, one SQL query fetches eligible question IDs by chapter:

```sql
SELECT chapter, id
FROM questions
WHERE subject = :subject
  AND globally_open IS TRUE
  AND verification_status = 'verified'
  AND source_info->>'section_type' IN (:div_type_aliases)
  AND chapter IS NOT NULL
```

`_DIV_TO_RAW` in `test_generator.py` maps each canonical div key to all known raw aliases so no eligible question is missed.

### Step 2 — Quota distribution (largest-remainder)

1. **Floor allocation** — every eligible chapter gets at least `min_per_chapter` questions (capped by pool size).
2. **Proportional remainder** — remaining slots distributed proportionally to `chapter_weights` using the largest-remainder method.
3. **Cap enforcement** — no chapter receives more questions than its pool.
4. **Soft relaxation** — if `eligible_chapters < min_chapters`, log a `WARNING` and proceed. Never raise.

### Step 3 — Random sampling

Within each chapter slot, questions are shuffled with the seeded RNG and the first N are taken. Same seed → identical paper (stored in `dynamic_tests.seed` for debuggability).

### Step 4 — Integrity assertions

Before returning, the generator asserts:
- No duplicate question IDs across the entire manifest.
- No section exceeds its quota count.

These raise `RuntimeError` — they indicate generator bugs, not pool shortfalls.

### Step 5 — Section ordering (JEEM)

```
Physics - Section A (div1)  →  Physics - Section B (div2)
Chemistry - Section A       →  Chemistry - Section B
Mathematics - Section A     →  Mathematics - Section B
```

Matches `_JEEM_SECTIONS` in `question_service.py` exactly.

---

## Creation Flow (generated_test_service.py)

```
create_generated_test(db, exam="JEEM", user_id)
    │
    ├── BLUEPRINTS["JEEM"]              ← look up blueprint constants
    │
    ├── test_generator.generate(db, blueprint)
    │       ├── _fetch_chapter_pool() × 6   (6 SQL queries, one per subject×div)
    │       ├── _pick_questions() × 6       (largest-remainder + seeded sample)
    │       └── integrity assertions        (duplicate check, overcount check)
    │
    ├── test_id = f"gen_{uuid7()}"
    │
    └── sb_insert("dynamic_tests", {
            id, exam, blueprint_version,
            title, duration, total_marks,   ← from blueprint (no recomputation at read time)
            seed, created_by, manifest
        })
```

The Postgres session is used **read-only**. No `db.commit()`. No write to `tests`.

---

## Fetch Flow (questions.py + generated_test_service.py)

```
GET /questions/test/{gen_id}?output_type=JEEM
    │
    ├── test_resolver.resolve_test(gen_id)
    │       ├── id starts with "gen_" → query dynamic_tests
    │       ├── [LRU cache hit] → return cached TestResolution (0 Supabase calls)
    │       └── [cache miss]    → sb_select("dynamic_tests") → cache → return TestResolution
    │
    ├── resolution.meta.type == "generated"
    │
    └── fetch_jeem_from_resolution(db, resolution)
            ├── manifest = resolution.raw["manifest"]  ← already in memory, no Supabase call
            ├── unpack → ordered_ids (75 UUIDs in section order)
            ├── SELECT * FROM questions WHERE id IN (ordered_ids)   ← 1 SQL query
            └── _orm_to_jeem_question_out() × 75  ← reuses existing shaper
                → JEEMTestOut (identical shape to curated tests)
```

**Total round-trips on cache miss:** 1 Supabase + 1 Postgres.  
**Total round-trips on cache hit:** 0 Supabase + 1 Postgres.

---

## Pool Health — Audit CLI

Before enabling the generator for users, run:

```bash
python -m app.tools.audit_blueprint jeem
```

Sample output:

```
=== JEEM blueprint v1 audit ===

── PHYSICS ──
  div1: 14 eligible chapters, need ≥8   (min_pool_per_chapter=5)
  [OK]   chapter count: 14 ≥ 8
  [OK]   all chapters have ≥5 questions
         total pool: 312 questions, need 20

  div2: 6 eligible chapters, need ≥4   (min_pool_per_chapter=5)
  [OK]   chapter count: 6 ≥ 4
  [FAIL] chapters below min_pool (5):
         GRV: 3 questions
         MAM: 2 questions
         total pool: 28 questions, need 5

...

Shortfalls detected — fix the DB pool before enabling for production.
```

Exit code `0` = all clear. Exit code `1` = shortfalls. Exit code `2` = bad argument.

**On failure:** set `globally_open = true` on more verified questions for the flagged `(chapter, div)` cells until every cell has ≥`min_pool_per_chapter_div` questions.

---

## Adding a New Exam

1. **Create a blueprint file** — `app/core/blueprints/neet.py`. Define `NEET_BLUEPRINT_V1` and add it to `BLUEPRINTS`.

2. **Add section ordering** — in `test_generator.generate()`, add an `elif blueprint.exam == "NEET":` branch defining the render order.

3. **Add an output adapter** — in `generated_test_service.py`, add `fetch_neet_from_resolution()` mirroring `fetch_jeem_from_resolution()` but using NEET shaping constants from `question_service.py`.

4. **Wire the router** — in `questions.py`, add a branch under the `NEET` output type using `test_resolver.resolve_test` and routing by `meta.type`. Add `"NEET"` to the supported set in `tests.py`.

5. **Audit** — run `python -m app.tools.audit_blueprint neet` before releasing.

No changes to Supabase schema, `dynamic_tests`, or the session endpoints.

---

## Tuning Parameters

All tuning is done in the blueprint file — no code changes elsewhere.

| parameter | location | effect |
|---|---|---|
| `count` per `DivQuota` | `jeem.py` | questions per subject per division |
| `min_chapters` | `jeem.py` | minimum distinct chapters; generator warns if pool is short |
| `min_per_chapter` | `jeem.py` | floor allocation per chapter before proportional distribution |
| `chapter_weights` | `jeem.py` | relative weight per chapter; higher = more questions from that chapter |
| `min_pool_per_chapter_div` | `jeem.py` | audit failure threshold per (chapter, div) cell |
| `total_marks` | `jeem.py` | stored in `dynamic_tests` at creation; used by `TestMeta` |

After any change, bump `version` in the blueprint and re-run the audit CLI.

---

## Key Invariants

- **`globally_open = true`** is the only gate into the generator pool. Setting this flag on a question makes it immediately eligible for all generated tests of the matching exam type.
- **`verification_status = 'verified'`** is always enforced — unverified questions are never served.
- Generated tests use `used_in[]` nowhere — question tagging is replaced entirely by the manifest.
- The `manifest` JSONB in Supabase is immutable after creation. Fetching the same `test_id` twice always returns the same questions in the same order.
- **No routing logic outside `test_resolver.py`.** All callers receive a `TestResolution` and act on `meta.type`.
- **No double round-trips.** The resolver returns the manifest in the same call that determines type; `fetch_jeem_from_resolution` takes data already in memory.
- The generator never raises due to pool shortfalls — it fills what it can and logs warnings. Hard failures (`RuntimeError`) indicate generator bugs (duplicate IDs, overcount).
