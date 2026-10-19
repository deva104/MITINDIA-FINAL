# ClaimPilot AI

Automated motor-insurance claim assessment, built for an Indian hackathon.
Final deadline: **Monday 19 October 2026, night (IST)**.

- Repo: `D:\Hack PROJ\MIT INDIA FINAL HACK\claimpilot`, branch `main`
- Deployed: https://mitindia-final.onrender.com (Render free tier, single worker)

A customer sends vehicle damage photos and documents (claim form, RC, driving
licence) over the WhatsApp Cloud API, or uploads them in the web console. The
system assesses the claim. Assessors review and override it in the insurer
console, and every override is an immutable `audit_log` row.

## Core principle

Apply this to every judgment:

**The system never trusts a single source, never presents a low-confidence
finding as fact, and always says what it could not determine.**

In practice: cross-check documents against each other and against the photos.
Route low-confidence findings to "undetermined" or `needs_review`, never to a
confident answer. Tell the customer or assessor what was dropped, expired,
excluded or unavailable instead of failing silently.

## Architecture

Pipeline (`main.analyse_claim`, shared by `/api/analyze` and WhatsApp):

1. **Stage 0: classification.** `gemini.classify_image` sorts every image
   into vehicle photo, RC, DL, claim form, policy or other.
   `rules.route_classification` routes it: an uncertain or unscored image is
   kept as a vehicle photo.
2. **Stage 0.5: duplicate gate, vehicle photos only.** Perceptual hash
   (`forensics.check_duplicates`) against every earlier `files.phash`,
   excluding this claim by `claim_id`. Documents are never gated, excluded or
   grouped. A byte-identical document from an earlier claim is only noted as
   `document_reuse` info.
3. **Stage 1: vision.**
   - `extract_fields` runs one call per document, with that document's own
     pages and its own field subset. It returns a status: ok, partial,
     failed or not_attempted.
   - `assess_damage` grades the photos, grouped per vehicle.
   - Confidence is parsed by `gemini.parse_confidence` and is **never
     defaulted**. When the model omits it, the finding is kept as
     unconfirmed and listed under undetermined.
4. **Stage 2: forensics.**
   - ELA is **informational only**. Calibration found no threshold that
     separates genuine from edited photos; see `forensics.py`.
   - EXIF verdicts, including "unverifiable".
   - The Sightengine AI-generation check, a signal only, cached.
5. **Stage 3: rules.**
   - `rules.run_rules` runs the cross-document checks.
   - `build_estimate` prices one line per part, for confirmed findings and
     for the indicative range.
   - `rules.establish_documents` is the **single source of truth** for
     documents. The WhatsApp checklist, the Documents card, the
     undetermined list and the decision reason all read its result.
6. **Confidence decision.** `rules.assess_confidence` returns
   `auto_assess` / `needs_review` / `reject`. An escalated claim (AI
   unavailable) is never `auto_assess`.
7. **Output.** A WhatsApp reply and the insurer console (`static/`). Every
   empty state in the console says whether nothing was found or nothing
   was checked.

Modules:

| File | Role |
|---|---|
| `main.py` | FastAPI app, endpoints, `analyse_claim`, WhatsApp webhook and media intake, overrides and notifications |
| `whatsapp.py` | Cloud API client, session state (`wa_sessions`), 24h window (`wa_last_inbound`), reply formatting, checklist, `process_session` |
| `db.py` | Dual backend: SQLite when `DATABASE_URL` is unset, Neon Postgres when set. **Chosen once at import.** Tables: claims, files, vision_cache, audit_log, processed_messages, media, wa_sessions, wa_last_inbound |
| `vision.py` / `providers.py` | Provider chain groq → gemini → openrouter (`VISION_PROVIDERS`, models from `*_VISION_MODEL`), temperature 0, content-hash cache in `vision_cache` |
| `gemini.py` | Prompts and normalisation for classify / extract / assess |
| `forensics.py` | phash duplicate gate, ELA, EXIF, Sightengine, processed previews stored as media |
| `rules.py` | Cross-document rules, estimate, confidence layer |
| `static/` | Insurer console |

Storage:

- Media bytes live in the `media` table, keyed by sha256 (64 hex chars), and
  are served at `/media/{id}`. The `MAX_MEDIA_BYTES` limit is 2 MB.
