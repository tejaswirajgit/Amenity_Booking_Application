from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

load_dotenv()


class EmailServiceError(RuntimeError):
	"""Raised when email configuration is invalid or delivery fails."""


def _get_required_env(name: str) -> str:
	value = os.getenv(name, "").strip().strip('"').strip("'")
	if not value:
		raise EmailServiceError(f"Missing required environment variable: {name}")
	return value


@dataclass(frozen=True)
class EmailSettings:
	smtp_host: str
	smtp_port: int
	smtp_user: str
	smtp_password: str
	email_from: str
	email_from_name: str


@lru_cache
def get_email_settings() -> EmailSettings:
	port_raw = _get_required_env("SMTP_PORT")
	try:
		smtp_port = int(port_raw)
	except ValueError as exc:
		raise EmailServiceError("SMTP_PORT must be a valid integer.") from exc

	return EmailSettings(
		smtp_host=_get_required_env("SMTP_HOST"),
		smtp_port=smtp_port,
		smtp_user=_get_required_env("SMTP_USER"),
		smtp_password=_get_required_env("SMTP_PASSWORD"),
		email_from=_get_required_env("EMAIL_FROM"),
		email_from_name=os.getenv("EMAIL_FROM_NAME", "Mail api").strip() or "Mail api",
	)


@lru_cache
def _get_mail_config() -> ConnectionConfig:
	settings = get_email_settings()
	return ConnectionConfig(
		MAIL_USERNAME=settings.smtp_user,
		MAIL_PASSWORD=settings.smtp_password,
		MAIL_FROM=settings.email_from,
		MAIL_FROM_NAME=settings.email_from_name,
		MAIL_PORT=settings.smtp_port,
		MAIL_SERVER=settings.smtp_host,
		MAIL_STARTTLS=True,
		MAIL_SSL_TLS=False,
		USE_CREDENTIALS=True,
		VALIDATE_CERTS=True,
	)


@lru_cache
def _get_mail_client() -> FastMail:
	return FastMail(_get_mail_config())


async def send_email(to_email: str, subject: str, html_body: str) -> None:
	"""Send an HTML email asynchronously via SMTP (Gmail-compatible)."""
	to_email_clean = to_email.strip()
	subject_clean = subject.strip()
	if not to_email_clean:
		raise EmailServiceError("to_email is required.")
	if not subject_clean:
		raise EmailServiceError("subject is required.")
	if not html_body.strip():
		raise EmailServiceError("html_body is required.")

	message = MessageSchema(
		subject=subject_clean,
		recipients=[to_email_clean],
		body=html_body,
		subtype=MessageType.html,
	)

	try:
		# Add timeout to prevent hanging
		await asyncio.wait_for(_get_mail_client().send_message(message), timeout=30.0)
	except asyncio.TimeoutError as exc:
		raise EmailServiceError(f"Email delivery timeout (30s): SMTP connection failed. Check SMTP_HOST={get_email_settings().smtp_host} and credentials.") from exc
	except Exception as exc:
		raise EmailServiceError(f"Email delivery failed: {exc}") from exc
