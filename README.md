# mindDoc — Backend

REST API for **mindDoc**, a mobile journalling application providing structured emotional support through guided journal entries, crisis-aware safeguards, and an AI reflection layer.

Implements **SRS-ESC-001 v0.9**. Progress, decisions and deviations are recorded in the project development log (`my dev log.md`), kept outside the repository.

---

## Stack

- Python 3.13 with FastAPI and Uvicorn
- PostgreSQL 17, hosted on Supabase (`ap-south-1`)
- SQLAlchemy ORM with Alembic migrations
- bcrypt password hashing, JWT (HS256) sessions
- OpenAI Python SDK, pointed at Azure OpenAI (`gpt-4.1-mini`) for every generated message

> The mobile client is React Native and lives in a separate repository.

---

## Getting Started

### Prerequisites

- Python 3.11 or above
- Access to a PostgreSQL 15+ instance

### Setup

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # macOS / Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create a .env file in the project root (see below)

# 4. Apply database migrations
alembic upgrade head

# 5. Run the server
uvicorn app.main:app --reload
```

The API is then available at `http://127.0.0.1:8000`, with interactive documentation at `http://127.0.0.1:8000/docs`.

### Environment Variables

Create a `.env` file in the project root. It is git-ignored and must **never** be committed — nor may any copy of it, such as `.env.backup`; `.gitignore` covers `.env.*` for that reason.

```env
DATABASE_URL=postgresql://user:password@host:5432/database
JWT_SECRET=<generate with: python -c "import secrets; print(secrets.token_urlsafe(32))">
RESEND_API_KEY=re_...
OPENAI_API_KEY=<Azure OpenAI key, or an api.openai.com key>
```

Optional, with defaults shown:

```env
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=30
CONSENT_VERSION=v1
EMAIL_FROM=onboarding@resend.dev
APP_BASE_URL=http://127.0.0.1:8000
ELEVATED_DISTRESS_THRESHOLD=9

# Azure OpenAI: the v1 endpoint of the resource, and the deployment names.
# Leave OPENAI_BASE_URL unset to use api.openai.com instead.
OPENAI_BASE_URL=https://<resource>.openai.azure.com/openai/v1/
OPENAI_CLASSIFIER_MODEL=gpt-4.1-mini
OPENAI_RESPONSE_MODEL=gpt-4.1-mini
OPENAI_TIMEOUT_SECONDS=30
LLM_MOCK=false                 # true → fixed placeholder replies, no API calls
CONVERSATION_CONTAINMENT_TURNS=30
```

**Without an API key the app still works.** Every capture prompt falls back to stored wording (FR-ENT-023) and entries complete normally; only the optional reflection conversation is unavailable.

---

## Project Structure

```
app/
├── base_instructions.md      Who Echo is, and how a conversation must end
├── capture_instructions.md   How a single capture question is worded
├── clinical_context/*.md     Guardrails per focus area — highest priority in a prompt
├── skills/<name>/SKILL.md    Response style per detected topic (Agent Skills format)
├── journal_types/*.md        Structure and pacing per journal
├── content/                  Clinical content sets (JSON) — reviewable without reading code
│   ├── capture_schedules/    Which values each journal records, in order
│   ├── libraries/            Thinking traps, feelings, triggers
│   └── *.json                Crisis responses, grounding, scales, fallback wording
├── core/                     Config, database, security, capture, crisis, language, LLM client
├── models/                   SQLAlchemy models — what the database stores
├── routers/                  API endpoints, one file per feature area
├── schemas/                  Pydantic schemas — what the API accepts and returns
└── main.py                   Application entry point

alembic/versions/             Migration scripts, applied in sequence
evals/check_in_eval.py        Runs the real prompts against the model and checks SRS rules
postman/                      Postman collection with saved example responses
```

> Models and schemas are deliberately separate: `password_hash` exists in the `Account` model but appears in no response schema, so it cannot leak through the API.

> Prompt content lives in Markdown and JSON, not in Python. The Clinical Advisor can review and revise what Echo says without reading code, and a change takes effect without a restart.

