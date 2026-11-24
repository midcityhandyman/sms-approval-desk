# sms-approval-desk

Automated systems draft texts to customers faster than anyone can proofread them, and a wrong text cannot be unsent.
This is the small desk in between: nothing goes out until a staff member has approved the exact wording on their phone.

## What it does

- **Holds drafted texts** in SQLite. Each draft has a recipient, a purpose and a body, and starts as `pending`.
- **Lets staff decide from a phone.** The frontend is a single mobile first page meant to open as a Telegram Mini App from a bot button. Each card has three actions: Approve, Edit, Reject.
- **Treats an edit as a new version.** Editing never changes text in place. It appends version N+1 and puts the draft back to `pending`, so edited wording always gets a fresh approval. Old versions stay in the history.
- **Ties every decision to a version.** Approve and Reject name the version the reviewer was looking at. If someone edited the draft in the meantime the server answers `409 stale_version` instead of approving text the reviewer never read.
- **Verifies who is asking.** Every API call carries Telegram's signed init data. The server recomputes the HMAC-SHA256 signature with the bot token, checks freshness, then checks the user against a staff allowlist. There are no passwords and no sessions.
- **Sends through a mock.** Approval hands the text to a sender interface. The only implementation is `MockSender`, which records what it was asked to send and can simulate a carrier failure. Failed sends can be retried without a second approval.
- **Will not send twice.** Dispatch claims the draft with a conditional update, checks the outbox for an earlier delivery, and a partial unique index allows at most one `sent` row per draft.
- **Optional second approver rule.** With `REQUIRE_SECOND_APPROVER=true` the person who wrote a version cannot approve it.

## Architecture

```mermaid
flowchart LR
    U[Upstream drafter<br/>bot or script] -->|POST /api/drafts| API
    subgraph Phone
      TG[Telegram client] -->|opens Mini App with signed initData| SPA[Single page frontend<br/>app/static]
    end
    SPA -->|Authorization: tma initData| API[FastAPI<br/>app/main.py]
    API --> AUTH[telegram_auth.py<br/>HMAC-SHA256 check, freshness, staff list]
    API --> SVC[service.py<br/>versions and state machine]
    SVC --> DB[(SQLite<br/>drafts, draft_versions, events, outbox)]
    SVC -->|approved version only| SND[sender.py<br/>MockSender]
```

Draft states:

```mermaid
stateDiagram-v2
    [*] --> pending: drafted
    pending --> pending: edit (new version)
    pending --> rejected: reject
    rejected --> pending: edit (new version)
    pending --> approved: approve this version
    approved --> sending: claim
    sending --> sent: sender ok
    sending --> failed: sender error
    failed --> sending: retry
    failed --> pending: edit (new version)
    sent --> [*]
```

| File | Role |
| --- | --- |
| `app/telegram_auth.py` | Init data parsing, signature check, freshness check. Standard library only. |
| `app/service.py` | The approval rules. No HTTP, tested directly. |
| `app/main.py` | FastAPI routes, auth dependency, static frontend. |
| `app/db.py` | Schema and transaction helper for SQLite. |
| `app/sender.py` | `Sender` protocol and `MockSender`. |
| `app/sms.py` | Phone format check and segment estimate (GSM-7 or UCS-2). |
| `app/config.py` | Settings from environment variables, with unsafe combinations refused. |
| `app/seed.py` | Five synthetic drafts. |
| `app/static/` | `index.html`, `app.js`, `styles.css`. No build step. |

### How the signature check works

Telegram gives a Mini App a query string that includes a `hash` field. The server never trusts the other fields until it has recomputed that hash:

```
secret_key        = HMAC_SHA256(key="WebAppData", msg=bot_token)
data_check_string = every field except hash, as key=value, sorted, joined by newlines
expected          = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))
```

The comparison is constant time. The signature is checked first, then `auth_date` (too old or dated in the future is refused), then the `user` JSON. Repeated keys are refused because they make the check string ambiguous. Each failure has its own stable error code (`bad_signature`, `expired`, `missing_hash` and so on).

## Run it

Needs Python 3.10 or newer. No keys, no network.

```bash
pip install -r requirements.txt
python -m app.seed
uvicorn app.main:create_app --factory
```

Open http://127.0.0.1:8000 and narrow the window to phone width. Outside Telegram there is no init data, so in `APP_ENV=dev` the page asks `/api/dev/init-data` to sign some for a fake reviewer using the built in fake bot token. The signed string then goes through the same verification as real Telegram data on every request. Tap the name in the top right corner to switch between the two dev reviewers. That endpoint returns 404 when `APP_ENV=prod`.

Configuration is by environment variable. See `.env.example`.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

106 tests. They use a temp SQLite file and a fake bot token and make no network calls.

