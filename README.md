# InsureIT

InsureIT assesses Indian motor-insurance claims sent over WhatsApp or uploaded
in a web console: it reads the damage photos and the claim documents (claim
form, RC, driving licence), cross-checks them, and prices the damage. Every
result goes to an assessor console, where a person can review and override
any finding before a decision is final.

**Live:** https://mitindia-final.onrender.com. The console asks for the admin
token; `/api/health` is public and shows the deployed commit and provider
state. The free hosting tier sleeps after 15 minutes idle, so the first
request after a pause can take about a minute.

## Design principles

- **Never trust a single source.** The claim form is checked against the RC
  and the licence, and the photos against the RC. A finding the model is
  unsure about is not counted in the headline estimate.
- **Say what could not be determined.** Every claim carries an
  "undetermined" list: missing or unreadable documents, illegible plates,
  failed checks, provider failures. A confidence the model did not report is
  never filled in with a default. An empty section says whether nothing was
  found or nothing was checked.
- **A human can override everything, and it is audited.** An assessor can
  confirm or reject any damage finding, change its severity, and approve,
  reject or ask for more information on the claim. Each override is a new
  row in `audit_log` with the actor, the reason and the before and after
  values. Rows are never edited; only the admin demo reset, which wipes all
  claims, clears them.

## Pipeline

One pipeline (`main.analyse_claim`) serves the console upload and WhatsApp.

1. **Classification.** Each image is sorted into vehicle photo, RC, driving
   licence, claim form, policy or other. An image the model is not confident
   about is kept as a vehicle photo, so damage is never dropped.
2. **Duplicate gate.** A perceptual hash of each vehicle photo is compared
   with every photo from earlier claims. A reused photo is excluded. A claim
   whose photos are all reused is rejected. Documents are never gated: the
   same RC appearing in two claims is normal and is only noted.
3. **Vision.**
   - Fields are extracted one document at a time. A document whose type is
     known is asked only for the fields that type carries.
   - A document longer than 3 pages is read in chunks of 3 pages (Groq's
     image limit). Each field keeps the highest-confidence value and names
     its pages, for example "Claim_Form.pdf p.1-3". If one chunk fails, the
     fields from the other chunks are kept, and the customer is asked for
     the missing pages.
   - One call grades the damage in all photos and groups the photos by
     vehicle.
4. **Forensics.**
   - Error-level analysis, shown for information only.
   - EXIF capture time and location, or "unverifiable" when the photo was
     re-encoded in transit, as WhatsApp does.
   - An AI-generation score from Sightengine, used as a review signal only.
5. **Rules.**
   - Cross-document checks:
     - insured on the claim form vs RC owner
     - driver on the claim form vs licence holder
     - claim-form registration vs RC
     - number plate in the photos vs RC, on the claimed vehicle only
     - licence expiry vs accident date
     - policy period vs accident date
     - accident description vs damaged area
   - Names are matched the way Indian documents write them: any word order,
     initials, S/O W/O D/O C/O dropped, MD/MOHD and PVT/LTD treated as the
     same word.
   - The driver need not be the owner: owner vs licence holder alone is a
     note, never a flag.
   - The estimate prices each damaged part once, from a rate card. An
     estimate above Rs 50,000 is sent for a survey, following the IRDAI
     Master Circular on Protection of Policyholders' Interests, 2024.
6. **Decision.**
   - `auto_assess`, `needs_review` or `reject`, with the reasons.
   - Any HIGH flag, a low or unreported confidence, a missing or unread
     document, an unpriced part or a survey means `needs_review`.
   - A claim the AI could not assess is escalated to a person, never
     auto-assessed.
7. **Output.**
   - A WhatsApp reply. It shows only what the customer can act on, for
     example a request for a clearer photo of page 4.
   - The full record in the console: the decision, every check, and the
     pipeline trace with the provider that served each stage.

## Vision providers and the key pool

