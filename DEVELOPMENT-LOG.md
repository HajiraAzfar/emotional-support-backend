# Development Log — Emotional Support Companion

Backend implementation record for SRS-ESC-001 v0.9.

Keep this file in the repository root and update it at the end of each working
session. Its purpose is to record **what was built, what was deferred, and why** —
the reasoning is the part that is hardest to reconstruct later.

---

## 1. Technology stack

| Layer | Choice | Version | Notes |
| --- | --- | --- | --- |
| Language | Python | 3.13.4 | |
| API framework | FastAPI | 0.141.1 | Auto-generates OpenAPI docs at `/docs` |
| Server | Uvicorn | 0.52.4 | Run with `--reload` in development |
| ORM | SQLAlchemy | 2.0.52 | |
| Database driver | psycopg2-binary | 2.9.12 | |
| Database | PostgreSQL (Supabase) | 17 | Hosted, `ap-south-1` (Mumbai) |
| Migrations | Alembic | 1.19.2 | |
| Password hashing | bcrypt | 5.0.0 | Used directly, not via passlib |
| Tokens | python-jose | 3.5.0 | HS256 |
| Settings | pydantic-settings | 2.15.0 | Reads `.env` |

**Frontend (not yet started):** React Native.

---

## 2. Project structure

```
emotional-support-backend/
├── alembic/
│   └── versions/          Migration scripts
├── app/
│   ├── content/           Clinical content sets (JSON)
│   ├── core/              Config, database, security, dependencies, rate limiting
│   ├── models/            SQLAlchemy models (database tables)
│   ├── routers/           API endpoints, one file per feature area
│   ├── schemas/           Pydantic schemas (request/response shapes)
│   └── main.py            Application entry point
├── .env                   Secrets — git-ignored, never committed
└── alembic.ini
```

**Models vs schemas.** Models describe what the database stores; schemas describe
what the API accepts and returns. They are deliberately separate so that fields
such as `password_hash` exist in the model but cannot appear in any response.

---

## 3. Data model (implemented so far)

### `accounts`
| Column | Type | Notes |
| --- | --- | --- |
| id | UUID | Primary key, randomly generated |
| email | varchar(255) | Unique, indexed |
| password_hash | varchar(255) | bcrypt hash; plaintext never stored |
| verified | boolean | Default false; does not gate app use (FR-AUTH-003) |
| weekly_goal | integer | Default 3 (FR-ONB-008) |
| interface_language | varchar(10) | Default `en` |
| consent_version | varchar(20) | Nullable (FR-ONB-002) |
| consent_at | timestamp | Nullable |
| distress_baseline | integer | Nullable, 0–10 (FR-ONB-006) |
| distress_baseline_at | timestamp | Nullable |
| created_at / updated_at | timestamp | |

### `focus_areas`
| Column | Type | Notes |
| --- | --- | --- |
| id | UUID | Primary key |
| account_id | UUID | FK → accounts, `ON DELETE CASCADE`, indexed |
| code | varchar(50) | Identifier, not display text |

Unique constraint on `(account_id, code)` prevents duplicate selections.

**UUID primary keys** are used rather than auto-incrementing integers so that
record identifiers cannot be enumerated or used to infer how many users exist.
This matters for an application holding mental health data.

---

## 4. Implemented endpoints

| Method | Path | Auth | Requirement |
| --- | --- | --- | --- |
| GET | `/health` | No | — |
| POST | `/auth/signup` | No | FR-AUTH-001, 002 |
| POST | `/auth/login` | No | FR-AUTH-004, 005, 006 |
| GET | `/auth/me` | Yes | FR-AUTH-006 |
| GET | `/onboarding/status` | Yes | FR-ONB-003, 010 |
| GET | `/onboarding/distress-scale` | No | FR-ONB-006 |
| POST | `/onboarding/consent` | Yes | FR-ONB-001, 002 |
| PUT | `/onboarding/focus-areas` | Yes | FR-ONB-004 |
| POST | `/onboarding/distress-baseline` | Yes | FR-ONB-006 |
| PUT | `/onboarding/goal` | Yes | FR-ONB-008 |

Authenticated endpoints expect an `Authorization: Bearer <token>` header.

---

## 5. Requirements status

### 4.2 Authentication

| ID | Status | Notes |
| --- | --- | --- |
| FR-AUTH-001 | Implemented | Duplicate email returns 409 with sign-in guidance |
| FR-AUTH-002 | Implemented | Salted bcrypt; hash excluded from all responses by schema |
| FR-AUTH-003 | Deferred to v1.1 | Requires an email delivery service |
| FR-AUTH-004 | Implemented | Identical body, status and content-length for both failure modes |
| FR-AUTH-005 | Implemented | 5 failures per email per 15 minutes; see deviation D-3 |
| FR-AUTH-006 | Partially implemented | Access token only; see deviation D-2 |
| FR-AUTH-007–014 | Not started | App lock — device-side only, scheduled after the entry engine |
| FR-AUTH-015 | Deferred to v1.1 | Requires email |
| FR-AUTH-016 | Deferred to v1.1 | Requires email and device fingerprinting |
| FR-AUTH-017 | Not started | Device-side |

### 4.3 Onboarding

