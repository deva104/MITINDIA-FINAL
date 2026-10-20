# InsureIT

Formerly ClaimPilot. The product name is InsureIT everywhere a person sees
it: console, WhatsApp messages, `/api/health` (`service`), the API title.
These keep the old name on purpose and must not be renamed: the repo and its
folder, env vars (`CLAIMPILOT_USE_TEST_DB`), tables, modules, storage keys
(`claimpilot.db`, localStorage `claimpilot.*`, the `claimpilot-media-url:`
signing salt), the Render service name in `render.yaml`, and the `CLM-`
claim prefix.

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
   - ELA is **off** (`ELA_ENABLED=false`): calibration found no threshold
     that separates genuine from edited photos. The code and its calibration
     tests stay behind the flag; the console shows no ELA card or heatmap.
   - EXIF only for a photo that carried metadata: stripped metadata (normal
     on WhatsApp) gets no entry and no undetermined item.
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
  analysis have higher ids and become the next claim. Media byte-identical
  to rows being analysed (the unique key refuses it) is held in
  `whatsapp.DEFERRED_MEDIA` and added back as soon as the analysis releases
  its rows, so it is the next claim too.
- **During an analysis**, a text (DONE included) gets exactly one "Your claim
  is still being analysed — you will get the result here." In the guided
  flow, new media gets one "Your claim is still being analysed — please send
  this again after you get the result." per analysis, however many items
  arrive, and is not kept (a new claim starts from the menu). Outside it
  (no stage) the older rule holds: "these photos will start a new claim". The start-a-claim
  greeting is never sent to a number with session media or a running
  analysis.
- **Stray events.** Every inbound message's type is logged (with
  `errors=<code>` on `unsupported`), never the number. A 3-photo send in
  production delivered 4 webhook events. An event that is not a photo,
  document, text or a listed unsupported type gets no reply when media from
  that number was sent within 15 s before or after it (`STRAY_EVENT_WINDOW`).
  Times are Meta's send timestamps, so database latency cannot push one
  album's events apart. Reactions and edits are never answered.
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
- The pool holds up to `DB_POOL_MAX_SIZE` (default 20) connections. The worst
  case in production is about 25 at once: an analysis holds one for its whole
  run plus one nested, and each image the console shows is a `/media` read.
  A `PoolTimeout` is retried once; then the console gets a 503 (it polls
  again) and a WhatsApp customer gets "Reply DONE to try again", never an
  error.

Runtime:

- Blocking work never runs on the event loop: the analysis pipeline, intake
  classification, image and PDF processing, blob writes and the webhook
  dedup run in threads (`asyncio.to_thread`).
- `ANALYSIS_CONCURRENCY` (default 2) caps parallel analyses.
- **Provider time limits.** Every single provider call is capped at 20 s by
  wall clock (`VISION_CALL_TIMEOUT`; the SDK timeout `VISION_TIMEOUT_SECONDS`
  is 20 too, with no SDK retries). No provider spends more than 25 s on one
  vision call in total (`VISION_PROVIDER_BUDGET`). A timeout moves to the
  next provider at once, with no retry. A gemini 5xx gets one retry after
  2 s; other providers' 5xx move on at once. Every timeout and retry is
  logged.
