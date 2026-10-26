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
VoiceEntry=                    # Groq key for voice messages (Whisper large v3); unset turns voice off
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
| 4 | Dashboard and engagement | 4.4 | ✅ Streak, entry counts | ✅ Complete |
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
- Focus areas, work issues, life vision (160 characters), distress baseline (0–10). The weekly goal question was removed (2026-10-26).
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
| Check-in | three a day, each slot with its own questions (see *Check-ins, three a day*); a check-in without a slot (before 23 Oct) asked mood (1–5), triggers, note, thinking traps, feelings, feeling intensity (0–10) | Conversation offered |
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
- **Streak**: days in a row with at least one completed entry, counted over her whole history, plus the longest so far and how many days of this week had one. Missing today does not break it; missing a whole day does.
- The calendar returns each day with the mood she recorded on it, and the streak carries her all-time entry count and this month's, so the client can draw the ring without a second request.
- Mood and feeling-intensity series drawn as line charts (react-native-svg) with gridlines and the ends of the scale named; each point sits on the day it was recorded, so a gap in writing shows as a gap. Tapping a point opens that entry.
- **Trends over time**, which is what the section is actually for: the weekly average mood, how often each thinking pattern and trigger came up week by week (the three most frequent, as lines), and how her entries were spread across the mood scale.
- Trigger, feeling and trap counts ordered by frequency, each with its change against the previous period, exposure distress per cycle, when in the day she writes, activity calendar, journal type counts.
- A series carries the ends of its own scale, so no chart hardcodes them.
- Counts are entries, never percentages (FR-INS-007); nothing is compared with other users (FR-INS-022); only completed entries are counted (FR-INS-023).
- **The weekly questionnaires are PHQ-9 and GAD-7**, asked inside the evening check-in (below), so Insights never offers one. The scores are stored and never shown (FR-INS-018); each trend is drawn as a position between 0 and 1 with no numbers, no severity bands and no condition named (FR-INS-019). The older generic placeholder (`content/questionnaire.json`) and its endpoints remain but are no longer offered.
- FR-INS-012 works: tapping a thinking pattern opens the library article that covers it.

### Check-ins, three a day

