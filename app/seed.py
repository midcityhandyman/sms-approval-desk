"""Synthetic drafts for local development and the demo.

Every name, number and message here is invented. Phone numbers use the
555-01xx range that is reserved for fiction.
"""

from __future__ import annotations

import sqlite3

from . import service
from .config import Settings, load_settings
from .db import connect, init_db

DRAFTER = service.Actor(id=None, name="drafting bot")

SAMPLE_DRAFTS = [
    {
        "recipient_name": "Alex Sample",
        "recipient_phone": "+15555550101",
        "purpose": "Appointment reminder",
        "body": "Hi Alex, this is a reminder that your service visit is booked for Tuesday"
        " between 9 and 11. Reply C to confirm or R to reschedule.",
    },
    {
        "recipient_name": "Jordan Example",
        "recipient_phone": "+15555550102",
        "purpose": "Quote follow up",
        "body": "Hi Jordan, following up on the quote we sent last week. Any questions"
        " we can answer? Happy to walk through it by phone.",
    },
    {
        "recipient_name": "Casey Placeholder",
        "recipient_phone": "+15555550103",
        "purpose": "Running late notice",
        "body": "Hi Casey, our crew is running about 20 mins behind today. New arrival"
        " window is 2:20 to 2:40. Sorry for the wait!!",
    },
    {
        "recipient_name": "Riley Testcase",
        "recipient_phone": "+15555550104",
        "purpose": "Review request",
        "body": "Thanks for choosing us, Riley. If you have a minute, a short review at"
        " https://example.com/review would mean a lot.",
    },
    {
        "recipient_name": "Morgan Demo",
        "recipient_phone": "+15555550199",
        "purpose": "Invoice reminder",
        "body": "Hi Morgan, a friendly reminder that invoice 1042 is due Friday. You can"
        " pay at https://example.com/pay or reply with questions.",
    },
]


def seed(conn: sqlite3.Connection, settings: Settings, force: bool = False) -> int:
    """Insert the sample drafts. Does nothing if the desk already has drafts."""
    existing = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
    if existing and not force:
        return 0
    for item in SAMPLE_DRAFTS:
        service.create_draft(conn, settings, source="drafting-bot", actor=DRAFTER, **item)
    return len(SAMPLE_DRAFTS)


def main() -> None:
    settings = load_settings()
    conn = connect(settings.db_path)
    init_db(conn)
    added = seed(conn, settings)
    print(f"Seeded {added} synthetic drafts into {settings.db_path}")
    conn.close()


if __name__ == "__main__":
    main()