---

## Module Status

| # | Module | SRS Section | Backend | Frontend |
|---|--------|:---:|---|---|
| 1 | Authentication (account layer) | 4.2 | ✅ Complete | ✅ Complete |
| 2 | Application lock (device layer) | 4.2 | — Not applicable | ✅ Complete |
| 3 | Onboarding | 4.3 | ✅ Complete | ✅ Complete |
| 4 | Dashboard and engagement | 4.4 | ✅ Streak, entry counts, weekly goal | ✅ Complete |
| 5 | Entry engine | 4.5 | ✅ Complete except voice input | ✅ Complete except voice input |
| 6 | Journal types | 4.6 | ✅ All five journals | ✅ All five journals |
| 7 | Selection libraries | 4.7 | ✅ Complete | ✅ Complete |
| 8 | Crisis detection and response | 4.8 | ✅ Complete (content pending review) | ✅ Complete |
| 9 | AI reflection module | 4.9 | ✅ Complete | ✅ Complete |
| 10 | Insights | 4.10 | ✅ Complete (questionnaire is a generic placeholder) | ✅ Complete |
| 11 | Learning library | 4.11 | ✅ Complete (articles are drafts) | ✅ Complete |
| 12 | User data control | 4.12 | 🟡 Entry deletion only | 🟡 Entry deletion only |

> The application lock is device-resident by design (**FR-AUTH-009**: the lock secret is never transmitted), so it has no backend component. The app stores a salted hash of the PIN (20,000 SHA-256 rounds) in device storage and nothing else; forgetting it means signing in again.

---

## Features Implemented

### Authentication

- Signup is **email-first**: the client submits only an email, the account is created pending (no password), and a 6-digit code is emailed. The code is exchanged for a setup token, which sets the password and returns the first session.
- Passwords stored only as salted bcrypt hashes; the hash appears in no response schema.
- Login failures are indistinguishable between unknown email, wrong password, and pending account.
- Rate limiting: five failures per email in fifteen minutes across login and code verification.
- Refresh token rotation with family revocation; the mobile client refreshes automatically on a 401.
- Code-based password reset, which revokes every session on success.
- New-device notification by fingerprint, revealing nothing about the app in the notification.

### Onboarding

- Consent recorded with document version and timestamp; the current version is served so the client can detect a change.
- Focus areas, work issues, life vision (160 characters), distress baseline (0–10) and weekly goal (2, 3, 5, 7).
- `elevated_distress` computed from `ELEVATED_DISTRESS_THRESHOLD`, so the threshold is configuration rather than a release.
- Status endpoint reporting what remains, so an interrupted sequence can resume.

### Entry engine (SRS 4.5)

- Each journal is built from a **capture schedule** (`content/capture_schedules/*.json`): which values, in what order, with which control. The schedule holds no user-facing wording (FR-ENT-001, CON-009).
- One value per message, answered or explicitly skipped, never requested twice, and persisted the moment it is answered (FR-ENT-003, FR-ENT-024).
- A value may declare `depends_on`: it is only asked when the value it follows up on was actually answered, so "how strong was that?" never arrives after a skipped feelings step.
- The whole entry is one thread: every prompt and answer is a stored message with its author, position, and the value it carries (FR-ENT-026).
- Entries complete on the final value, independently of the conversation (FR-ENT-020).
- Drafts resume at the first unrecorded value; history lists finished entries and drafts together and is searchable by what the user wrote.
- Deletion removes the entry, its thread and its recorded values permanently (FR-ENT-008).
- **Not implemented:** voice input (FR-ENT-010 to 017), which needs a speech conversion service.

### Journal types (SRS 4.6)

