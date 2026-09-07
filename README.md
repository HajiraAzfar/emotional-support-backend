# Emotional Support Companion — Backend

REST API for a mobile journalling application providing structured emotional
support through guided journal entries, crisis-aware safeguards, and an AI
reflection layer.

Implements SRS-ESC-001 v0.9. Detailed progress, decisions and deviations are
recorded in [DEVELOPMENT-LOG.md](DEVELOPMENT-LOG.md).

---

## Stack

- **Python 3.13** with **FastAPI** and **Uvicorn**
- **PostgreSQL 17**, hosted on Supabase (`ap-south-1`)
- **SQLAlchemy** ORM with **Alembic** migrations
- **bcrypt** password hashing, **JWT** (HS256) sessions

The mobile client is React Native and lives in a separate repository.

---

## Getting started

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

The API is then available at `http://127.0.0.1:8000`, with interactive
documentation at `http://127.0.0.1:8000/docs`.

### Environment variables

Create a `.env` file in the project root. **It is git-ignored and must never be
committed.**

```
DATABASE_URL=postgresql://user:password@host:5432/database
JWT_SECRET=<generate with: python -c "import secrets; print(secrets.token_urlsafe(32))">
```

Optional, with defaults shown:

```
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=10080
CONSENT_VERSION=v1
```

---

## Project structure

```
app/
├── content/     Clinical content sets (JSON) — reviewable without reading code
├── core/        Config, database engine, security, auth dependency, rate limiting
├── models/      SQLAlchemy models — what the database stores
├── routers/     API endpoints, one file per feature area
├── schemas/     Pydantic schemas — what the API accepts and returns
└── main.py      Application entry point

alembic/versions/   Migration scripts, applied in sequence
```

Models and schemas are deliberately separate: `password_hash` exists in the
`Account` model but appears in no response schema, so it cannot leak through the
API.

---

## Module status

| # | Module | SRS section | Backend | Frontend |
| --- | --- | --- | --- | --- |
| 1 | Authentication (account layer) | 4.2 | Complete | Not started |
| 2 | Application lock (device layer) | 4.2 | Not applicable | Not started |
| 3 | Onboarding | 4.3 | Complete | Not started |
| 4 | Dashboard and engagement | 4.4 | Not started | Not started |
| 5 | Entry engine | 4.5 | Not started | Not started |
| 6 | Journal types | 4.6 | Not started | Not started |
| 7 | Selection libraries | 4.7 | Not started | Not started |
| 8 | Crisis detection and response | 4.8 | Not started | Not started |
| 9 | AI reflection module | 4.9 | Not started | Not started |
| 10 | Insights | 4.10 | Not started | Not started |
| 11 | Learning library | 4.11 | Not started | Not started |
| 12 | User data control | 4.12 | Not started | Not started |

The application lock is device-resident by design (FR-AUTH-009: the lock secret
is never transmitted), so it has no backend component.

---

## Features implemented

### Authentication

- Registration with unique email and a minimum eight-character password
- Duplicate email rejected with guidance to sign in instead
- Passwords stored only as salted bcrypt hashes, never in recoverable form
- Password hash excluded from every API response by schema design
- Login failures indistinguishable between an unknown email and a wrong
  password — identical body, status code and content length
- Rate limiting: five failed attempts per email within fifteen minutes, then
  refused for the remainder of the window; counter clears on success
- JWT issued on both registration and login
- Reusable authentication dependency protecting any endpoint in one line

### Onboarding

- Consent acknowledgement recorded with document version and timestamp
- Current consent version served to the client so it can detect a version change
  and request re-acknowledgement
- Focus areas recorded as neutral codes, zero or more per account
- Distress baseline captured as an integer 0–10 with timestamp, range enforced
- Written descriptions for all eleven distress values served from the clinical
  content set
- Weekly entry goal restricted to 2, 3, 5 or 7, defaulting to 3
- Onboarding status endpoint reporting which steps remain, supporting resumption
  of an interrupted sequence

---

## API endpoints

Authenticated endpoints require an `Authorization: Bearer <token>` header.

| Method | Path | Auth | Description |
| --- | --- | --- | --- |
| GET | `/health` | No | Liveness check |
| POST | `/auth/signup` | No | Create an account; returns a token |
| POST | `/auth/login` | No | Authenticate; returns a token |
| GET | `/auth/me` | Yes | Current account details |
| GET | `/onboarding/status` | Yes | Onboarding progress and current consent version |
| GET | `/onboarding/distress-scale` | No | Written descriptions for values 0–10 |
| POST | `/onboarding/consent` | Yes | Record consent acknowledgement |
| PUT | `/onboarding/focus-areas` | Yes | Replace focus area selections |
| POST | `/onboarding/distress-baseline` | Yes | Record distress baseline |
| PUT | `/onboarding/goal` | Yes | Set weekly entry goal |

Full request and response schemas are generated automatically at `/docs`.

---

## Database

| Table | Purpose |
| --- | --- |
| `accounts` | Credentials, onboarding responses, preferences |
| `focus_areas` | Focus area selections, one row per selection |
| `alembic_version` | Current migration state |

UUID primary keys are used throughout rather than sequential integers, so record
identifiers cannot be enumerated or used to infer user counts — a relevant
consideration for an application holding mental health data.

### Migrations

```bash
# After changing a model, generate a migration
alembic revision --autogenerate -m "description of the change"

# Review the generated file in alembic/versions/, then apply it
alembic upgrade head

# Roll back the most recent migration
alembic downgrade -1
```

Always read a generated migration before applying it. Autogenerate is reliable
but not infallible, and a migration that drops a table is not recoverable.

---

## Known limitations

Recorded in full, with reasoning, in [DEVELOPMENT-LOG.md](DEVELOPMENT-LOG.md)
section 6.

- **No refresh token rotation.** A single seven-day access token is issued.
  A compromised token stays valid until expiry, and logout is client-side only.
- **Rate limiting is per-process.** Failure counts live in application memory and
  reset on restart. A production deployment requires a shared store.
- **No email delivery.** Email verification, password reset, and new-device
  notification are consequently unimplemented.
- **Distress scale wording is provisional** and requires clinical review before
  demonstration.
- **No automated tests.** All verification to date has been manual, through
  `/docs` and Postman.

---

## Security notes

- `.env` is git-ignored. Database credentials and the JWT secret must never be
  committed.
- The Supabase Data API is disabled for this project. The database is reachable
  only through this backend, not directly from any client.
- Access tokens are bearer credentials and should be treated with the same care
  as passwords.