- **The chain.** Calls go to Groq, then Gemini, then OpenRouter
  (`VISION_PROVIDERS`). A provider is skipped when a call has more images
  than it accepts. Results are cached by content hash, so a repeated image
  costs nothing.
- **Time limits.** Every single provider call is capped at 20 seconds, and
  no provider may spend more than 25 seconds on one call in total. A timeout
  moves to the next provider at once. A Gemini 5xx gets one retry after
  2 seconds; any other 5xx moves on.
- **The key pool.** Each provider can have several keys: `GROQ_API_KEY`
  plus any `GROQ_API_KEY_<suffix>`, and the same for Gemini and OpenRouter.
  - A 429 per-minute limit cools that key for the provider's retry time,
    and the next key is used immediately.
  - A daily limit exhausts the key until its reset.
  - A rejected key (401/403) is marked invalid until restart.
  - Only when every key is limited does the call move to the next provider.
  - Logs and `/api/health` show key labels and states, never key values.

## Running locally

Python 3.10.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file with at least `ADMIN_TOKEN` and one provider key (see
the table below). Without `DATABASE_URL` the app uses a local SQLite file
(`claimpilot.db`). Then:

```bash
uvicorn main:app --port 8000 --workers 1
```

Open http://localhost:8000, enter the admin token in the claim feed panel,
add documents and photos, and press Analyse claim. WhatsApp needs a public
HTTPS URL: point the Meta app's webhook at `/webhook`, with
`WHATSAPP_VERIFY_TOKEN` as its verify token.

Run exactly one worker: the lock that stops one WhatsApp session being
analysed twice lives in process memory.

`seed_demo.py` resets a database and loads demo claims through the real
pipeline. It wipes the target, so it requires `--yes` and a valid admin
token; see its docstring.

## Environment variables

| Name | What it does |
|---|---|
| `DATABASE_URL` | Postgres connection string (Neon in production). Unset: local SQLite. |
| `CONSOLE_USERS` | Console sign-in: a JSON list of `{username, display_name, city, password_hash}`. Make an entry with `python scripts/hash_password.py --json <user> "<name>" <city>`. Without it nobody can sign in. |
| `SESSION_SECRET` | Signs console session cookies. Unset: derived from `ADMIN_TOKEN`. |
| `ADMIN_TOKEN` | For scripts (seed_demo): accepted by every claim, upload, override, reset and admin endpoint in place of a session. Also the key for signed media links. |
| `ELA_ENABLED` | `true` turns error-level analysis back on. Default `false`. |
| `GROQ_API_KEY`, `GROQ_API_KEY_<suffix>` | Groq keys; the bare name is used first, then suffixes alphabetically. |
| `GEMINI_API_KEY`, `GEMINI_API_KEY_<suffix>` | Gemini keys, pooled the same way. |
| `OPENROUTER_API_KEY`, `OPENROUTER_API_KEY_<suffix>` | OpenRouter keys, pooled the same way. |
| `VISION_PROVIDERS` | Provider order. Default `groq,gemini,openrouter`. |
| `GROQ_VISION_MODEL`, `GEMINI_VISION_MODEL`, `OPENROUTER_VISION_MODEL` | Model per provider; each has a default. |
| `VISION_CALL_TIMEOUT` | Cap on one provider call, in seconds. Default 20. |
| `VISION_PROVIDER_BUDGET` | Total time one provider may spend on one call, in seconds. Default 25. |
| `VISION_TIMEOUT_SECONDS` | SDK-level timeout, in seconds. Default 20. |
| `CACHE_ENABLED` | Cache vision results by content hash. Default `true`. |
| `WHATSAPP_TOKEN` | WhatsApp Cloud API access token. |
| `WHATSAPP_PHONE_NUMBER_ID` | The business phone number that sends replies. |
| `WHATSAPP_WABA_ID` | The WhatsApp Business Account, for subscribing the app. |
| `WHATSAPP_VERIFY_TOKEN` | Answers Meta's webhook verification request. |
| `WHATSAPP_APP_SECRET` | Meta app secret. When set, webhook calls without a valid `X-Hub-Signature-256` are refused. |
| `GRAPH_API_VERSION` | Meta Graph API version for WhatsApp calls. |
| `SIGHTENGINE_API_USER`, `SIGHTENGINE_API_SECRET` | Sightengine credentials for the AI-generation check. |
| `AI_DETECTION_ENABLED` | Run the AI-generation check. Default `true`. |
| `REQUIRED_DOCS` | Documents a claim must include. Default `rc,driving_licence,claim_form`. |
| `STRICT_DOCS` | When `true`, a WhatsApp customer cannot skip missing documents. Default `false`. |
| `ANALYSIS_CONCURRENCY` | Analyses that may run at once. Default 2. |
| `DB_POOL_MAX_SIZE` | Postgres connection pool size. Default 20. |
| `DB_POOL_TIMEOUT` | Seconds to wait for a pooled connection. Default 10. |
| `TEST_DATABASE_URL` | Tests only: the Neon test branch. |
| `CLAIMPILOT_USE_TEST_DB` | Tests only: `1` runs the suite against `TEST_DATABASE_URL`. |
| `RENDER_GIT_COMMIT` | Set by Render; reported as `commit` in `/api/health`. |

