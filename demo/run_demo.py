"""Offline walk through of the approval desk.

Runs the real FastAPI app in process against a throwaway SQLite file, signs
Telegram init data with a fake bot token, and plays two fake reviewers.
No network, no keys.

    python demo/run_demo.py
"""

from __future__ import annotations

import sys
import tempfile
import warnings
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Keep the captured output free of library deprecation notices.
warnings.simplefilter("ignore")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import connect, init_db  # noqa: E402
from app.main import create_app  # noqa: E402
from app.seed import seed  # noqa: E402
from app.sender import MockSender  # noqa: E402
from app.telegram_auth import make_init_data  # noqa: E402

FAKE_TOKEN = "424242424:DEMO-fake-token-not-issued-by-telegram"
ROBIN = (2001, "Robin", "Reviewer")
SKY = (2002, "Sky", "Checker")
OUTSIDER = (3003, "Sam", "Outsider")


def header(user: tuple[int, str, str], token: str = FAKE_TOKEN) -> dict:
    return {"Authorization": "tma " + make_init_data(token, *user)}


def step(title: str) -> None:
    print(f"\n== {title}")


def show(response, note: str = "") -> dict:
    data = response.json()
    if "error" in data:
        line = f"{response.status_code} {data['error']['code']}: {data['error']['message']}"
    elif "status" in data and "current_version" in data:
        line = (
            f"{response.status_code} draft {data['id']} -> {data['status']},"
            f" version {data['current_version']}"
        )
    else:
        line = f"{response.status_code} ok"
    print(f"   {line}{'  (' + note + ')' if note else ''}")
    return data


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        settings = Settings(
            db_path=str(Path(folder) / "demo.sqlite3"),
            bot_token=FAKE_TOKEN,
            staff_ids=frozenset({ROBIN[0], SKY[0]}),
            require_second_approver=True,
        ).validate()
        conn = connect(settings.db_path)
        init_db(conn)
        added = seed(conn, settings)
        conn.close()
        sender = MockSender()

        print("SMS Approval Desk demo (offline, fake bot token, mock sender)")
        print(f"Seeded {added} synthetic drafts. Staff allowlist: Robin (2001), Sky (2002).")
        print("Second approver rule: ON (you cannot approve text you wrote).")

        with TestClient(create_app(settings, sender)) as client:
            step("1. Requests without valid Telegram init data are refused")
            show(client.get("/api/drafts"), "no Authorization header")
            fields = dict(parse_qsl(header(ROBIN)["Authorization"][4:]))
            fields["user"] = fields["user"].replace("2001", "2002")
            show(
                client.get("/api/drafts", headers={"Authorization": "tma " + urlencode(fields)}),
                "user id altered after signing",
            )
            show(client.get("/api/drafts", headers=header(ROBIN, "999:SOME-OTHER-fake-token")), "signed by a different bot")
            show(client.get("/api/drafts", headers=header(OUTSIDER)), "valid signature, not on the staff list")

            step("2. Robin opens the desk")
            me = client.get("/api/me", headers=header(ROBIN)).json()
            print(f"   Signed in as {me['name']} (Telegram id {me['id']})")
            for draft in client.get("/api/drafts?status=pending", headers=header(ROBIN)).json()["drafts"]:
                seg = draft["segments"]
                print(
                    f"   #{draft['id']} v{draft['current_version']} to {draft['recipient_name']}"
                    f" {draft['recipient_phone']} [{draft['purpose']}]"
                    f" {len(draft['body'])} chars, {seg['segments']} segment(s)"
                )

            step("3. Robin approves draft 1 as written")
            show(client.post("/api/drafts/1/approve", json={"version": 1}, headers=header(ROBIN)))

            step("4. Robin edits draft 3. The edit becomes version 2 and needs approval")
            new_text = (
                "Hi Casey, our crew is running about 20 minutes behind today."
                " New arrival window is 2:20 to 2:40. Sorry for the wait."
            )
            show(client.post("/api/drafts/3/edit", json={"body": new_text, "base_version": 1}, headers=header(ROBIN)))
            print(f"   Texts sent so far: {len(sender.sent)} (the edit sent nothing)")

            step("5. Sky still has version 1 on screen and taps Approve")
            show(client.post("/api/drafts/3/approve", json={"version": 1}, headers=header(SKY)))

            step("6. Robin tries to approve the edit Robin just wrote")
            show(client.post("/api/drafts/3/approve", json={"version": 2}, headers=header(ROBIN)))

            step("7. Sky reloads, reads version 2 and approves it")
            show(client.post("/api/drafts/3/approve", json={"version": 2}, headers=header(SKY)))

            step("8. Sky rejects draft 4 with a reason")
            show(
                client.post(
                    "/api/drafts/4/reject",
                    json={"version": 1, "reason": "Job is not finished yet"},
                    headers=header(SKY),
                )
            )

            step("9. Sky approves draft 5 but the mock carrier refuses the number")
            show(client.post("/api/drafts/5/approve", json={"version": 1}, headers=header(SKY)))
            print("   (carrier recovers)")
            sender.clear_failures()
            show(client.post("/api/drafts/5/retry", headers=header(ROBIN)), "retry by Robin")

            step("10. A second tap on Approve cannot send a text twice")
            show(client.post("/api/drafts/1/approve", json={"version": 1}, headers=header(SKY)))

            step("History of draft 3")
            for event in client.get("/api/drafts/3", headers=header(ROBIN)).json()["events"]:
                note = f" ({event['note']})" if event["note"] else ""
                print(f"   v{event['version_no']} {event['action']:<11} by {event['actor_name']}{note}")

            step("Outbox (what the mock sender was handed)")
            for row in client.get("/api/outbox", headers=header(ROBIN)).json()["outbox"]:
                detail = row["provider_message_id"] or row["error"]
                print(
                    f"   draft {row['draft_id']} v{row['version_no']} to {row['to_phone']}:"
                    f" {row['status']} [{detail}]"
                )
                print(f"      \"{row['body']}\"")

            step("Final counts")
            counts = client.get("/api/me", headers=header(ROBIN)).json()["counts"]
            print("   " + ", ".join(f"{name}: {count}" for name, count in counts.items() if count))
            print(f"   Mock sender delivered {len(sender.sent)} texts in {sender.attempts} attempts.")


if __name__ == "__main__":
    main()
