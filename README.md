# Emotional Support Companion — Backend

REST API for a mobile journalling application providing structured emotional support through guided journal entries, crisis-aware safeguards, and an AI reflection layer.

Implements **SRS-ESC-001 v0.9**. Detailed progress, decisions, and deviations are recorded in `DEVELOPMENT-LOG.md`.

---

## Stack

- Python 3.13 with FastAPI and Uvicorn
- PostgreSQL 17, hosted on Supabase (`ap-south-1`)
- SQLAlchemy ORM with Alembic migrations
- bcrypt password hashing, JWT (HS256) sessions

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

Create a `.env` file in the project root. It is git-ignored and must **never** be committed.

```env
DATABASE_URL=postgresql://user:password@host:5432/database
JWT_SECRET=<generate with: python -c "import secrets; print(secrets.token_urlsafe(32))">
RESEND_API_KEY=re_...
```

Optional, with defaults shown:

```env
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=30
CONSENT_VERSION=v1
EMAIL_FROM=onboarding@resend.dev
APP_BASE_URL=http://127.0.0.1:8000
```

---

## Project Structure

```
app/
├── content/     Clinical content sets (JSON) — reviewable without reading code
├── core/        Config, database engine, security, auth dependency, verification, rate limiting
├── models/      SQLAlchemy models — what the database stores
├── routers/     API endpoints, one file per feature area
├── schemas/     Pydantic schemas — what the API accepts and returns
└── main.py      Application entry point

alembic/versions/   Migration scripts, applied in sequence
```

> Models and schemas are deliberately separate: `password_hash` exists in the `Account` model but appears in no response schema, so it cannot leak through the API.

---

## Module Status

| # | Module | SRS Section | Backend | Frontend |
|---|--------|:---:|---|---|
| 1 | Authentication (account layer) | 4.2 | ✅ Complete | ✅ Complete |
| 2 | Application lock (device layer) | 4.2 | — Not applicable | ⬜ Not started |
| 3 | Onboarding | 4.3 | ✅ Complete | ✅ Complete |
| 4 | Dashboard and engagement | 4.4 | ⬜ Not started | ⬜ Not started |
| 5 | Entry engine | 4.5 | ⬜ Not started | ⬜ Not started |
| 6 | Journal types | 4.6 | ⬜ Not started | ⬜ Not started |
| 7 | Selection libraries | 4.7 | ⬜ Not started | ⬜ Not started |
| 8 | Crisis detection and response | 4.8 | 🟡 Partial (resource list only) | 🟡 Partial |
| 9 | AI reflection module | 4.9 | ⬜ Not started | ⬜ Not started |
| 10 | Insights | 4.10 | ⬜ Not started | ⬜ Not started |
| 11 | Learning library | 4.11 | ⬜ Not started | ⬜ Not started |
| 12 | User data control | 4.12 | ⬜ Not started | ⬜ Not started |

> The application lock is device-resident by design (**FR-AUTH-009**: the lock secret is never transmitted), so it has no backend component.

---

## Features Implemented

### Authentication

- Signup is **email-first** rather than email-and-password together. The client submits only an email; the account is created in a pending state (no password set) and a 6-digit verification code is emailed. The code is exchanged for a short-lived setup token, which is then used to set the password and receive the account's first session.
- Signup on an already fully registered email is rejected immediately with a `409` and guidance to sign in instead. A signup on a still-pending account is treated as a resend and issues a fresh code.
- Passwords stored only as salted bcrypt hashes, never in recoverable form.
- Password hash excluded from every API response by schema design.
- Login failures are indistinguishable between an unknown email, a wrong password, and a pending (password-not-yet-set) account — identical body, status code, and content length.
- Rate limiting: five failed attempts per email within fifteen minutes, applied to both login and code verification, then refused for the remainder of the window; counter clears on success.
- Refresh token rotation with family revocation: each refresh token works once, and presenting a consumed token revokes every token descended from that sign-in.
- Password reset is **code-based** rather than link-based: the client requests a code by email, submits it in-app, and sets a new password without leaving the app. All sessions are revoked on a successful reset.
- New-device notification via fingerprint, revealing nothing about the app in the notification itself.
- Reusable authentication dependency protecting any endpoint in one line.

### Onboarding

- Consent acknowledgement recorded with document version and timestamp.
- Current consent version served to the client so it can detect a version change and request re-acknowledgement.
- Focus areas recorded as neutral codes, zero or more per account.
- Distress baseline captured as an integer 0–10 with timestamp, range enforced.
- Written descriptions for all eleven distress values served from the clinical content set.
- Weekly entry goal restricted to 2, 3, 5, or 7, defaulting to 3.
- Onboarding status endpoint reporting which steps remain, supporting resumption of an interrupted sequence.

