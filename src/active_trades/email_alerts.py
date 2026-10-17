from __future__ import annotations

import os
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any


class EmailConfigError(ValueError):
    """Email alert environment variables are missing or invalid."""


@dataclass(frozen=True)
class EmailSettings:
    """Shared SES SMTP settings, the same variables SFP alerts use."""

    host: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    sender: str
    recipients: tuple[str, ...]
    sender_name: str = ""
    port: int = 587
    timeout_seconds: float = 15

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> EmailSettings:
        values = os.environ if environ is None else environ

        def required(*names: str) -> str:
            for name in names:
                value = (values.get(name) or "").strip()
                if value:
                    if "\r" in value or "\n" in value:
                        raise EmailConfigError(f"{name} must be a single line.")
                    return value
            raise EmailConfigError(f"{' or '.join(names)} must be set for email alerts.")

        raw_port = (values.get("SES_SMTP_PORT") or "").strip() or "587"
        try:
            port = int(raw_port)
        except ValueError:
            raise EmailConfigError("SES_SMTP_PORT must be an integer.") from None
        if not 1 <= port <= 65535:
            raise EmailConfigError("SES_SMTP_PORT must be between 1 and 65535.")
        recipients = tuple(
            part.strip()
            for part in required("ALERT_EMAIL_TO", "EMAIL_TO").replace(";", ",").split(",")
            if part.strip()
        )
        return cls(
            host=required("SES_SMTP_HOST"),
            username=required("SES_SMTP_USER", "SES_IAM_USER"),
            password=required("SES_SMTP_PASS"),
            sender=required("EMAIL_FROM"),
            recipients=recipients,
            sender_name=(values.get("EMAIL_FROM_NAME") or "").strip(),
            port=port,
        )


class EmailSender:
    """Send one plain-text message per call over an encrypted SMTP connection."""

    def __init__(
        self,
        settings: EmailSettings,
        *,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        smtp_ssl_factory: Callable[..., Any] = smtplib.SMTP_SSL,
    ) -> None:
        self.settings = settings
        self._smtp_factory = smtp_factory
        self._smtp_ssl_factory = smtp_ssl_factory

    def send(self, subject: str, body: str) -> None:
        settings = self.settings
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr((settings.sender_name, settings.sender))
        message["To"] = ", ".join(settings.recipients)
        message["Message-ID"] = make_msgid()
        message.set_content(body)
        context = ssl.create_default_context()
        if settings.port in (465, 2465):
            server = self._smtp_ssl_factory(
                settings.host, settings.port, timeout=settings.timeout_seconds, context=context
            )
        else:
            server = self._smtp_factory(settings.host, settings.port, timeout=settings.timeout_seconds)
        with server:
            if settings.port not in (465, 2465):
                server.starttls(context=context)
            server.login(settings.username, settings.password)
            server.send_message(message)