- WhatsApp sessions are split in two:
  - `wa_sessions` holds only state: pending_choice, awaiting_docs, the
    analysing flag and cutoff, and timestamps.
  - Media arrival is **append-only** in `wa_session_media`, with
    `UNIQUE (wa_id, sha256, page)` and `INSERT ... ON CONFLICT DO NOTHING`.
    Never store session media by read-modify-write of a session copy.
    Never hold a loaded session across an `await` and then write it back.
  - Session media is media ids, never file paths.
- Analysis snapshots the row ids it consumes. Rows that arrive during the
  analysis have higher ids and become the next claim.
- Photos over 2 MB are re-encoded to a JPEG of at most 2048px at quality 90,
  keeping EXIF. A lower quality is used only if 90 is still over the cap.
  Photos are never rejected for size.
- WhatsApp PDFs are stored as rendered pages (`kind = document_page`: the same
  1568px JPEGs `extract_fields` sends, at most 8 pages), never as PDF bytes.
  The original's sha256 and byte size go on the claim's `files` row
  (`source_sha256`, `source_bytes`).
- The duplicate gate excludes rows by `claim_id`, never by path.
- Sessions idle for 30 minutes expire. The customer is told the next time
  they write, and the session's media and orphaned blobs are deleted.
- Render's free-tier disk is wiped on spin-down. Anything that must persist
  goes in the DB. `uploads/` holds only per-request temp files.
- Neon suspends compute after 5 minutes idle. The pool checks each
  connection on checkout, recycles idle ones after 120s, and retries once
  when a connection dies before its first statement.

Runtime:

- Blocking work never runs on the event loop: the analysis pipeline, intake
  classification, image and PDF processing, blob writes and the webhook
  dedup run in threads (`asyncio.to_thread`).
- `ANALYSIS_CONCURRENCY` (default 2) caps parallel analyses.
- Vision calls time out after `VISION_TIMEOUT_SECONDS` (default 45), with no
  SDK-level retries.
- Run exactly **one** uvicorn worker. `ANALYSING_IDS` is an in-process lock.
  `render.yaml` pins this, plus Python 3.10.11 and the Oregon region.
- Admin endpoints (`/api/demo/reset`, `/api/claims/{id}/override`,
  `/api/whatsapp/subscribe`, `/api/whatsapp/status`) require the
  `X-Admin-Token` header to equal `ADMIN_TOKEN`. When `ADMIN_TOKEN` is unset,
  every request to them gets a 401. The console asks for the token once and
  keeps it in localStorage.

## Known limitations

- The Sightengine AI-generation check sees the re-encoded photo, not the
  customer's original bytes, for any photo over 2 MB.
- Original PDF files are not retained. Only the rendered pages and the
  original's sha256 and size are kept.

## Tests

Every test module imports `tests/bootstrap.py` first. It fixes the backend
before `db` is imported:

- Without `CLAIMPILOT_USE_TEST_DB=1`, it forces `DATABASE_URL=""`, which
  selects SQLite even when `.env` holds the production URL.
- With `CLAIMPILOT_USE_TEST_DB=1`, it points `DATABASE_URL` at
  `TEST_DATABASE_URL`. It aborts if that value is empty or has the same host
  as a production `DATABASE_URL`.

New test files must keep that import first.

SQLite suite (fast, around 10 s):

```powershell
Remove-Item Env:DATABASE_URL, Env:CLAIMPILOT_USE_TEST_DB -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m unittest discover -q tests
```

Neon test-branch suite (uses `TEST_DATABASE_URL` from `.env`). From India it
takes about 25 minutes, because every round trip to Oregon costs roughly
250 ms. Raise the pool checkout timeout for that run, and make sure nothing
else is using the test branch: every test's setUp deletes all rows.

```powershell
$env:CLAIMPILOT_USE_TEST_DB = "1"; $env:DB_POOL_TIMEOUT = "30"
.\.venv\Scripts\python.exe -m unittest discover -q tests
Remove-Item Env:CLAIMPILOT_USE_TEST_DB, Env:DB_POOL_TIMEOUT
```

## Working rules

- Never commit or push unless explicitly asked.
- Never run tests or write data against the production `DATABASE_URL`. Use
  `TEST_DATABASE_URL` via `CLAIMPILOT_USE_TEST_DB=1`.
- Never print secrets: no `.env` values, connection strings, tokens or API
  keys, in output, logs or commits.
- Every claim of "passed" must include the output that proves it.
- Report results as a table: `check | expected | actual | PASS/FAIL`.
