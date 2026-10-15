"""Notifications: Notion comments with user mentions, and optional SMTP email.

Email is optional. It is enabled only when SMTP_HOST, SMTP_USER and SMTP_PASS
are all set (SMTP_PORT defaults to 587, SMTP_FROM to SMTP_USER). A failed send
is logged and reported as False; it never stops a job. Secrets are never logged.
"""

from __future__ import annotations

import logging
import os
import smtplib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from hq import notion as nt

log = logging.getLogger("hq.notify")

Mailer = Callable[[str, str, str], bool]  # (to, subject, body) -> sent?
SMTP_TIMEOUT = 30


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    sender: str


def smtp_settings(env: Mapping[str, str] | None = None) -> SmtpSettings | None:
    """SMTP settings from the environment, or None when email is not configured."""
    source = os.environ if env is None else env
    host = (source.get("SMTP_HOST") or "").strip()
    user = (source.get("SMTP_USER") or "").strip()
    password = source.get("SMTP_PASS") or ""
    if not (host and user and password):
        return None
    port_text = (source.get("SMTP_PORT") or "").strip()
    try:
        port = int(port_text) if port_text else 587
    except ValueError:
        log.warning("SMTP_PORT is not a number; using 587")
        port = 587
    sender = (source.get("SMTP_FROM") or "").strip() or user
    return SmtpSettings(host=host, port=port, user=user, password=password, sender=sender)


def send_email(settings: SmtpSettings, to: str, subject: str, body: str) -> None:
    """Send one plain-text email over STARTTLS. Raises on failure."""
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.host, settings.port, timeout=SMTP_TIMEOUT) as server:
        server.starttls()
        server.login(settings.user, settings.password)
        server.send_message(message)


def make_mailer(settings: SmtpSettings | None) -> Mailer | None:
    """A ``(to, subject, body) -> bool`` sender, or None when SMTP is not configured."""
    if settings is None:
        return None

    def _send(to: str, subject: str, body: str) -> bool:
        try:
            send_email(settings, to, subject, body)
            return True
        except (smtplib.SMTPException, OSError) as exc:
            log.warning("Email to a participant failed: %s", type(exc).__name__)
            return False

    return _send


def comment_rich_text(text: str, mention_ids: Iterable[str] = (), trailing: str = "") -> list[dict[str, Any]]:
    """Rich text for a Notion comment: ``text``, then one @mention per user id.

    Duplicate user ids are mentioned once, in first-seen order.
    """
    items: list[dict[str, Any]] = nt.rich_text(text)
    seen: set[str] = set()
    for user_id in mention_ids:
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        items.append(nt.mention_user(user_id))
        items.extend(nt.rich_text(" "))
    if trailing:
        items.extend(nt.rich_text(trailing))
    return items