## Tests

The tests never call a real provider; every provider is stubbed. Every test
module imports `tests/bootstrap.py` first, which decides the database before
anything else loads.

SQLite (default, about a minute):

```bash
python -m unittest discover -q tests
```

The Neon test branch (`TEST_DATABASE_URL` in `.env`). The bootstrap refuses to
run if that URL is empty or points at the production host. From India the
full run takes about 40 minutes, because each round trip goes to Oregon.

```bash
CLAIMPILOT_USE_TEST_DB=1 DB_POOL_TIMEOUT=30 python -m unittest discover -q tests
```

On Windows PowerShell: `$env:CLAIMPILOT_USE_TEST_DB = "1"; $env:DB_POOL_TIMEOUT = "30"`,
then the same `python -m unittest` command.

`tests/test_whatsapp_flow.py` replays a real WhatsApp conversation end to end,
with a provider timeout and a provider error, and checks every reply.

## Known limitations

- **A re-photograph of an earlier claim's photo is not caught by the
  duplicate gate.** The gate compares perceptual hashes, and a screen photo
  of a previously claimed image changes the hash beyond the threshold; a
  test pins this. On WhatsApp an image the classifier recognises as a
  screenshot or a photo of a screen is refused at intake, but that depends
  on the vision model recognising it.
- **No reverse image search.** A photo downloaded from the internet passes the
  duplicate gate unless it was submitted to InsureIT before. In calibration
  the AI-generation check also scored an image downloaded from Google as
  clean.
- **Sightengine accuracy varies by generator and by camera processing.** In
  calibration a genuine phone photo with heavy AI enhancement scored 0.99,
  the same as a generated image, while a screenshot scored clean. The score
  is a review signal that never rejects a claim; an assessor decides.
- **Error-level analysis is informational only.** Calibration found no
  threshold that separates edited photos from genuine ones once WhatsApp has
  recompressed them.
- **The plate check is not rerun after vehicle selection.** When photos show
  two vehicles, the checks run at analysis time for the first claimable one.
  If the customer then picks the other vehicle on WhatsApp, its plate is not
  rechecked against the RC.
- **Gemini's free quota is per Google Cloud project.** Several keys add
  capacity only if each comes from a different project; keys from one
  project share its daily limit. Groq likewise counts limits per
  organisation, not per key.
- **Other limits:**
  - Key cool-downs are held in memory, so a limited key is tried once more
    after a restart.
  - The AI check sees the re-encoded copy of a photo over 2 MB.
  - Original PDFs are not kept, only their rendered pages, hash and size.
  - WhatsApp PDFs are stored as `document<n>.pdf`, not under the customer's
    filename.
  - Estimates use a rate card seeded from published market rates, not an
    insurer's network rates.
