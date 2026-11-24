"""Outbound SMS senders.

Only a mock is shipped. It never opens a socket. A real provider would be one
more class with the same ``send`` method.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider: str
    provider_message_id: str = ""
    error: str = ""


class Sender(Protocol):
    name: str

    def send(self, to_phone: str, body: str) -> SendResult: ...


class MockSender:
    """Pretends to send a text and remembers what it was asked to send.

    Numbers ending in one of ``fail_suffixes`` fail, which lets tests and the
    demo exercise the failure and retry path. ``clear_failures`` simulates the
    provider recovering.
    """

    name = "mock"

    def __init__(self, fail_suffixes: tuple[str, ...] = ("0199",)) -> None:
        self.fail_suffixes = tuple(fail_suffixes)
        self.sent: list[dict] = []
        self.attempts = 0
        self._lock = threading.Lock()

    def clear_failures(self) -> None:
        self.fail_suffixes = ()

    def send(self, to_phone: str, body: str) -> SendResult:
        with self._lock:
            self.attempts += 1
            attempt = self.attempts
            if self.fail_suffixes and to_phone.endswith(self.fail_suffixes):
                return SendResult(
                    ok=False, provider=self.name, error="mock carrier rejected the number"
                )
            digest = hashlib.sha256(f"{attempt}|{to_phone}|{body}".encode("utf-8")).hexdigest()
            message_id = f"mock-{attempt:04d}-{digest[:8]}"
            self.sent.append({"to": to_phone, "body": body, "id": message_id})
            return SendResult(ok=True, provider=self.name, provider_message_id=message_id)


def build_sender(provider: str) -> Sender:
    if provider == "mock":
        return MockSender()
    raise ValueError(f"Unknown SMS provider {provider!r}. Only 'mock' is implemented.")