---

## API Endpoints

Authenticated endpoints require an `Authorization: Bearer <token>` header.

| Method | Path | Auth | Description |
|---|---|:---:|---|
| GET | `/health` | No | Liveness check |
| POST | `/auth/signup` | No | Begin signup with an email; sends a verification code, or returns `409` if already registered |
| POST | `/auth/verify-signup-code` | No | Redeem a signup code; returns a setup token |
| POST | `/auth/set-password` | No | Redeem a setup token, set a password, receive the first session |
| POST | `/auth/login` | No | Authenticate; returns a token pair |
| POST | `/auth/refresh` | No | Rotate a refresh token |
| GET | `/auth/me` | Yes | Current account details |
| GET | `/auth/verify-email` | No | Legacy link-based email verification; not called by the current flow |
| POST | `/auth/forgot-password` | No | Begin a password reset; sends a reset code |
| POST | `/auth/verify-reset-code` | No | Redeem a reset code; returns a reset token |
| POST | `/auth/reset-password-code` | No | Redeem a reset token, set a new password |
| POST | `/auth/reset-password` | No | Legacy link-based password reset; not called by the current flow |
| GET | `/onboarding/status` | Yes | Onboarding progress and current consent version |
| GET | `/onboarding/distress-scale` | No | Written descriptions for values 0–10 |
| GET | `/onboarding/focus-areas/options` | No | Valid focus area codes |
| POST | `/onboarding/consent` | Yes | Record consent acknowledgement |
| PUT | `/onboarding/focus-areas` | Yes | Replace focus area selections |
| POST | `/onboarding/distress-baseline` | Yes | Record distress baseline |
| PUT | `/onboarding/goal` | Yes | Set weekly entry goal |
| GET | `/crisis/resources` | No | Crisis helpline resource list |

> Full request and response schemas are generated automatically at `/docs`.

---

## Database

| Table | Purpose |
|---|---|
| `accounts` | Credentials (nullable until signup completes), onboarding responses, preferences |
| `focus_areas` | Focus area selections, one row per selection |
| `refresh_tokens` | Hashed refresh tokens with family and revocation state |
| `verification_tokens` | Hashed single-use tokens and 6-digit codes for signup verification, setup, password reset, and reset codes, distinguished by purpose |
| `known_devices` | Device fingerprints seen per account |
| `alembic_version` | Current migration state |

- UUID primary keys are used throughout rather than sequential integers, so record identifiers cannot be enumerated or used to infer user counts — a relevant consideration for an application holding mental health data.
- `password_hash` on `accounts` is nullable. A row with no password represents a pending signup: the email has been confirmed as reachable, or a code has been redeemed, but a password has not yet been set. An account only becomes fully usable — able to log in, able to request a password reset — once `password_hash` is set.

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

## Known Limitations

Recorded in full, with reasoning, in `DEVELOPMENT-LOG.md`.

- **No email delivery beyond the account holder's own address.** Without a verified sending domain, Resend delivers only to the address the Resend account itself was created with. Multi-user testing and production use both require domain verification.
- **Rate limiting is per-process.** Failure counts live in application memory and reset on restart. A production deployment requires a shared store.
- **Distress scale wording is provisional** and requires clinical review before demonstration.
- **Elevated distress (FR-ONB-007) has no support response yet.** Selecting 9 or 10 on the distress scale currently saves like any other value. This remains the highest-priority safety gap.
- Legacy link-based `verify-email` and `reset-password` endpoints are unused under the current flow and are candidates for removal.
- **Signup enumeration-safety trade-off:** unlike login and forgot-password, `/auth/signup` now reveals whether an email is already fully registered, via an immediate `409`. This was a deliberate product decision, made to improve the signup experience, not an oversight.
- No automated tests run in CI. All verification to date has been manual, through `/docs` and a Postman collection.

---

## Security Notes

- `.env` is git-ignored. Database credentials and the JWT secret must never be committed.
- The Supabase Data API is disabled for this project. The database is reachable only through this backend, not directly from any client.
- Access tokens are bearer credentials and should be treated with the same care as passwords.
- ⚠️ A temporary debug print statement in `app/core/verification.py` (inside `create_code`) currently logs generated codes to the server console, as a workaround for the email delivery limitation above. **It must be removed before any deployment beyond local development**, since logging a live authentication code is a genuine credential leak once real users are involved.
UPD