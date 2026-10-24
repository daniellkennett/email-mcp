"""Bulk send engine: compliance footer, MIME building, SMTP delivery, audit trail."""

import re
import smtplib
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape
from urllib.parse import urlencode

from email_mcp import audit
from email_mcp.config import Settings
from email_mcp.contacts import load_suppressions, valid_email
from email_mcp.templates import html_to_text
from email_mcp.tls import tls_context

SMTP_TIMEOUT_SECONDS = 30


class SendError(Exception):
    """The batch cannot be sent as requested."""


class RateLimitExceeded(Exception):
    """The batch would exceed MAX_MESSAGES_PER_HOUR."""


@dataclass
class Message:
    """One outgoing email."""

    to_email: str
    subject: str
    html: str
    text: str | None = None
    headers: dict[str, str] | None = None


@dataclass
class SendResult:
    """Outcome of a send_bulk run."""

    run_id: str
    total: int
    sent: int
    skipped: int
    failed: int
    dry_run: bool
    duration_seconds: float
    details: list[dict]        # {"email":..,"status":"sent"|"skipped"|"failed","reason":str|None}
    warnings: list[str] = field(default_factory=list)


CLOSING_BODY_RE = re.compile(r"</body\s*>", re.IGNORECASE)


def unsubscribe_url(settings: Settings, email: str) -> str | None:
    """Per-recipient unsubscribe URL, or None when no base URL is configured."""
    base = settings.unsubscribe_base_url
    if not base:
        return None
    query = urlencode({"email": email})
    if base.endswith(("?", "&")):
        return f"{base}{query}"
    joiner = "&" if "?" in base else "?"
    return f"{base}{joiner}{query}"


def apply_compliance(settings: Settings, html: str, email: str) -> tuple[str, dict[str, str]]:
    """Insert the unsubscribe footer before </body> and return List-Unsubscribe headers.

    Returns (html, {}) unchanged when no UNSUBSCRIBE_BASE_URL is configured.
    """
    url = unsubscribe_url(settings, email)
    if url is None:
        return html, {}
    footer = (
        '<div style="margin-top:32px;padding-top:12px;border-top:1px solid #ddd;'
        'font-size:12px;color:#666;">'
        "<p>You are receiving this because you are on a contact list for "
        f"{escape(settings.from_name)}. "
        f'<a href="{escape(url, quote=True)}">Unsubscribe</a> from future emails.</p>'
        "</div>"
    )
    # Match case-insensitively without lowering the whole document: lower() can change
    # length (U+0130 becomes two code points), which would shift the index mid-tag.
    matches = list(CLOSING_BODY_RE.finditer(html))
    if matches:
        index = matches[-1].start()
        body = html[:index] + footer + html[index:]
    else:
        body = html + footer
    headers = {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }
    return body, headers


def build_mime(settings: Settings, message: Message) -> EmailMessage:
    """Build a multipart/alternative message (plaintext + HTML)."""
    text = message.text or html_to_text(message.html)
    mime = EmailMessage()
    mime["From"] = formataddr((settings.from_name, settings.from_email))
    mime["To"] = message.to_email
    mime["Subject"] = message.subject
    if settings.reply_to:
        mime["Reply-To"] = settings.reply_to
    mime["Date"] = formatdate(localtime=False, usegmt=True)
    mime["Message-ID"] = make_msgid(domain=settings.from_email.rpartition("@")[2])
    mime.set_content(text)
    mime.add_alternative(message.html, subtype="html")
    for name, value in (message.headers or {}).items():
        del mime[name]
        mime[name] = value
    return mime


def prepare_message(settings: Settings, message: Message, email: str) -> Message:
    """Return a per-recipient copy with the compliance footer, headers and plaintext link."""
    html, compliance_headers = apply_compliance(settings, message.html, email)
    text = message.text
    url = unsubscribe_url(settings, email)
    if text and url:
        text = f"{text.rstrip()}\n\nUnsubscribe: {url}"
    headers = {**compliance_headers, **(message.headers or {})}
    return replace(message, to_email=email, html=html, text=text, headers=headers)


def check_hourly_allowance(settings: Settings, requested: int) -> None:
    """Raise RateLimitExceeded if the batch would exceed the trailing-hour cap."""
    used = audit.hourly_count(settings)
    if used + requested > settings.max_messages_per_hour:
        remaining = max(settings.max_messages_per_hour - used, 0)
        raise RateLimitExceeded(
            f"Hourly send limit reached: {used} sent in the last 60 minutes, "
            f"{remaining} remaining of {settings.max_messages_per_hour}, "
            f"but this batch needs {requested}."
        )


def is_connection_error(exc: Exception) -> bool:
    """True when the failure means the socket is unusable (not a per-recipient refusal).

    smtplib.SMTPException subclasses OSError, so the SMTP check must come first.
    """
    if isinstance(exc, smtplib.SMTPException):
        return isinstance(exc, (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError))
    return isinstance(exc, OSError)


def describe(exc: Exception) -> str:
    """Human-readable error text for an exception."""
    return str(exc) or type(exc).__name__


def open_smtp(settings: Settings) -> smtplib.SMTP:
    """Open and authenticate an SMTP connection (STARTTLS or implicit SSL)."""
    context = tls_context(settings)
    server: smtplib.SMTP
    if settings.smtp_starttls:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS)
    else:
        server = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT_SECONDS, context=context
        )
    try:
        if settings.smtp_starttls:
            server.starttls(context=context)
        server.login(settings.smtp_user, settings.smtp_password)
    except BaseException:
        server.close()
        raise
    return server