A check-in now has a **slot**: morning (05:00–11:59), afternoon (12:00–17:59) or evening (18:00–04:59, which stays that evening's day). Windows are on the user's clock (`core/slots.py`).

- **One per slot per day**: `entries` carries `slot`, `local_date`, `tz_offset` and `timezone`, with a partial unique index (`uq_entries_checkin_slot`). Starting a started slot resumes it; a finished one returns 409.
- **A slot can be done only inside its window**: no backfill. Once the window has passed it is closed, and an answer or a change sent to a check-in after that is refused (409); an unfinished one is no longer offered to resume. A **finished slot check-in is never changed** (`PATCH .../captures/...` returns 409), so the reports below never change after they appear.
- **Each slot asks its own questions** (`content/capture_schedules/check_in_slots/*.json`). Core in every slot: mood (1–5), stress (0–10), energy (1–5), feelings and how strong each is (0–10). Morning adds sleep, an intention and anything hard ahead; afternoon adds activities, enjoyment, getting things done, a stressful event (its follow-ups only after a Yes: the schedule's `when` key) and coping; evening adds the day's rating, activities, the plan, what helped and what was hard, three Yes/No questions that open the Thought, Saving and Exposure journals, and one win.
- **PHQ-9 and GAD-7 weekly** (`core/assessments.py`): included in the evening only when none was answered or declined in the week before the entry started; worded exactly as published (`fixed: true`, never by the model); scored into `questionnaire_responses` (`phq9-v1`, `gad7-v1`) when the evening completes, or recorded as declined if left part-way. Severity bands exist for a future clinician report only. Answers cannot be changed afterwards.
- **Safety**: PHQ-9 item 9 above "Not at all" records a `danger` crisis event (`field = phq9_9`), puts the fixed crisis content in the thread, stops AI for the entry, and the response tells the app to open the crisis resources at once (`crisis_event.show_resources`). `POST /entries/{id}/crisis/acknowledge` stamps `crisis_events.acknowledged_at` when she moves on.
- **Cross-links**: `POST /entries` with `source_entry_id` (a check-in) opens a journal whose feelings card arrives pre-ticked with the check-in's feelings (`next_capture.preselected`).
- `GET /entries/check_in/slots?tz_offset=` lists yesterday's and today's six slots: done, started, open, upcoming or closed, with their windows.
- Reminders are local to the phone (the app's own Android module), one per slot (08:00 / 14:00 / 21:00 by default), so the server stores no reminder settings. A slot already finished that day stays quiet.

### Check-in reports: a day, a week, a month

PDFs of the slot check-ins only (`core/checkin_reports.py` builds the data, `core/checkin_pdf.py` draws it with ReportLab). Built on request, never stored.

- **Ready once the period is over on her clock**: a day at 05:00 the next morning (when its evening closes), a week (Monday to Sunday) on the Monday after, a month on the 1st. Before that the PDF returns 409; a period without a finished check-in returns 404.
- `GET /insights/check-ins/reports?tz_offset=` lists what she can open, newest first: `latest_daily` (the day before the one she is in — the app shows it on top until today is over, with `check_ins: 0` when it had none), then finished `daily` (30), `weekly` (12) and `monthly` (12) with at least one check-in.
- `GET /insights/check-ins/report.pdf?kind=daily|weekly|monthly&start=YYYY-MM-DD&tz_offset=` (a week starts on its Monday, a month on the 1st; otherwise 422). `Cache-Control: no-store`.
- **Structure**: "What stood out" (the conclusions) first, then charts and tables, her own words, and the helplines. A day: each slot's mood, stress and energy (bar charts), feelings and their strength, then the morning, afternoon and evening answers. A week: mood by slot across the days (lines), stress, enjoyment and getting things done, sleep and day ratings by day (bars), averages by time of day, sleep against mood, feelings, triggers, activities, coping, better and harder days, the plan, wins / what helped / what was hard. A month: a mood calendar, mood and stress week by week, and the same tables.
- **Conclusions are rule-based**, each shown only with enough behind it (`MIN_GROUP = 3` on each side of a pattern, gaps of 1 point of stress and 0.5 of mood): calmest time of day, best mood time, sleep against mood, what better days held, stressful days and their trigger, coping, the most-named feeling, best and hardest day, the plan, and the change against the previous week or month. A day only describes. The wording is in `content/checkin_report.json` (`DRAFT_NOT_CLINICALLY_APPROVED`).
- **PHQ-9 / GAD-7** appear in weekly and monthly reports only as two unlabelled lines of positions (no score, no band, no condition named).

### Saving Journal (savouring)

Noticing, appreciating and extending a good moment: one that **just happened**, one **remembered**, or one **looked forward to**. Depth over volume. `core/savoring.py`, `core/savoring_report.py`, `routers/savoring.py`.

- **Stored on the shared engine**: an `entries` row (`journal_type = 'savouring'`, `local_date`, `client_id`, `source_entry_id`, plus `is_favorite` and `event_date`) and one `captured_values` row per answer (`savor_type`, `savor_mode`, `event`, `categories`, `people`, `places`, `mood_before`, `feelings`, `feeling_ratings`, `sensory_note`, `mood_after`, `techniques`, `significance`, `souvenir`, `expected_rating`, `actual_rating`). Moments from before the module read the same way.
- **The app owns the steps** (fixed wording, `src/savoring/content.ts`) and sends a moment once: `POST /savoring/entries`. Quick: what happened, mood before (0–10), good feelings with a strength each, save (mood after optional). Full: a 30-second savor pause, then mood after, techniques, what made it good, a souvenir line, and for an anticipated moment how good she expects it to be and when. Only "what happened" is required.
- **Rules, checked on the server**: at most **3 moments a day** on her clock (a 409 `{code: "daily_cap", message}` worded for her); dated **today or yesterday** (`outside_window`); a retry or an offline sync with the same `client_id` is saved once. Fixed lists for categories (`libraries/savoring_categories.json`), techniques (`savoring_techniques.json`) and feelings (the feelings library's positive items, now with Calm and In awe; her own words allowed). Free text is crisis-screened like every journal.
- **From a check-in**: `GET /savoring/prefill?source_entry_id=` gives the check-in's positive feelings to start ticked; the moment keeps `source_entry_id`.
- `GET /savoring/home` (today's and yesterday's room, the last three, "how was it really?" follow-ups, memory lane, the support signal), `GET|PUT /savoring/entries[/{id}]` (history with `type`, `emotion`, `favorite`, `date` filters; an edit replaces what it says), `PUT .../favorite`, `POST .../actual` (only once the awaited day has come), delete is `DELETE /entries/{id}`.
- **Heatmap and streak**: `GET /savoring/heatmap?from=&to=` → `{days: [{date, points, level, grace_day}], streak: {current, best, total_days, today_counted}}`. A quick moment is 1 point, a full one 2; levels 0 / 1 / 2 / 3+. A day with any moment counts; one missed day between two counted ones is bridged automatically, once in any 7 days; today never breaks it while it lasts.
- **Memory lane**: two or three moments at least a week old, favourites first, the same all week.
- **Support**: when the last 5 moments all ended at mood 2 or below, `support` is `break` (a short break, someone to talk to); with a check-in average of 2 or less over 7 days, `reach_out` (professional support). Never a block.
- **Report**: `GET /reports/savoring?from=&to=`: days, moments per active day, average lift (moments with both moods only), the three kinds, top feelings (her own words grouped as "Other (your words)"), categories, people, places, time of day, techniques ranked by lift (3 uses at least), expected vs actual, savoring days beside the week's check-in mood, highlights. Insights only from 7 moments, worded as patterns (`content/savoring_report.json`); trends count at most 5 moments a day.
- **Combined clinical report**: `savoring_report.savoring_section(db, account_id, period, consent)` returns section D (counts, average lift, top feelings, techniques by lift, anticipation vs reality, adherence; excerpts only with `consent.include_excerpts`; no streaks), or `{"empty": true, "note": "No savoring entries in this period"}`; it never raises. `core/journal_events.py` gives the shared `{user_id, module, entry_id, local_date, created_at, mood_signal}` rows (a function, not a view, so SQLite and Postgres agree).
- **CSV**: `GET /savoring/export.csv?from=&to=&include_notes=` (one row per moment, feelings and techniques joined, her written notes only with `include_notes`, cells that could run as formulas escaped).
- **Not used, on purpose**: Expo, Victory Native, WeasyPrint and Supabase RLS (the backend filters every query by account and the tests check another account sees nothing), column encryption (Supabase encrypts the disk; column encryption would break search). The opt-in reminder and the default mode live on the phone.

### Thought Journal (CBT thought record)

She catches an upsetting automatic thought, looks at the evidence, writes a balanced alternative, and sees her belief and feelings change. Curious and gentle: nothing calls a thought wrong. `core/thought.py`, `core/thought_report.py`, `routers/thought.py`.

- **Same engine, same ids**: `entries` (`journal_type = 'thought'`, `client_id`, `local_date`, `source_entry_id`, `is_favorite`, new `edited_at`) and `captured_values` under the journal's ids from before (`situation`, `automatic_thought`, `feelings`, `emotions_before` / `emotions_after` — one 0–100 rating per feeling, `thinking_traps`, `supporting_evidence`, `contradicting_evidence`, `revised_thought`) plus new ones (`thought_mode`, `situation_tags`, `second_thought`, `belief_before`, `belief_after`, `balanced_belief`, `next_step`). `thought.FIELDS` maps them to the names the app and the clinical report use (`evidence_for`, `evidence_against`, `balanced_thought`). Records from before load, display and report as usual; every average skips what they lack.
- **The app owns the steps** (fixed wording, `src/thought/content.ts`). Quick (~3 min): situation and tags, feelings with a strength each, the thought (and an optional second), belief %, the balanced thought, re-rate. Full (~8 min) adds thinking patterns (with examples), evidence for and against (with fixed helper questions), and a next step. The **trauma variant** (FR-JRN-005) still leaves out "what makes it feel true" and makes the situation optional.
- `PUT /thought/records/{client_id}`: every step saves a draft (`complete: false`), the last one finishes it (`complete: true`), and the same call carries a later change — so a retry or an offline sync is the same record. The app keeps a copy of each step on the phone. Drafts are kept **7 days** (`GET /thought/home` lets older ones go; a safety flag raised in one stays, unlinked). A finished record is counted on the day it was finished.
- **Edits**: `PUT /thought/entries/{id}` — free for 24 hours after finishing, marked `edited_at` after that.
- **Safety**: free text that changed in a save is crisis-screened at once (each step and the final save); a flag opens the resources and she chooses whether to go on. From **3 finished records in a day**, `gentle: true` brings a kind word (a break, grounding, someone to talk to), never a block. A **recurring theme** (3+ records in 30 days sharing a tag and a pattern, belief still ≥ 70% with a drop under 10) suggests talking with a professional, counted from tags and patterns, never read from her words.
- **From a check-in**: `GET /thought/prefill?source_entry_id=` gives the check-in's feelings; the app offers the journal when a slot check-in has stress ≥ 7 or mood ≤ 2 (and on the evening's "a thought still bothering you").
- **AI assist, only when she asks**: `POST /thought/entries/{id}/question {side}` (one Socratic question about the evidence) and `POST /thought/entries/{id}/traps` (up to two pattern ids for her to confirm). Prompts in `app/thought_assist.md`; never advice, a diagnosis, a label or the balanced thought; 8 calls per 15 minutes; none after a crisis flag; `null` when the model is not there, and the app asks a fixed question instead. The old "another perspective" and "Talk it through" are not part of the new flow.
- `GET /thought/entries` (finished records, filters `tag`, `trap`, `favorite`, `date`), `GET /thought/entries/{id}` (a draft or a record; one from before gets a `client_id`), `PUT .../favorite`, `GET /thought/heatmap` (a day counts with one finished record or more; the shared `core/streaks.py`, grace day as in the Saving Journal).
- **Report**: `GET /reports/thought?from=&to=`: finished count and completion rate, belief drop, feelings eased, balanced-thought belief (each only from 5 finished records that carry those ratings), top patterns and tags, records opened from a hard check-in, the reframes with the biggest drop, the recurring theme; insights worded as patterns (`content/thought_report.json`).
- **Section B** for the combined clinical report: `thought_report.thought_section(db, account_id, period, consent)` (the metrics above, the recurring theme from counts, excerpts only with `consent.include_excerpts`, under the report's own names) or `{"empty": true, "note": "No thought records in this period"}`; never raises. Finished records are also `journal_events` rows (`mood_signal`: how much the feelings eased, on 0–10).

### Exposure Journal (graded exposure)

Inhibitory learning: she builds a fear ladder, **predicts** what will happen, does a step, and **reviews** the gap between prediction and reality. Stopping, pausing or doing part of a step is never a failure. Fixed wording, no AI. `core/exposure.py`, `core/exposure_report.py`, `routers/exposure.py`.

- **Ladders**: new tables `exposure_ladders` (fear name, fixed tags, archived) and `exposure_ladder_items` (description, expected SUDS 0–100, order, archived), both with **row-level security** on Supabase (a step owned through its ladder). The backend still checks ownership in every query; RLS keeps Supabase's own API roles out. `GET|POST /exposure/ladders`, `PUT /exposure/ladders/{id}`, `POST|PUT|DELETE .../steps[/{item_id}]`, `PUT .../order`. New steps are placed by expected anxiety; she can reorder. **A step (or ladder) an exposure points at is archived, never deleted**, so records and reports keep its name. Ladder words are crisis-screened like any free text, and kept like all free text (disk-encrypted by Supabase, never logged).
- **Exposures** stay `entries` (`journal_type = 'exposure'`) with `captured_values`: the journal's ids from before (`feared_outcome`, `distress_before/_during/_after` on 0–10, `planned_activity`, `post_account`, `learning`) plus new ones (`exposure_status`, `exposure_mode`, `ladder_item_id`, `predicted_likelihood/_severity/_anxiety`, `safety_behaviors_planned/_used`, `learning_goal`, `scheduled_for`, `anxiety_peak/_end/_readings` on 0–100, `outcome_occurred`, `next_step`, `feelings`). `exposure.FIELDS` names them (`post_account` → `what_happened`); **records from before read their 0–10 distress ×10** (during as the peak, after as the end) and still load, show, report and export.
- **One mapping for the state** (`exposure.ENTRY_STATUS`, set in one place): draft / planned → `in_progress`; completed / partial → `completed`; skipped → `closed`. Transitions only go forward (a finished record can be corrected between completed, partial and skipped, never sent back to a plan). Streaks, the heatmap, report averages and `journal_events` count completed and partial only; the weekly report and Insights read `exposure_status` too, so a skipped step is never counted as tried.
- `PUT /exposure/records/{client_id}` saves every step (draft), the plan (`planned`, with an optional local `scheduled_for`), and how it went; drafts and plans are kept **7 days**. Edits after 24 hours are marked `edited_at`. Changed words are crisis-screened at each save; a flag opens the resources and she chooses whether to go on.
- `GET /exposure/home`: drafts, plans, the last three, the ladders, the kind word from the **2nd exposure in a day**, the **no-change pattern** (one step tried 4+ times over 2+ weeks with neither the predicted likelihood nor the peak coming down by 5), and `suggest_professional` (trauma focus or onboarding distress ≥ 8) for the app's one-time screening note. `GET /exposure/prefill` (from a check-in or a thought record), `GET /exposure/entries[/{id}]`, `PUT .../favorite`, `GET /exposure/heatmap`.
- **Report** `GET /reports/exposure`: done / in part / set aside, ladders in progress, steps tried and completed, the highest completed, exposures per week, predicted likelihood against how often the outcome happened (partly = half), predicted / peak / end anxiety, repeated attempts per step, safety behaviours planned against used; averages from 3 exposures; pattern wording (`content/exposure_report.json`).
- **Section C** `exposure_report.exposure_section(db, account_id, period, consent)` (or `{"empty": true, "note": "No exposure entries in this period"}`; never raises), `journal_events` rows (`mood_signal`: how far the peak fell short of the prediction, on 0–10), and her **export** `GET /exposure/export.json?include_notes=` with the ladders, their steps and every exposure (her notes only on request). Account deletion removes the ladders and their steps.
- The planned exposure's reminder is a one-off alarm on the phone (`scheduleOnce` in the app's own module), cancelled as soon as the plan is done, done in part, set aside or changed.

**Setup**: `alembic upgrade head` applies `c3e5a7b9d1f2` (slot columns, the unique index, `source_entry_id`, `crisis_events.acknowledged_at`), `d4f6a8c0e2b3` (`entries.is_favorite`, `entries.event_date`, index on account, journal and day), `e5a7c9e1f3b4` (`entries.edited_at`) and `f6b8d0f2a4c5` (the ladder tables, with RLS on Postgres). The reports need no migration. `flow_test.py` covers the windows and no backfill, one-per-slot (API and index), finished check-ins kept as they are, per-slot questions, the `when` rule, PHQ-9/GAD-7 due logic, wording, scoring and bands, item 9, acknowledgement, cross-links, the slots board, and the reports: readiness, periods, conclusions with their thresholds, the listing and the PDFs; and the Saving Journal: the daily cap (with a day boundary on her clock and a retried client id), the window, validation, mood lift with a missing mood after, follow-ups, favourites, edits, the check-in prefill, another account's isolation, heatmap points and levels, the streak's grace day, pending today and best run, the report's thresholds, technique minimum and 5-a-day trend cap, section D (empty, consent, no streak), journal_events, the CSV and its notes toggle, crisis screening and the support signal; and the Thought Journal: records from before (load, display, new ratings, Insights and its PDF), drafts saved, resumed and expired (the safety flag kept), completed-only counting, the 24-hour edit window, belief and feeling drops, the third-of-a-day kind word, crisis screening once per change, the check-in prefill, assist with and without a model and its rate limit, the trauma variant, isolation, the heatmap, the 5-record minimum, the recurring theme and section B.

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
| `VoiceEntry` (or `GROQ_API_KEY`) | no | Groq key for voice messages; unset turns voice off and the app falls back to typing |
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