| Journal | Values | Ending |
|---|---|---|
| Check-in | mood (1–5), triggers, note, thinking traps, feelings, feeling intensity (0–10) | Conversation offered |
| Savouring | event, why it mattered, feelings (positive first) | One closing message, no conversation (FR-JRN-002) |
| Thought record | name, situation, automatic thought, traps, feelings, feeling intensity, supporting and contradicting evidence, revised thought | Conversation offered |
| Thought record, trauma variant | no supporting evidence, situation optional | Fixed grounding message (FR-JRN-005) |
| Exposure | feared outcome, distress before, planned activity, account, distress during and after, learning | Conversation offered; a further cycle reuses the same feared outcome (FR-JRN-006) |
| Free write | one account, which may be sent as several messages | **Extended conversation** — see below |

- The exposure journal shows a fixed scope notice, which must be acknowledged before the first value, to users whose focus areas include trauma (FR-JRN-007).
- Free write and the thought trauma variant end with a grounding message for those users (FR-JRN-008).
- **Free write is the extended session.** It runs longer before any closure condition fires (`content/conversation.json`: the same concern may be restated five times rather than three, three minimal replies rather than two, a containment failsafe of 60 turns rather than 30), and she can send several messages in the conversation before Echo answers, so a thought is never interrupted mid-way. Each message is still screened for crisis as it arrives.

### Selection libraries (SRS 4.7)

- 15 thinking traps (name, definition, example), 60 feelings in six categories, 40 triggers in six categories.
- The user may add her own terms (max 20 characters) to feelings and triggers; they are screened for crisis on submission (FR-PICK-006/007) and shown first as "My words".
- Categories linked to her focus areas are listed before the rest; in a savouring entry, positive feelings come first (FR-PICK-005).

### Crisis detection and response (SRS 4.8)

- **Two stages** on every free-text value: a local rule set and the model, with the higher tier winning (FR-CRIS-002). The rules alone decide when the model is unavailable.
- **Four tiers** (FR-CRIS-003): `clear`, `mild`, `danger`, `emergency`.
  - *mild* — no interruption; a support reference appears at the end of the entry (FR-CRIS-005).
  - *danger* / *emergency* — fixed content from the content set, and **every AI call for that entry stops** (FR-CRIS-006/007). The entry can still be completed (FR-CRIS-004).
- A repeat danger response within 7 days is abbreviated (FR-CRIS-012).
- Each non-clear assignment is recorded with tier, field, entry and time — **never the text** (FR-CRIS-013).
- Crisis content is rendered from `content/crisis_responses.json` and is never generated (FR-CRIS-008).

### AI reflection (SRS 4.9)

- **Two calls per turn.** A cheap classifier returns the topic (from a fixed list of ten) and the risk tier; the main call writes the reply. Structured output makes an invalid value impossible.
- Prompt order: base instructions → clinical context (highest priority) → skills for the detected topic → journal type → profile → recorded entry → reply language.
- The skill files' `description` lines are what the classifier chooses between, so the skills are the single source of truth for both.
- **Language and register are decided in code**, not left to the model: the reply follows the language of the user's latest message (English, Roman Urdu, mixed, Urdu script) and always addresses her as "aap".
- Closure is enforced by code as well as prompt: two minimal replies, the same concern three times, a request to stop, or an invisible length failsafe. At most one question per reply; a closing message has none.
- When she reaches a new way of seeing something, the conversation **consolidates for one turn and closes on the next**, so it tapers rather than stopping dead.

### Insights (SRS 4.10)