- `tests/test_telegram_auth.py`: a pinned hash vector, an independent recomputation with `hashlib`, tampered fields, a token from another bot, missing or empty hash, repeated keys, expired and future dated data, malformed user JSON.
- `tests/test_service.py`: versioning, stale decisions, reject and reopen, failed send and retry, the double delivery guards, the second approver rule.
- `tests/test_api.py`: every route refuses unsigned requests, the staff allowlist, the full edit then approve flow over HTTP, the dev helper being absent in prod.
- `tests/test_sms_and_config.py`: segment counting, phone format, unsafe configuration being refused.

CI (`.github/workflows/ci.yml`) runs the tests and the demo on Python 3.10 and 3.12.

## Demo

```bash
python demo/run_demo.py
```

The demo starts the real app in process against a throwaway database and plays two fake reviewers through the whole story: unsigned and forged requests being refused, an approval, an edit that becomes version 2, a stale approval being refused, the second approver rule, a rejection, a carrier failure with a retry, and a double tap that does not send twice. The captured output is in [`demo/output.txt`](demo/output.txt). Excerpt:

```
== 4. Robin edits draft 3. The edit becomes version 2 and needs approval
   200 draft 3 -> pending, version 2
   Texts sent so far: 1 (the edit sent nothing)

== 5. Sky still has version 1 on screen and taps Approve
   409 stale_version: You were looking at version 1 but the draft is now at version 2. Reload and review the latest text.

== 6. Robin tries to approve the edit Robin just wrote
   403 second_approver_required: You wrote this version, so someone else has to approve it

== 7. Sky reloads, reads version 2 and approves it
   200 draft 3 -> sent, version 2
```

All names, phone numbers and messages in the seed data, tests and demo are invented. Phone numbers use the 555-01xx range reserved for fiction.

## API

All routes except `/api/health` and the dev helper need `Authorization: tma <initData>`.

| Method and path | Purpose |
| --- | --- |
| `GET /api/health` | Liveness. |
| `GET /api/me` | The signed in reviewer and counts per status. |
| `GET /api/drafts?status=pending` | List drafts with their current text. |
| `POST /api/drafts` | Create a draft (`recipient_name`, `recipient_phone`, `body`, `purpose`). |
| `GET /api/drafts/{id}` | One draft with all versions and the event history. |
| `POST /api/drafts/{id}/edit` | `{body, base_version}`. Appends a version, back to pending. |
| `POST /api/drafts/{id}/approve` | `{version}`. Approves that version and sends it. |
| `POST /api/drafts/{id}/reject` | `{version, reason}`. |
| `POST /api/drafts/{id}/retry` | Retry a failed send. |
| `GET /api/outbox` | Every send attempt handed to the sender. |
| `GET /api/dev/init-data?user=1001` | Dev only. Signs init data for a fake reviewer. |

Errors look like `{"error": {"code": "stale_version", "message": "..."}}`.

## Where this pattern runs

This is drawn from systems that run two small service companies day to day, where software drafts routine customer texts (reminders, follow ups, running late notices) and a person on the team approves each one from their phone before it goes out. The code here is a clean rewrite of the pattern with synthetic data. It shares no code, data or configuration with those systems.

## Limitations

- **No real SMS provider.** Only `MockSender` exists, and configuration refuses any other provider name. Wiring a carrier means writing one class with a `send(to_phone, body)` method, plus delivery receipts, which are not modelled at all.
- **Never run inside real Telegram here.** The signature check follows Telegram's documented algorithm and is tested with a fake token and vectors computed independently in the tests. It has not been exercised against init data produced by a live Telegram client. The newer Ed25519 `signature` field for third party validation is not checked (it is only treated as one more signed field).
- **No bot.** There is no Telegram bot that posts the "open desk" button or notifies staff about new drafts. You would point a bot's Mini App URL at this server yourself.
- **No machine credential for upstream drafters.** `POST /api/drafts` uses the same staff auth as everything else. A real deployment would add a separate service credential for the system that writes drafts.
- **Init data is replayable until it expires.** Anyone who captures a valid init data string can use it for `INIT_DATA_MAX_AGE_SECONDS` (one hour by default). That is how the Telegram scheme works. Serve over HTTPS and keep the window short.
- **A crash mid send needs a human.** If the process dies after the sender was called but before the result was recorded, the draft stays in `sending`. Nothing sweeps it, on purpose, because retrying blindly could send twice.
- **Single node.** SQLite with one writer at a time is fine for a handful of reviewers and not for more. No migrations tool, just `CREATE TABLE IF NOT EXISTS`.
- **No scheduling, quiet hours, opt out handling or rate limits.** Those belong in front of a real sender and are not here.
- **Segment counts are an estimate** for the reviewer, not a billing figure.
- **The frontend is untested by automation.** It was checked by hand in a browser at phone width. The tests cover the backend and that the static files are served.
- **Local runs were on Python 3.12 only.** CI is configured for 3.10 and 3.12 but has not run, since this repository has not been pushed anywhere.

License: MIT, see the LICENSE file.