- **Chunked documents.** A document of more than 3 pages (Groq's image limit)
  is read in chunks of 3 pages, one call each, cached per chunk
  (`extract_fields(<name> p.1-3)`). Per field, the highest-confidence found
  value wins, with `source_doc` "<name> p.1-3". A failed chunk keeps the other
  chunks' fields, becomes a `failed` entry named "<name> p.4" (with `pages`),
  and the customer is asked for those pages.
- Run exactly **one** uvicorn worker. `ANALYSING_IDS` is an in-process lock.
  `render.yaml` pins this, plus Python 3.10.11 and the Oregon region.
- **Provider key pool** (`providers.discover_keys`, state in `vision.py`).
  - Keys: `GEMINI_API_KEY` (label `main`) first, then every non-empty
    `GEMINI_API_KEY_<suffix>` in alphabetical order, labelled by the suffix
    in lower case. The same for `GROQ_API_KEY*` and `OPENROUTER_API_KEY*`.
  - 429 per minute: that key cools for the retry-after (default 60 s) and
    the next key is tried at once. 429 daily: exhausted until the stated
    reset, else 00:00 UTC. 401/403: invalid until restart, with a WARNING
    naming the label. Limited keys are skipped without a call.
  - 503 / overloaded / 5xx: no key rotation; the chain moves to the next
    provider. Only when every key of a provider is limited does a 429 move
    the chain on.
  - Logs and `/api/health` show labels and states only. **Never log or
    return a key value or any prefix of one.** Error text passes through
    `providers.redact`.
  - The live pipeline never waits for a key. `seed_demo.py` calls
    `vision.wait_for_keys(True)`: when every key of every provider is
    limited it sleeps until the soonest one returns (at most 60 s a step).
- **Access.** Claim data is personal data: names, licence and chassis
  numbers, phone numbers, document photos.
  - These require a signed-in console user (session cookie) or the
    `X-Admin-Token` header equal to `ADMIN_TOKEN` (401 otherwise):
    `/api/claims`, `/api/claims/{id}`, `/api/analyze`, `/api/storage`,
    `/api/demo/reset`, `/api/claims/{id}/override`,
    `/api/claims/{id}/request-info`, `/api/me`, `/api/whatsapp/subscribe`,
    `/api/whatsapp/status`. Scripts (seed_demo) use the token and name the
    actor; a signed-in user is always the actor, whatever the body says.
  - **Console sign-in** (`auth.py`). Users come from `CONSOLE_USERS`, a JSON
    list of `{username, display_name, city, password_hash}`; hashes are
    PBKDF2-SHA256 with a salt per user (`python scripts/hash_password.py`,
    or `--json <user> <name> <city>` for a whole entry; it reads the password
    without echo). The session cookie `claimpilot_session` is signed with
    `SESSION_SECRET` (else derived from `ADMIN_TOKEN`), HttpOnly, Secure,
    SameSite=Lax, 12 hours. A mutating request signed in by cookie must carry
    `X-Requested-With: InsureIT` (403 otherwise). CORS never allows
    credentials. Five failed sign-ins from one address in 10 minutes lock it
    for 10 minutes (in memory; the last `X-Forwarded-For` entry is the
    address). Logout clears the cookie and this process refuses it until it
    expires. `/api/login` and `/api/session` answer 200 with `signed_in`
    (and `locked`, `retry_after`), so the browser logs no error for a wrong
    password. Passwords, hashes, cookies and user names are never logged.
  - `/media/{id}` needs a signature, not the header, because `<img>` cannot
    send one. Claim responses carry `?exp=…&sig=…` (HMAC from `ADMIN_TOKEN`,
    valid 6–7 h, `main.sign_media_urls`); anything else gets a 403. The cache
    header is `private`.
  - Open by design: `/api/health` (configuration and labels only) and
    `/webhook`.
  - The console shows a sign-in screen until `/api/session` says signed
    in, then the profile (display name, city, claims decided) and Sign out.
    It stops polling when signed out or on a 401. There is no token form
    and no free-text assessor name.
- **Webhook signature.** With `WHATSAPP_APP_SECRET` set (Meta app settings,
  App secret), `POST /webhook` must carry a valid `X-Hub-Signature-256`
  (HMAC-SHA256 of the raw body) or it gets a 403. Unset: a WARNING per
  request and the request is accepted. `/api/health` reports
  `webhook_signature: enforced | not_configured`.
- **No phone numbers in logs.** Log a sender with `whatsapp.who()` (last four
  digits), a message with `whatsapp.message_ref()` (a wamid embeds the
  number), and any provider response body through `whatsapp.log_safe()`.
- **Warm-up.** `imagehash.phash` imports `scipy.fftpack` on first use, about
  4 s on Render's CPU. `forensics.warm_up` runs it in a thread at startup
  and logs `[forensics] warm-up: phash ready in …s`.

## WhatsApp conversation (guided flow)

- **START** (no active session): "Welcome to InsureIT", *1* file a claim, *2*
  track a claim. MENU, END, EXIT and HELP work at any point (case and
  punctuation ignored; exit/cancel/quit/stop are EXIT; haan/ha/nahi, y/nope
  are YES/NO). During an analysis a text gets the one busy line, EXIT gets
  "will finish", and media gets one "send this again after the result".
- **Every message that waits for input** ends with the footer "_Reply EXIT to
  cancel · MENU for the main menu_" and is remembered as `last_prompt`
  (`whatsapp.ask`). A greeting, NO after EXIT, or noise restates it
  (`restate`; a session without one falls back to `stage_prompt`). A
  greeting is never a plate or a tracking ID.
- **EXIT confirms first.** Before submission it counts what will be discarded;
  after it says the claim stays submitted. MENU and END with media pending
  ask the same question: nothing the customer sent is discarded without YES.
  Sending media after EXIT means carrying on.
- **File a claim:**
  - The plate is normalised and validated (standard and BH series), then
    looked up in the mock VAHAN registry (`registry.lookup`, where the VAHAN
    API call goes in production). Expired or third-party-only policies end
    the claim path.
  - Steps 1–4: damage photos (DONE; cannot be skipped), RC, licence, claim
    form (SKIP allowed). Every image, and every page of a PDF, is classified
    (`whatsapp.classify_pages`) into its own slot (`wa_session_media.slot`)
    whatever the step, **only** on a positive classification at confidence
    >= 0.6. A screenshot is refused ("please send the original photo"); a
    person, a scene, a blank image or anything below 0.6 gets "That does not
    look like a <expected item>" and fills nothing. One PDF can fill several
    slots ("That PDF filled: …"). A resend says "You already sent that …".
  - Media sent before step 1 (at START, the plate or the vehicle check) is
    kept for the claim and counted at step 1.
  - After step 4 (or SUBMIT early) a **Ready to submit** summary is shown;
    analysis starts only on SUBMIT.
  - The result adds tracking ID, assessor and next step. The claim then
    stays **open** (`stage = open_claim`): documents sent in the session
    attach to it (`main.reassess_claim`: extraction and rules again, damage
    assessment reused, a `documents_added` event).
  - A document already on the claim (by sha256, `claim_shas` in the session)
    is not attached again.
  - **Request info** (`/api/claims/{id}/request-info`, console dialog): one
    item (`whatsapp.REQUEST_ITEMS`: RC, licence, claim form, clearer damage
    photos, number plate photo, other) and a comment. It is recorded as the
    `more_info` decision with `requested` and `comment`, a
    `more_info_requested` event, and sent inside the 24h window; outside it
    the audit says not sent and the request goes out the next time the
    customer writes. The next item the customer sends is classified: the
    requested kind is attached (documents: extraction and rules again;
    photos: kept on the claim as `added_photos`, damage grading not re-run),
    with a `documents_received` event and "Documents received" on the feed
    card; anything else gets "That looks like a <type>, but your assessor
    asked for <requested>. Please send <requested>."
  - A document for a claim in `more_info` asks "Add this to CLM-…?" first.
- **Track:** a tracking ID (any case, spaces allowed) or ALL, only this
  number's claims; a tracking ID at the menu or on an open claim is answered
  at once. A word that is not an ID gets the options again, never "could not
  find"; an ID-like miss says so once, then the options.
- **END** clears the session and repeats the tracking ID. 30 minutes idle:
  the next message says the session timed out, then START.
- **Registry data.** `data/mock_vahan_registry.xlsx` (100 fictitious rows,
  `scripts/generate_registry.py`). Rows from real test documents are never
  committed (the repo is public): `data/private_registry_rows.json`
  (gitignored) or `REGISTRY_PRIVATE_ROWS`.
- **Registry cross-checks** (`rules.registry_flags`): plate vs RC, owner vs
  RC owner (name matcher), the photographed vehicle vs the registration
  (make AND body type differ: HIGH; make only, body only or colour only:
  MEDIUM, "could not be confirmed from photos", since models misread makes),
  policy on the accident date from the registry. A make is shown to anyone
  only at `make_confidence` >= 0.8 (`main.MAKE_CONFIDENCE`); below that the
  label is colour and body type ("White Sedan"). With two vehicles
  in the photos, the one matching the registration is chosen
  (`selected_vehicle_group`); the customer is asked only when none clearly
  matches.
- **Feed.** `/api/claims` adds each claim's `final_decision` and
  `info_request` from its audit trail (cached in `main.FEED_STATE`, dropped on
  every override, request and received document; one worker), so a card
  shows the assessor's decision as soon as the override returns.
- **Console checks.** Every flag and note carries `comparison`: rows of
  `{source, value}` and a verdict (MISMATCH, EXPIRED, OUTSIDE POLICY, …;
  `rules.comparison`, also filled for claims saved earlier). Checks that ran
  and agreed come back as `checks` (MATCH, VALID, COVERED;
  `rules.passed_checks`). Cards are tinted faint red (HIGH, MEDIUM,
  suspicious), green (match, clean) or grey (information).
- **Assessors and timeline.** Six mock assessors (`assessors.py`), assigned
  by registry city, round robin, phone `ASSESSOR_CONTACT`. `claim_events`
  holds the timeline (submitted, assessor_assigned, survey_scheduled,
  documents_added, more_info_requested, approved, rejected,
  assessment_updated).

## Domain and presentation rules

- **Timestamps.** Every stored timestamp is timezone-aware UTC
  (`db.utc_now_iso()`, `+00:00`). Browsers render it in local time. Legacy
  naive rows are read as UTC (`db.as_utc_iso`). Session, inbound and dedup
  times are epoch seconds.
- **Owner vs driver.** In Indian motor insurance the driver need not be the
  owner.
  - The insured on the claim form is matched to the RC owner
    (`insured_owner_mismatch`, HIGH).
  - The claim form's driver is matched to the licence holder
    (`driver_licence_mismatch`, HIGH).
  - RC owner vs licence holder alone is only an INFO note
    (`payload.notes`, `driver_not_owner`), never a flag.
  - No claim form → undetermined "Driver identity".
  - Names are compared by `rules.name_match`. Word order does not matter
    ("S Karthik" = "Karthik S"), an initial stands for a word, S/O W/O D/O
    C/O and what follows are dropped, honorifics are ignored, and MD/MOHD
    and PVT/LTD are aliases. A subset ("Rajesh Kumar" in "Rajesh Kumar
    Singh") is `partial`: an INFO note (`*_partial_match`), never a flag.
    Only a `mismatch` is HIGH. Initials alone never match.
- **Number plates.** A plate is compared with the RC only on the claimed
  vehicle (the first claimable group). Photos of another vehicle, such as
  the other party's, are reported once by `vehicle_mismatch`.
- **Survey.** An estimate whose upper bound exceeds Rs 50,000 needs a survey.
  The reason cites the IRDAI Master Circular on Protection of Policyholders'
  Interests, 2024 (`rules.SURVEY_REASON`); the console shows that text.
- **Approval message.** "approved — estimated repair cost ₹a–₹b. The final
  amount is confirmed against the repair invoice." With no confirmed
  estimate the range is left out.
- **Who sees what.** Each undetermined entry has an `audience`.
  - `customer_actionable` entries carry a `request`: missing or unread
    documents, an illegible plate, an illegible document field. WhatsApp
    shows only these, as "To complete your claim" requests.
  - Everything else is `internal`: forensics, AI check, EXIF, ELA, model
    confidence, providers. It is shown only in the console.
  - The WhatsApp review line never gives the reason.
- **Provider provenance.** A fallback that still produced a result is not
  uncertainty. It appears on the stage in the pipeline trace (`timings[].via`,
  e.g. "via gemini (groq rate-limited)"), not under undetermined. Only a
  failure that left something unknown is undetermined.

## Known behaviour

### Sightengine AI-generation check: live calibration, 19 Oct 2026

How it was measured:
- Four images scored with the live Sightengine `genai` model through the
  pipeline's own `forensics.check_ai_generated`.
- Thresholds unchanged: **suspicious ≥ 0.85, inconclusive 0.50–0.85,
  clean < 0.50**.
- Each image scored twice; the repeat came from the vision cache.

| image | file | score | verdict | first call | repeat |
|---|---|---|---|---|---|
| real photo, taken on own phone | phone.jpeg, 1076×1128, no EXIF | **0.990** | **suspicious** | 2.68s | 0.01s, cache hit, same result |
| image downloaded from Google | google.jpeg, 1600×1200 | 0.001 | clean | 2.31s | 0.01s, cache hit, same result |
| YouTube screenshot | youtube.jpeg, 1548×695 (same file as `test data/Photos/CAR A PIC1.jpeg`) | 0.001 | clean | 2.52s | 0.00s, cache hit, same result |
| AI-generated damaged car | ai.jpeg, 1376×768 | 0.990 | suspicious | 2.91s | 0.00s, cache hit, same result |

What this shows, for Q&A:
- **A genuine phone photo scored as high as a generated image.** The photo
  shows heavy phone-side AI enhancement: smoothed textures and
  over-sharpened edges, typical of AI zoom or super-resolution. The detector
  flags AI *processing*, not only AI *fabrication*.
- **A downloaded image and a screenshot scored clean**, although neither is
  an original capture. The check says nothing about provenance or reuse.
- **So the check is one signal, never a verdict.** A "suspicious" score adds
  a HIGH review flag and "photo authenticity requires verification". It
  never rejects a claim; an assessor decides. Thresholds were deliberately
  not tuned to make these four images pass or fail.
- **Caching:** each repeat came from the content-hash cache in about 0.01s,
  with no second API call and an identical result.

### Groq free tier

Groq's free tier allows about 7,000 input tokens per minute. One image call
is about 2,200 tokens, so roughly three image calls a minute per key. Beyond
that the call returns 429, the key cools, and the next Groq key is tried
(no retry, no wait). When every Groq key is cooling the chain moves to
Gemini. The claim still completes, with the result produced by the fallback.

### Gemini free tier

The free tier allows 20 requests per day per model, counted **per Google
Cloud project**, not per key. Several keys only add capacity when each one
belongs to a different project; keys from the same project share one daily
cap and all hit 429 together.

## Known limitations

- The Sightengine AI-generation check sees the re-encoded photo, not the
  customer's original bytes, for any photo over 2 MB.
- Original PDF files are not retained. Only the rendered pages and the
  original's sha256 and size are kept.
- Key cool-downs live in memory. After a restart (a Render spin-down
  included) a limited key is tried once more, gets its 429 again and is
  marked again. This is deliberate: nothing is persisted.

## Tests

Every test module imports `tests/bootstrap.py` first. It fixes the backend
before `db` is imported:

- Without `CLAIMPILOT_USE_TEST_DB=1`, it forces `DATABASE_URL=""`, which
  selects SQLite even when `.env` holds the production URL.
- With `CLAIMPILOT_USE_TEST_DB=1`, it points `DATABASE_URL` at
  `TEST_DATABASE_URL`. It aborts if that value is empty or has the same host
  as a production `DATABASE_URL`.

New test files must keep that import first.

Tests that touch the provider chain must not see the developer's real keys
or another test's cool-downs. Wrap them in `helpers.provider_keys()` (strips
every real `*_API_KEY*`, sets fakes) and `helpers.fresh_key_state()`. The
suite must report 0 real provider calls.

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