- One period control — 7 days, 30 days, 3 months, all — applied to every view (FR-INS-001).
- **Days are hers, not the server's.** The client sends its UTC offset and every date — chart points, the calendar, the streak — is grouped by her local day, so an entry written at 1am counts for that day.
- **Streak**: days in a row with at least one completed entry, counted over her whole history, plus the longest so far and this week against the weekly goal she set. Missing today does not break it; missing a whole day does.
- The calendar returns each day with the mood she recorded on it, and the streak carries her all-time entry count and this month's, so the client can draw the ring without a second request.
- Mood and feeling-intensity series drawn as line charts (react-native-svg) with gridlines and the ends of the scale named; each point sits on the day it was recorded, so a gap in writing shows as a gap. Tapping a point opens that entry.
- **Trends over time**, which is what the section is actually for: the weekly average mood, how often each thinking pattern and trigger came up week by week (the three most frequent, as lines), and how her entries were spread across the mood scale.
- Trigger, feeling and trap counts ordered by frequency, each with its change against the previous period, exposure distress per cycle, when in the day she writes, activity calendar, journal type counts.
- A series carries the ends of its own scale, so no chart hardcodes them.
- Counts are entries, never percentages (FR-INS-007); nothing is compared with other users (FR-INS-022); only completed entries are counted (FR-INS-023).
- **The wellbeing questionnaire works, but its instrument is a generic placeholder** (`content/questionnaire.json` says so). Offered every 14 days after the last completion or decline; the score is stored and never shown (FR-INS-018); the trend is drawn as a position between 0 and 1 with no numbers, no severity bands and no condition named (FR-INS-019). The real instrument must come from the Clinical Advisor.
- FR-INS-012 works: tapping a thinking pattern opens the library article that covers it.

---

## API Endpoints

Authenticated endpoints require an `Authorization: Bearer <token>` header.

| Method | Path | Auth | Description |
|---|---|:---:|---|
| GET | `/health` | No | Liveness check |
| POST | `/auth/signup` | No | Begin signup with an email; sends a verification code, or `409` if already registered |
| POST | `/auth/verify-signup-code` | No | Redeem a signup code; returns a setup token |
| POST | `/auth/set-password` | No | Redeem a setup token, set a password, receive the first session |
| POST | `/auth/login` | No | Authenticate; returns a token pair |
| POST | `/auth/refresh` | No | Rotate a refresh token |
| GET | `/auth/me` | Yes | Current account details |
| POST | `/auth/forgot-password` | No | Begin a password reset; sends a reset code |
| POST | `/auth/verify-reset-code` | No | Redeem a reset code; returns a reset token |
| POST | `/auth/reset-password-code` | No | Redeem a reset token, set a new password |
| GET | `/auth/verify-email` | No | Legacy link-based verification — superseded by the code flow, unused by the client |
| POST | `/auth/reset-password` | No | Legacy link-based reset — superseded by the code flow, unused by the client |
| GET | `/onboarding/status` | Yes | Onboarding progress and current consent version |
| GET | `/onboarding/distress-scale` | No | Written descriptions for values 0–10 |
| GET | `/onboarding/focus-areas/options` | No | Valid focus area codes |
| GET | `/onboarding/work-issues/options` | No | Valid work issue codes |
| POST | `/onboarding/consent` | Yes | Record consent acknowledgement |
| PUT | `/onboarding/focus-areas` | Yes | Replace focus area selections |
| PUT | `/onboarding/work-issues` | Yes | Replace work issue selections |
| POST | `/onboarding/life-vision` | Yes | Record life vision (max 160 characters) |
| POST | `/onboarding/distress-baseline` | Yes | Record distress baseline |
| PUT | `/onboarding/goal` | Yes | Set weekly entry goal |
| GET | `/entries` | Yes | Entries, newest activity first; `status`, `q` (search), `since_days`, `limit` |
| POST | `/entries` | Yes | Start an entry; `parent_entry_id` starts a further exposure cycle |
| GET | `/entries/{id}` | Yes | One entry with its whole thread and the value being requested |
| DELETE | `/entries/{id}` | Yes | Delete an entry permanently |
| POST | `/entries/{id}/acknowledge` | Yes | Acknowledge a scope notice, so capture can begin |
| POST | `/entries/{id}/captures` | Yes | Record one value; `more: true` keeps a repeatable value open |
| POST | `/entries/{id}/conversation` | Yes | `continue`, `stop`, or `end` the reflection conversation |
| POST | `/entries/{id}/messages` | Yes | One conversation turn; `more: true` stores the message and waits, so she can keep writing |
| GET | `/libraries/{name}` | Yes | A selection library, ordered for this user; `prefer=positive` |
| POST | `/libraries/{name}/terms` | Yes | Add one of the user's own terms |
| GET | `/insights` | Yes | Every insights view for one `period` (`7d`, `30d`, `3m`, `all`); `tz_offset` in minutes so days are counted in her timezone |
| GET | `/library` | Yes | Articles, with `q` search, `trap`, `category` and `saved_only` filters |
| GET | `/library/{slug}` | Yes | One article with its text |
| POST | `/library/{slug}/favourite` | Yes | Save an article |
| DELETE | `/library/{slug}/favourite` | Yes | Unsave it |
| GET | `/insights/questionnaire` | Yes | The questionnaire items and scale — no scoring information |
| POST | `/insights/questionnaire` | Yes | Record answers; the score is stored and never returned |
| POST | `/insights/questionnaire/decline` | Yes | Decline this offer; the next one waits the same period |
| GET | `/crisis/resources` | No | Crisis helpline resource list |