| ID | Status | Notes |
| --- | --- | --- |
| FR-ONB-001 | Implemented (backend) | Endpoint exists; ordering enforced by the client |
| FR-ONB-002 | Implemented | Version and timestamp persisted |
| FR-ONB-003 | Implemented (backend) | Current version served in `/onboarding/status` |
| FR-ONB-004 | Implemented | Zero or more selections |
| FR-ONB-005 | Ongoing constraint | Focus areas stored as neutral codes |
| FR-ONB-006 | Implemented | Range enforced 0–10; descriptions served from content set |
| FR-ONB-007 | Not started | Client-side screen |
| FR-ONB-008 | Implemented | Restricted to 2, 3, 5, 7; defaults to 3 |
| FR-ONB-009 | Satisfied by design | No endpoint requires a prior step |
| FR-ONB-010 | Implemented | `/onboarding/status` reports the first unanswered step |

---

## 6. Deviations from the SRS

Each deviation is a deliberate scope decision, not an oversight. All should be
reflected in the SRS before submission.

**D-1 — Supabase used for the database only.**
The SRS assumes a self-managed PostgreSQL instance. Supabase provides hosted
PostgreSQL with automated backups and a publicly reachable address, which the
project needs for deployment and demonstration. Supabase Auth is *not* used;
authentication is implemented in FastAPI as specified.

**D-2 — Simplified session handling.**
FR-AUTH-006 specifies a short-lived access token with a rotating refresh token
and family revocation. Implemented instead: a single access token with a
seven-day expiry. Refresh rotation is deferred to v1.1. Consequence: a stolen
token remains valid until expiry, and logout is client-side only (the app
discards the token; the server cannot invalidate it).

**D-3 — Rate limiting held in application memory.**
FR-AUTH-005 is satisfied functionally, but failure counts are stored in a
process-level dictionary. They reset when the server restarts and are not shared
across multiple server processes. A production deployment would use Redis or an
equivalent shared store.

**D-4 — Email-dependent requirements deferred.**
FR-AUTH-003 (email verification), FR-AUTH-015 (password reset) and FR-AUTH-016
(new-device notification) all require an email delivery service. Grouped and
deferred to v1.1 so the dependency is introduced once rather than three times.

**D-5 — Distress scale wording is provisional.**
FR-ONB-006 names the Clinical Advisor as the source for the written descriptions
accompanying each 0–10 value. The current wording in
`app/content/distress_scale.json` is placeholder text and requires clinical
review before demonstration.

---

## 7. Technical decisions

**Capture schedules will be external JSON, not database rows.**
FR-ENT-001 requires that adding a value to a journal type needs no application
code change. Loading schedules from files at startup satisfies this and keeps
journal definitions reviewable in version control.

**Clinical content lives in `app/content/`, not in code.**
Wording that a clinical reviewer may need to change is stored as JSON and loaded
at startup, so review does not require reading Python.

**`pool_pre_ping` enabled on the database engine.**
Supabase's connection pooler closes idle connections. Without pre-ping,
SQLAlchemy hands out a dead connection after a quiet period and the request fails
with `server closed the connection unexpectedly`. This was observed in testing
and fixed.

**Authentication is a dependency, not per-endpoint code.**
`get_current_user` in `app/core/dependencies.py` extracts and verifies the token
and returns the account. Every protected endpoint declares it as a parameter, so
the check cannot be forgotten on a new endpoint.

---

## 8. Testing performed

Verified through the generated OpenAPI interface (`/docs`) and Postman.

| Case | Expected | Result |
| --- | --- | --- |
| Signup, new email | 201 with token | Pass |
| Signup, duplicate email | 409 | Pass |
| Signup, password under 8 characters | 422 | Pass |
| Signup, malformed email | 422 | Pass |
| Login, correct credentials | 200 with token | Pass |
| Login, wrong password | 401 | Pass |
| Login, unregistered email | 401, identical response | Pass |
| Login, 6th attempt after 5 failures | 429 despite correct password | Pass |
| `/auth/me`, valid token | 200 with account | Pass |
| `/auth/me`, no token | 401 | Pass |
| `/auth/me`, altered token | 401 | Pass |
| Onboarding consent | 200, version and timestamp stored | Pass |
| Onboarding focus areas | 200, both codes returned | Pass |
| Onboarding distress baseline | 200, value stored | Pass |
| Onboarding goal, value 5 | 200 | Pass |
| Onboarding goal, value 4 | 422 with explanatory message | Pass |
| Distress scale | 200, eleven descriptions | Pass |

No automated test suite exists yet. All testing to date has been manual.

---

## 9. Session log

### Session 1 — 6–7 September 2026

Environment set up from an empty repository: virtual environment, FastAPI,
package structure, Supabase project, Alembic.

Implemented the authentication backend (account model, bcrypt hashing, signup,
login, JWT issuance and verification, `get_current_user`, rate limiting) and the
onboarding backend (consent, focus areas, distress baseline, weekly goal,
distress scale content, status).

Three migrations applied: `create accounts table`, `add onboarding fields to
accounts`, `add focus areas table`.

Issue found and fixed: stale database connections from the Supabase pooler,
resolved with `pool_pre_ping` and `pool_recycle` (see section 7).

---

## 10. Next steps

1. **Entry engine (SRS 4.5)** — `Entry`, `Message` and `CapturedValue` models;
   capture schedule loading; step-level persistence.
2. **Check-in journal (FR-JRN-001)** — one journal type working end to end
   before the remaining four are added.
3. **Frontend** — signup and login screens, then onboarding.

Remaining after that: selection libraries, crisis detection, AI reflection,
insights, learning library, data control, and the device-side app lock.