def connect_with_retry(settings: Settings) -> tuple[smtplib.SMTP | None, str | None]:
    """Connect, retrying once on connection-level failure. Returns (server, error)."""
    error: str | None = None
    for attempt in range(2):
        try:
            return open_smtp(settings), None
        except smtplib.SMTPAuthenticationError as exc:
            return None, f"SMTP authentication failed (code {exc.smtp_code})"
        except (smtplib.SMTPException, OSError) as exc:
            error = f"attempt {attempt + 1}: {describe(exc)}"
    return None, error


def close_smtp(server: smtplib.SMTP | None) -> None:
    """Quit and close an SMTP connection, ignoring errors from a dead socket."""
    if server is None:
        return
    with suppress(smtplib.SMTPException, OSError):
        server.quit()
    server.close()


def send_bulk(
    settings: Settings,
    messages: list[Message],
    *,
    dry_run: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> SendResult:
    """Send a batch with suppression, dedupe, compliance, rate limits and audit logging.

    progress(done, total) is called once per recipient as it is settled (1-based count).
    """
    started = time.monotonic()
    run_id = uuid.uuid4().hex[:12]
    total = len(messages)

    if total == 0:
        raise SendError("Nothing to send: the recipient list is empty.")
    if total > settings.max_recipients_per_run:
        raise SendError(
            f"Batch has {total} recipients, which exceeds "
            f"MAX_RECIPIENTS_PER_RUN={settings.max_recipients_per_run}."
        )
    warnings = [] if settings.unsubscribe_base_url else ["no_unsubscribe_url"]
    if dry_run:
        # A dry run must not raise on the hourly cap, but staying silent would let
        # an operator clear a dry run and then hit RateLimitExceeded for real.
        used = audit.hourly_count(settings)
        if used + total > settings.max_messages_per_hour:
            warnings.append(
                f"would_exceed_hourly_cap: {used} sent in the last hour plus {total} "
                f"requested exceeds MAX_MESSAGES_PER_HOUR={settings.max_messages_per_hour}"
            )
    else:
        # Sending marketing mail with no unsubscribe mechanism is not compliant, so this
        # is refused rather than warned about. A seed send to your own address can opt out
        # via ALLOW_SEND_WITHOUT_UNSUBSCRIBE.
        if not settings.unsubscribe_base_url and not settings.allow_send_without_unsubscribe:
            raise SendError(
                "Refusing a real send: UNSUBSCRIBE_BASE_URL is not set, so the message "
                "would carry no unsubscribe link and no List-Unsubscribe header. Set it to "
                "your unsubscribe page, or set ALLOW_SEND_WITHOUT_UNSUBSCRIBE=true for a "
                "test send to an address you control."
            )
        check_hourly_allowance(settings, total)
    suppressed = load_suppressions(settings)
    seen: set[str] = set()
    details: list[dict] = []
    queue: list[tuple[dict, Message]] = []
    done = 0

    def settle(detail: dict, subject: str) -> None:
        """Write the per-recipient audit record and report progress."""
        nonlocal done
        audit.log_event(settings, {"event": "send", "run_id": run_id, "dry_run": dry_run,
                                   "subject": subject, **detail})
        done += 1
        if progress is not None:
            with suppress(Exception):
                progress(done, total)

    # Pass 1: filter out invalid, duplicate and suppressed addresses.
    for message in messages:
        email = message.to_email.strip().lower()
        reason: str | None = None
        if not valid_email(email):
            reason = "invalid_email"
        elif email in seen:
            reason = "duplicate_in_batch"
        elif email in suppressed:
            reason = "suppressed"

        if reason is not None:
            detail = {"email": email, "status": "skipped", "reason": reason}
            details.append(detail)
            settle(detail, message.subject)
            continue

        seen.add(email)
        detail = {"email": email, "status": "pending", "reason": None}
        details.append(detail)
        queue.append((detail, prepare_message(settings, message, email)))

    # Pass 2: deliver the queue.
    if dry_run:
        # Build each message even though nothing is dispatched: assembling the MIME
        # is where malformed headers and bad addresses surface, so skipping it would
        # make "a dry run with no failures" tell the operator nothing.
        for detail, prepared in queue:
            try:
                build_mime(settings, prepared)
            except Exception as exc:
                detail.update(status="failed", reason=f"message build failed: {describe(exc)}")
            else:
                detail.update(status="sent", reason="dry_run")
            settle(detail, prepared.subject)
    elif queue:
        server, error = connect_with_retry(settings)
        try:
            for position, (detail, prepared) in enumerate(queue, start=1):
                if server is None:
                    detail.update(status="failed", reason=f"connection failed: {error}")
                else:
                    try:
                        server.send_message(build_mime(settings, prepared))
                        detail.update(status="sent", reason=None)
                    except Exception as exc:
                        detail.update(status="failed", reason=describe(exc))
                        if is_connection_error(exc):
                            close_smtp(server)
                            server, error = connect_with_retry(settings)
                settle(detail, prepared.subject)
                if (position % settings.batch_size == 0 and position < len(queue)
                        and settings.batch_pause_seconds > 0):
                    time.sleep(settings.batch_pause_seconds)
        finally:
            close_smtp(server)

    counts = {status: sum(1 for d in details if d["status"] == status)
              for status in ("sent", "skipped", "failed")}
    duration = round(time.monotonic() - started, 3)
    audit.log_event(settings, {
        "event": "run",
        "run_id": run_id,
        "dry_run": dry_run,
        "total": total,
        **counts,
        "duration_seconds": duration,
        "warnings": warnings,
    })
    return SendResult(
        run_id=run_id,
        total=total,
        sent=counts["sent"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        dry_run=dry_run,
        duration_seconds=duration,
        details=details,
        warnings=warnings,
    )