> Full request and response schemas are generated automatically at `/docs`.

---

## Database

| Table | Purpose |
|---|---|
| `accounts` | Credentials (nullable until signup completes), onboarding responses, preferences |
| `focus_areas` | Focus area selections, one row per selection |
| `refresh_tokens` | Hashed refresh tokens with family and revocation state |
| `verification_tokens` | Hashed single-use tokens and 6-digit codes, distinguished by purpose |
| `known_devices` | Device fingerprints seen per account |
| `entries` | One journal entry: type, status, conversation state and stage, crisis tier, exposure cycle link, notice acknowledgement |
| `messages` | Every message of a thread: author, kind, position, and the value it carries |
| `captured_values` | The typed answers of an entry, JSON-encoded, with an explicit skip flag |
| `crisis_events` | Tier, field, entry and time of each non-clear detection — no text |
| `user_terms` | The user's own additions to the feelings and triggers libraries |
| `questionnaire_responses` | One row per offer, completed or declined, with the stored score she is never shown |
| `library_favourites` | Articles she saved from the learning library |
| `alembic_version` | Current migration state |

- UUID primary keys throughout, so identifiers cannot be enumerated or used to infer user counts.
- `password_hash` on `accounts` is nullable: a row with no password is a pending signup.
- Deleting an account cascades to its entries, messages, values and terms; a deleted entry leaves its crisis records intact but unlinked, because those records hold no content.

---

## Migrations

```bash
# After changing a model, generate a migration
alembic revision --autogenerate -m "description of the change"

# Review the generated file in alembic/versions/, then apply it
alembic upgrade head

# Roll back the most recent migration
alembic downgrade -1
```

> **Always** read a generated migration before applying it. Autogenerate is reliable but not infallible, and a migration that drops a table is not recoverable.

---

## Testing

- **Flow suite:** `flow_test.py` at the repository root — 228 checks over the whole engine against an in-memory SQLite database. Run it with `python flow_test.py`; it touches nothing outside its own in-memory database.
- **Postman:** `postman/echo-check-in.postman_collection.json` — 100 requests, 216 assertions, each with a saved example response from a real run. Set `seedEmail` and `seedPassword`, then run the folders in order. Folder 05 creates crisis detection records, so use a test account.
- **Model behaviour:** `python -m evals.check_in_eval` runs the real prompts against the configured model and checks the rules that only show up in real output — one question per reply, no question in a closing message, the reply language, "aap" rather than "tum", and closure at the right turn. **Rerun it after any change to a prompt, skill, or clinical file.**
- There is no automated test suite in CI yet.

---

## Deployment

Live on FastAPI Cloud at
**https://emotional-support-backend-a0039c1a.fastapicloud.dev**

A push does **not** deploy. There is no GitHub Actions workflow here, so a
deploy is run explicitly from a directory linked with `fastapi cloud link`:

```bash
fastapi deploy
```

The upload honours `.gitignore`, so `.env`, `venv/` and `__pycache__/` stay out
of it.

### Dependencies must be declared, not just installed

The host serves the app with `fastapi run app/main.py` in a container built
from `requirements.txt` alone. Anything installed in the local venv but missing
from that file works here and fails there, after a successful build:

- **`fastapi[standard]`**, not plain `fastapi` — `fastapi run` lives in
  `fastapi-cli`, which the plain pin does not pull in. Without it the container
  dies with `RuntimeError: To use the fastapi command...`, which FastAPI Cloud
  surfaces only as a deployment stuck on "Verifying Readiness".
- **`resend`** — imported by `app/core/email.py`.

Both of these broke a deployment before being noticed. `numpy`, `scikit-learn`
and `sentence-transformers` are also undeclared, but they are reached only
through `app/core/domain_detection.py`, which nothing on the app's runtime path
imports.

### Settings

`DATABASE_URL`, `JWT_SECRET` and `RESEND_API_KEY` have no defaults, so the app
will not start without them, and `.env` is never uploaded. Eight settings belong
on the host, never in the repository:

| Key | Secret | Value |
| --- | --- | --- |
| `DATABASE_URL` | yes | the Supabase pooler URL |
| `JWT_SECRET` | yes | — |
| `RESEND_API_KEY` | yes | — |
| `OPENAI_API_KEY` | yes | — |
| `OPENAI_BASE_URL` | yes | the Azure resource's `/openai/v1/` endpoint |
| `OPENAI_CLASSIFIER_MODEL` | no | `gpt-4.1-mini` |
| `OPENAI_RESPONSE_MODEL` | no | `gpt-4.1-mini` |
| `APP_BASE_URL` | no | the deployed address, used in email links |

`LOG_VERIFICATION_CODES` must never be set on a host; its default `false` is
what keeps login codes out of logs.

Set them with `fastapi cloud env set NAME VALUE`, adding `--secret` for the
five credentials. Two things to know: `--value-stdin` hangs in Windows `cmd`
because it waits on stdin with no prompt and never sees EOF, and `env set` does
not reliably trigger the redeploy its help text promises — check
`fastapi cloud deployments list` and run `fastapi deploy` if nothing new
appears.

### Migrations

Not run by the platform. The database is the same Supabase instance used
locally, so `alembic upgrade head` is run from a machine that has it configured.

`render.yaml` is kept for a Render deployment, where `preDeployCommand` does run
migrations. It is unused at present.

---

## Known Limitations

Recorded in full, with reasoning, in the project development log.

- **All clinical content is draft.** Crisis responses, the grounding message, the exposure scope notice, every skill and clinical context file, and all three libraries need Clinical Advisor authorship and approval before release. SRS FR-CRIS-008 makes this a release condition, not a preference.
- **Helpline numbers are unverified.** The `last_verified` dates in `crisis_resources.json` should only be set once someone has called each number.
- **No voice input** (FR-ENT-010 to 017).
- **The questionnaire instrument is a placeholder** (FR-INS-017): five generic items, not a validated measure.
- **The learning library's articles are drafts.** Six Markdown files under `content/library/`, each saying in its own frontmatter that it awaits Clinical Advisor review. Searchable over title, summary and body; an article declares which thinking patterns it covers, which is how insights links to it.
- **No data export** (FR-DATA-001); only per-entry deletion exists.
- **No email delivery beyond the account holder's own address** until a sending domain is verified in Resend.
- **Rate limiting is per-process** and resets on restart.
- **Crisis tier boundaries are a draft split** of the regex rules, not a clinically defined criteria set.
- No automated tests run in CI; verification to date is the Postman collection, the eval set, and manual testing.

---

## Security Notes

- `.env` and every `.env.*` variant are git-ignored. Database credentials, the JWT secret and the model key must never be committed.
- The Supabase Data API is disabled for this project. The database is reachable only through this backend.
- Access tokens are bearer credentials and should be treated with the same care as passwords.
- User text is sent to the configured model provider for classification and reply generation. Crisis detection records deliberately store no text, and prompts wrap user-written values in `<user_text>` tags so they are treated as information rather than instructions.
- Verification codes are printed to the console only when `LOG_VERIFICATION_CODES` is `true`. It defaults to `false`, which is what keeps login codes out of server logs. Set it in a local `.env` if you need to read a code while the Resend sending domain is unverified; **never set it on a deployed host.**
