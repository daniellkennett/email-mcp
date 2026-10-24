"""FastMCP server exposing the local email-marketing engine to Claude.

Every tool returns plain JSON-serialisable data. Engine exceptions are turned into
{"error": ..., "hint": ...} dicts by the ``guarded`` decorator so the model can act on them.
Nothing here may write to stdout: stdout carries the MCP stdio protocol.
"""

import csv
import datetime
import functools
import logging
import os
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from email_mcp import contacts, templates
from email_mcp.audit import hourly_count, read_log
from email_mcp.config import ConfigError, Settings, load_settings, safe_child_path
from email_mcp.contacts import (
    Contact,
    ContactList,
    ContactListNotFound,
    add_suppression,
    load_contact_list,
    load_suppression_records,
    load_suppressions,
    valid_email,
)
from email_mcp.mailer import Message, RateLimitExceeded, SendError, SendResult, send_bulk
from email_mcp.templates import (
    TemplateNotFound,
    blank_variables,
    find_variables,
    html_to_text,
    malformed_placeholders,
    missing_variables,
    render,
)
from email_mcp.tls import trust_source

logger = logging.getLogger("email_mcp.server")

server = FastMCP("email-marketing")

PREVIEW_CHARS = 4000
PREVIEW_MAX_RECIPIENTS = 10
CONTACT_SAMPLE_MAX = 200
LOG_RECORDS_MAX = 200
SUPPRESSION_EXPORT_MAX = 2000
FAILURE_REASONS_SHOWN = 5
MISSING_REPORT_SHOWN = 20
IMPORT_EXAMPLES_SHOWN = 5
PLACEHOLDER_MARKERS = ("FILL_IN", "PUT-THE-", "PUT-YOUR-", "YOUR-RELAY-")
# Odoo marks an unmailable partner with one of these. Any truthy value means opted out.
# res.partner.is_blacklisted is computed from mail.blacklist; opt_out is the mailing-list flag.
OPT_OUT_FIELDS = ("is_blacklisted", "email_blacklisted", "opt_out", "opted_out", "unsubscribed")
DRY_RUN_NOTE = (
    "Dry run: everything was rendered and validated, but no SMTP connection was opened "
    "and nothing was sent. In a dry run, 'sent' counts the messages that WOULD be sent. "
    "Re-run with dry_run=false only after the operator has approved."
)
NO_UNSUBSCRIBE_WARNING = (
    "UNSUBSCRIBE_BASE_URL is not set: sent mail gets no unsubscribe footer and no "
    "List-Unsubscribe header. Set it before any real campaign."
)


@functools.lru_cache(maxsize=1)
def settings() -> Settings:
    """Load configuration once per process. A ConfigError is not cached, so it re-raises."""
    return load_settings()


def error_result(message: str, hint: str) -> dict[str, str]:
    return {"error": message, "hint": hint}


def jsonable(value: Any) -> Any:
    """Recursively convert engine values (paths, dates, sets) into JSON-safe data."""
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    return str(value)


def mask_login(login: str | None) -> str | None:
    """Mask an SMTP login for display: 'postmaster@mg.example.com' -> 'po***@mg.example.com'."""
    if not login:
        return None
    local, separator, domain = login.partition("@")
    if not separator:
        return f"{local[:2]}***"
    return f"{local[:2]}***@{domain}"


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def guarded(func):
    """Convert engine exceptions into {"error", "hint"} dicts instead of tracebacks."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ConfigError as exc:
            return error_result(
                str(exc),
                "Fix the configuration in the project-root .env (see .env.example), then restart "
                "the MCP server: settings are loaded once per server process.",
            )
        except TemplateNotFound as exc:
            return error_result(str(exc), "Call list_templates to see the valid template names.")
        except ContactListNotFound as exc:
            return error_result(str(exc), "Call list_contact_lists to see the valid list names.")
        except RateLimitExceeded as exc:
            return error_result(
                str(exc),
                "The hourly send cap is reached. Call send_log to see recent volume. Wait for "
                "the window to clear, or raise MAX_MESSAGES_PER_HOUR in .env and restart.",
            )
        except SendError as exc:
            return error_result(
                str(exc),
                "The send was refused or the SMTP relay failed. Check config_status, then "
                "SMTP_USER and SMTP_PASSWORD in .env (Mailgun and SendGrid use different login "
                "formats). Dry runs never contact SMTP.",
            )
        except ValueError as exc:
            return error_result(
                str(exc),
                "A template, contact list or variable problem. Check the inputs and retry.",
            )
        except Exception as exc:  # last resort: never hand the model a traceback
            logger.exception("Unhandled error in tool %s", func.__name__)
            return error_result(
                f"{type(exc).__name__}: {exc}",
                "Unexpected error. The traceback is in the MCP server log (stderr).",
            )

    return wrapper


def missing_names(html: str, subject: str, merged: dict[str, Any]) -> list[str]:
    """Variable names the body or subject needs that the merged map does not supply."""
    return sorted(set(missing_variables(html, merged)) | set(missing_variables(subject, merged)))


def blank_names(html: str, subject: str, merged: dict[str, Any]) -> list[str]:
    """Variable names the body or subject uses that are supplied but empty.

    Strict rendering only checks that a key exists, so a blank CSV cell renders as nothing and
    "Hi ," would go out. This catches it before the send.
    """
    return sorted(set(blank_variables(html, merged)) | set(blank_variables(subject, merged)))


def malformed_error(html: str, subject: str) -> dict[str, Any] | None:
    """Refuse a template or subject containing a placeholder the engine cannot fill."""
    found = sorted(set(malformed_placeholders(html)) | set(malformed_placeholders(subject)))
    if not found:
        return None
    return error_result(
        f"Template or subject contains unusable placeholder(s): {', '.join(found)}. "
        "Nothing was sent.",
        "Placeholder names may only contain letters, digits and underscores, so "
        "'{{ first-name }}' or '{{ first name }}' is treated as literal text and would "
        "reach the recipient with the braces visible. Rename them to snake_case.",
    )


def merge_preview(html: str, subject: str, merged: dict[str, Any]) -> tuple[str, str, list[str]]:
    """Render without strict checking so a preview can still show what it could fill.

    The third value lists names that would stop a real send, either absent from the merge map
    or present but blank. The subject is plain text (escape=False); the body is HTML.
    """
    unusable = sorted(set(missing_names(html, subject, merged))
                      | set(blank_names(html, subject, merged)))
    return (render(subject, merged, strict=False, escape=False),
            render(html, merged, strict=False),
            unusable)


def preview_item(
    email: str | None, subject_text: str, body: str, missing: list[str]
) -> dict[str, Any]:
    return {
        "email": email,
        "subject": subject_text,
        "html": body[:PREVIEW_CHARS],
        "truncated": len(body) > PREVIEW_CHARS,
        "unresolved_variables": missing,
    }


def load_eligible(cfg: Settings, name: str) -> tuple[ContactList, list[Contact]]:
    """Parse a contact list and drop suppressed addresses. Returns (list, eligible contacts)."""
    cl = load_contact_list(cfg, name)
    suppressed = load_suppressions(cfg)
    return cl, [c for c in cl.contacts if c.email.strip().lower() not in suppressed]


def normalise_recipients(raw: list[str]) -> tuple[list[str], list[str]]:
    """Return (valid unique addresses in input order, invalid entries). Dedupe ignores case."""
    seen: set[str] = set()
    good: list[str] = []
    bad: list[str] = []
    for item in raw:
        address = item.strip()
        if not valid_email(address):
            bad.append(address)
        elif address.lower() not in seen:
            seen.add(address.lower())
            good.append(address)
    return good, bad


def recipient_cap_error(cfg: Settings, count: int) -> dict[str, str] | None:
    if count > cfg.max_recipients_per_run:
        return error_result(
            f"{count} recipients exceeds the per-run cap of {cfg.max_recipients_per_run}.",
            "Stage the send with a smaller limit (send_campaign), or raise "
            "MAX_RECIPIENTS_PER_RUN in .env and restart the server.",
        )
    return None


def summarize_send(result: SendResult) -> dict[str, Any]:
    failures = [f"{item.get('email') or '?'}: {item.get('reason') or 'unknown error'}"
                for item in result.details
                if str(item.get("status", "")).lower() in ("failed", "error")]
    summary = {
        "run_id": result.run_id,
        "dry_run": bool(result.dry_run),
        "total": result.total,
        "sent": result.sent,
        "skipped": result.skipped,
        "failed": result.failed,
        "duration_seconds": round(float(result.duration_seconds), 2),
        "failure_reasons": failures[:FAILURE_REASONS_SHOWN],
        "warnings": list(result.warnings),
    }
    if result.dry_run:
        summary["note"] = DRY_RUN_NOTE
    return summary


def dispatch(messages: list[Message], dry_run: bool | None) -> dict[str, Any]:
    cfg = settings()
    effective_dry_run = cfg.dry_run_default if dry_run is None else dry_run
    return summarize_send(send_bulk(cfg, messages, dry_run=effective_dry_run))


@server.tool()
@guarded
def list_templates() -> Any:
    """List the HTML email templates available for sending.

    Each entry gives the template name (pass it as `template` to preview_campaign or
    send_campaign), the {{ variable }} names the template needs, the subject hint from its
    `<!-- subject: ... -->` comment (a suggestion only), its size and last-modified time.
    Read-only; sends nothing.
    """
    return [
        {
            "name": info.name,
            "variables": info.variables,
            "subject_hint": info.subject_hint,
            "size_bytes": info.size_bytes,
            "modified": info.modified,
        }
        for info in templates.list_templates(settings())
    ]


@server.tool()
@guarded
def read_template(name: str) -> dict[str, Any]:
    """Return the raw HTML of one template, plus its variables and subject hint.

    Use it to draft a subject line and to see which {{ variables }} a campaign must supply.
    The output can be large (the full HTML document), so read a template only when you need
    its markup. Read-only; sends nothing.
    """
    cfg = settings()
    html = templates.read_template(cfg, name)
    return {
        "name": name,
        "html": html,
        "variables": find_variables(html),
        "subject_hint": templates.parse_subject_hint(html),
    }


@server.tool()
@guarded
def list_contact_lists() -> Any:
    """List the CSV contact lists available for campaigns.

    Each entry gives the list name (pass it as `contact_list`), its row count and its column
    names. A list that cannot be parsed (for example, no email column) shows an `error`.
    Use read_contact_list to see a sample and the count of invalid or duplicate rows. Read-only;
    sends nothing.
    """
    listing: list[dict[str, Any]] = []
    for item in contacts.list_contact_lists(settings()):
        row: dict[str, Any] = {
            "name": item.get("name"),
            "count": item.get("count", 0),
            "columns": item.get("columns", []),
        }
        if item.get("error"):
            row["error"] = item["error"]
        listing.append(row)
    return listing


@server.tool()
@guarded
def read_contact_list(name: str, limit: int = 25) -> dict[str, Any]:
    """Show a sample of contacts from one CSV list, with list-level totals.

    `limit` (default 25, maximum 200) caps how many contacts are returned; the full list is
    never returned. Each sample item has `email` and `fields` (that contact's CSV columns,
    lowercase names). Also returns the total count, the column names, and the number of
    invalid or duplicate rows. Read-only; sends nothing.
    """
    capped = clamp(limit, 1, CONTACT_SAMPLE_MAX)
    cl = load_contact_list(settings(), name)
    return {
        "name": cl.name,
        "total": cl.count,
        "columns": list(cl.columns),
        "invalid_rows": len(cl.invalid),
        "limit": capped,
        "sample": [
            {"email": contact.email, "fields": jsonable(contact.fields)}
            for contact in cl.contacts[:capped]
        ],
    }


@server.tool()
@guarded
def preview_campaign(
    template: str,
    subject: str,
    variables: dict[str, str] | None = None,
    contact_list: str | None = None,
    sample_size: int = 3,
) -> dict[str, Any]:
    """Preview how a campaign will render for real recipients. Sends nothing.

    Each recipient's variables are their CSV columns overlaid by `variables`: an explicit
    `variables` value wins over a CSV column with the same name. The subject is rendered as a
    template too, so {{ first_name }} works there. The first `sample_size` recipients (max 10)
    are rendered in full.

    Returns:
    - recipient_count: contacts left after the suppression list is applied.
    - suppressed_count: contacts removed by the suppression list.
    - recipients_missing_variables: how many recipients have a variable the template or
      subject needs that is absent OR supplied but blank, with the first few named.
      send_campaign refuses to send while this is non-zero, because a blank renders as
      nothing ("Hi ,").
    - ready_to_send: true when a contact list was given, recipients remain, and none are missing variables.
    - previews: rendered subject and HTML for each sampled recipient. HTML is cut to about
      4000 characters, and `truncated` says so.
    - unresolved_variables: names the sampled previews could not fill.

    Without `contact_list`, only one preview is rendered from `variables` alone and the
    recipient counts are null. The unsubscribe footer is added at send time, so it does not
    appear in this preview.
    """
    cfg = settings()
    html = templates.read_template(cfg, template)
    bad_placeholder = malformed_error(html, subject)
    if bad_placeholder:
        return bad_placeholder
    shared = variables or {}

    if contact_list is None:
        subject_text, body, missing = merge_preview(html, subject, shared)
        return {
            "template": template,
            "previews": [preview_item(None, subject_text, body, missing)],
            "recipient_count": None,
            "suppressed_count": None,
            "recipients_missing_variables": None,
            "ready_to_send": False,
            "unresolved_variables": missing,
            "note": "No contact_list given: this is a single preview from variables only. "
            "Pass contact_list to check the real recipients.",
        }

    cl, eligible = load_eligible(cfg, contact_list)
    samples_wanted = clamp(sample_size, 1, PREVIEW_MAX_RECIPIENTS)
    previews: list[dict[str, Any]] = []
    missing_report: list[dict[str, Any]] = []
    for index, contact in enumerate(eligible):
        subject_text, body, missing = merge_preview(html, subject, {**contact.fields, **shared})
        if index < samples_wanted:
            previews.append(preview_item(contact.email, subject_text, body, missing))
        if missing:
            missing_report.append({"email": contact.email, "missing": missing})

    unresolved = sorted({name for item in previews for name in item["unresolved_variables"]})
    return {
        "template": template,
        "contact_list": contact_list,
        "recipient_count": len(eligible),
        "suppressed_count": len(cl.contacts) - len(eligible),
        "recipients_missing_variables": len(missing_report),
        "missing_report": missing_report[:MISSING_REPORT_SHOWN],
        "ready_to_send": bool(eligible) and not missing_report,
        "previews": previews,
        "unresolved_variables": unresolved,
    }


@server.tool()
@guarded
def send_email(
    recipients: list[str],
    subject: str,
    html_body: str,
    text_body: str | None = None,
    dry_run: bool | None = None,
) -> dict[str, Any]:
    """Send one HTML email to an explicit list of addresses (ad-hoc: no template, no contact list).

    Use it for one-off messages or a small hand-picked group. Each address is validated and
    duplicates are dropped. The body is sent exactly as given, so any unfilled {{ placeholder }}
    causes an error instead of going out literally. Suppressed addresses are skipped. The
    per-run recipient cap and hourly cap apply, and the unsubscribe footer is added when
    UNSUBSCRIBE_BASE_URL is set.

    `dry_run` defaults to the DRY_RUN_DEFAULT setting, which is true unless the operator has
    changed it. A dry run renders and validates everything but opens NO SMTP connection and
    sends nothing. Always do a dry run first and have the operator confirm before a real send.

    `text_body` is the plain-text alternative; if omitted, one is derived from `html_body`.

    Returns run_id, dry_run, total/sent/skipped/failed counts and the first few failure reasons.
    """
    cfg = settings()
    addresses, invalid = normalise_recipients(recipients)
    if invalid:
        return error_result(
            f"Invalid email address(es): {', '.join(invalid[:10])}",
            "Correct or remove these addresses and retry. Nothing was sent.",
        )
    if not addresses:
        return error_result("No recipients given.", "Pass at least one email address in recipients.")
    bad_placeholder = malformed_error(f"{html_body}\n{text_body or ''}", subject)
    if bad_placeholder:
        return bad_placeholder
    cap_error = recipient_cap_error(cfg, len(addresses))
    if cap_error:
        return cap_error
    unfilled = sorted(set(find_variables(subject)) | set(find_variables(html_body))
                      | set(find_variables(text_body or "")))
    if unfilled:
        return error_result(
            f"Unfilled template variables in the message: {', '.join(unfilled)}",
            "Replace the placeholders with real text, or use send_campaign to merge them per recipient.",
        )

    plain = text_body if text_body is not None else html_to_text(html_body)
    return dispatch(
        [Message(to_email=address, subject=subject, html=html_body, text=plain)
         for address in addresses],
        dry_run,
    )


@server.tool()
@guarded
def send_campaign(
    template: str,
    contact_list: str,
    subject: str,
    variables: dict[str, str] | None = None,
    dry_run: bool | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Send a template to the recipients of a contact list, merging each recipient's own fields.

    Merge rules: each recipient's variables are their CSV columns (lowercase names), overlaid by
    the shared `variables` argument. An explicit `variables` value WINS over a CSV column with
    the same name. The `subject` is also a template, so {{ first_name }} works in it.

    Safety:
    - Suppressed addresses are removed before anything is sent.
    - If any recipient to be sent to lacks a variable the template or subject needs, NOTHING is
      sent. The error lists each affected recipient and the missing names. Partial campaigns
      are never sent.
    - `limit` sends only to the first N eligible recipients, for a staged rollout (for example
      limit=10, check the results, then run the rest). A later run does not skip recipients
      already sent to, so the second run must target only the remaining contacts.
    - The per-run recipient cap and the hourly send cap are enforced.
    - `dry_run` defaults to the DRY_RUN_DEFAULT setting. A dry run renders and validates everything
      but opens NO SMTP connection and sends nothing. Always do a dry run first and have the
      operator confirm before a real send.

    Returns run_id, dry_run, total/sent/skipped/failed counts, the first few failure reasons,
    recipients_targeted and suppressed_excluded.
    """
    cfg = settings()
    if limit is not None and limit < 1:
        return error_result("limit must be at least 1.", "Omit limit to target every eligible recipient.")

    html = templates.read_template(cfg, template)
    bad_placeholder = malformed_error(html, subject)
    if bad_placeholder:
        return bad_placeholder
    cl, eligible = load_eligible(cfg, contact_list)
    if not eligible:
        return error_result(
            f"No recipients left in '{contact_list}' after the suppression list.",
            "Check the list with read_contact_list, and the suppression list in STATE_DIR.",
        )
    targets = eligible if limit is None else eligible[:limit]
    cap_error = recipient_cap_error(cfg, len(targets))
    if cap_error:
        return cap_error

    shared = variables or {}
    problems: list[dict[str, Any]] = []
    messages: list[Message] = []
    for contact in targets:
        merged = {**contact.fields, **shared}
        missing = missing_names(html, subject, merged)
        blank = blank_names(html, subject, merged)
        if missing or blank:
            problems.append({"email": contact.email, "missing": missing, "blank": blank})
            continue
        body = render(html, merged, strict=True)
        messages.append(
            Message(
                to_email=contact.email,
                subject=render(subject, merged, strict=True, escape=False),
                html=body,
                text=html_to_text(body),
            )
        )

    if problems:
        return {
            "error": f"{len(problems)} recipient(s) have unusable template variables; "
            "nothing was sent.",
            "hint": "`missing` means no such column or value; `blank` means the column exists "
            "but the cell is empty, which would render as nothing (\"Hi ,\"). Fill the cells, "
            "add the columns, or pass the values in `variables` (which applies to every "
            "recipient), then retry.",
            "missing": problems[:MISSING_REPORT_SHOWN],
        }

    result = dispatch(messages, dry_run)
    result["recipients_targeted"] = len(targets)
    result["suppressed_excluded"] = len(cl.contacts) - len(eligible)
    return result


def snake_key(raw: str) -> str:
    """Normalise a field name to the CSV column convention (snake_case)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", str(raw).strip()).strip("_").lower()


def flatten_value(value: Any) -> str:
    """Render one incoming field value as CSV text.

    Odoo returns a many2one as a two-element [id, display_name] pair, so the readable
    half is what belongs in a merge variable -- "Engines Ltd", not "[7, 'Engines Ltd']".
    """
    if value is None or value is False:
        return ""
    if isinstance(value, (list, tuple)):
        if len(value) == 2 and isinstance(value[1], str):
            return value[1].strip()
        return ", ".join(flatten_value(item) for item in value if item not in (None, False))
    return str(value).strip()


def looks_opted_out(row: dict[str, Any], fields: tuple[str, ...]) -> str | None:
    """Return the field name marking this row as opted out, or None.

    A JSON round trip may deliver booleans as strings, so the usual false-ish spellings
    count as not opted out and anything else truthy counts as opted out.
    """
    for field_name in fields:
        if field_name not in row:
            continue
        value = row[field_name]
        if isinstance(value, str):
            if value.strip().lower() in {"", "false", "0", "no", "none", "null"}:
                continue
            return field_name
        if value:
            return field_name
    return None


@server.tool()
@guarded
def import_contacts(
    rows: list[dict[str, Any]],
    name: str,
    email_field: str = "email",
    replace: bool = False,
) -> dict[str, Any]:
    """Write contact rows from any external source into a reusable contact list CSV.

    This is the bridge for contacts that live somewhere else -- an Odoo MCP server, another
    CRM, a spreadsheet you have already read. Fetch the records with that system's own
    tools, then pass them here as a list of flat JSON objects. Each row becomes one CSV row,
    and every field becomes a merge variable available to templates as `{{ field_name }}`,
    so `send_campaign` personalisation works exactly as it does for a hand-made list.

    Field names are normalised to snake_case, so Odoo's `function` or a CRM's
    "First Name" arrive usable. Pass `email_field` if the address is not in `email`.

    OPT-OUTS ARE ENFORCED IN BOTH DIRECTIONS. A row whose source marks it unmailable --
    any truthy `is_blacklisted`, `email_blacklisted`, `opt_out`, `opted_out` or
    `unsubscribed` -- is dropped from the list AND written to the local suppression list,
    so the exclusion survives later imports from any source, including one where the flag
    has been cleared. Addresses already suppressed locally are dropped too. Use
    `export_suppressions` to carry local unsubscribes back the other way.

    Rows with a missing or invalid address, and duplicates within the batch, are skipped and
    counted. `replace` must be true to overwrite an existing list. Returns the counts, the
    columns written, and a few partially masked examples per skip reason -- never the whole
    list, so a transcript does not become a copy of your subscriber data.
    """
    cfg = settings()
    if not rows:
        return error_result("No rows given.", "Pass the records fetched from the source system.")

    stem = name[:-4] if name.lower().endswith(".csv") else name
    target = safe_child_path(cfg.contacts_dir, f"{stem}.csv")
    if target is None:
        return error_result(
            f"'{name}' is not a usable list name.",
            "Use a plain filename with no path separators, for example odoo_newsletter.",
        )
    if target.exists() and not replace:
        return error_result(
            f"Contact list '{stem}' already exists.",
            "Pass replace=true to overwrite it, or choose another name.",
        )

    key = snake_key(email_field)
    suppressed = load_suppressions(cfg)
    skipped: dict[str, list[str]] = {"invalid_email": [], "duplicate": [],
                                     "opted_out_at_source": [], "already_suppressed": []}
    newly_suppressed = 0
    columns: set[str] = set()
    seen: set[str] = set()
    cleaned: list[dict[str, str]] = []

    for raw in rows:
        if not isinstance(raw, dict):
            skipped["invalid_email"].append("(not an object)")
            continue
        row = {snake_key(k): flatten_value(v) for k, v in raw.items()}
        address = row.pop(key, "").lower()
        # Drop any other address-named column: the value popped above is the one that was
        # validated, deduped and checked against the suppression list, so letting a stray
        # source "email" field through would mail an address that passed none of those.
        row.pop("email", None)
        label = mask_login(address) or "(blank)"
        # Detect on the normalised row, not the raw dict: a CRM sending "Unsubscribed"
        # or "Is_Blacklisted" would otherwise slip past the snake_case field names.
        opt_out_field = looks_opted_out(row, OPT_OUT_FIELDS)

        if opt_out_field is not None:
            skipped["opted_out_at_source"].append(f"{label} [{opt_out_field}]")
            if valid_email(address) and add_suppression(
                    cfg, address, f"opted out at source ({opt_out_field})"):
                newly_suppressed += 1
        elif not valid_email(address):
            skipped["invalid_email"].append(label)
        elif address in suppressed:
            skipped["already_suppressed"].append(label)
        elif address in seen:
            skipped["duplicate"].append(label)
        else:
            seen.add(address)
            # The opt-out flags themselves are not merge variables.
            payload = {k: v for k, v in row.items() if k not in OPT_OUT_FIELDS}
            columns.update(payload)
            cleaned.append({"email": address, **payload})

    if not cleaned:
        return error_result(
            "No mailable contacts left after filtering; nothing was written.",
            "Check the skip counts: every row was invalid, duplicate, opted out at the "
            "source, or already on the local suppression list.",
        )

    header = ["email"] + sorted(columns)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cleaned)

    blank_counts = {column: sum(1 for r in cleaned if not r.get(column, "").strip())
                    for column in header}
    return {
        "contact_list": stem,
        "path": str(target),
        "rows_in": len(rows),
        "contacts_written": len(cleaned),
        "columns": header,
        "merge_variables": [c for c in header if c != "email"],
        "skipped": {reason: len(items) for reason, items in skipped.items()},
        "skipped_examples": {reason: items[:IMPORT_EXAMPLES_SHOWN]
                             for reason, items in skipped.items() if items},
        "newly_suppressed": newly_suppressed,
        "columns_with_blanks": {c: n for c, n in blank_counts.items() if n},
        "next_step": "Check columns_with_blanks against the template's variables: "
                     "send_campaign refuses a run when a needed value is blank. "
                     "Then preview_campaign, then a dry run.",
    }


@server.tool()
@guarded
def export_suppressions(limit: int = 500) -> dict[str, Any]:
    """List locally suppressed addresses so they can be mirrored into the source system.

    Opt-outs recorded here -- via the `unsubscribe` tool, or captured during
    `import_contacts` -- are honoured by this server only. The source system (Odoo, or
    whatever holds the master contact record) does not learn about them on its own, so
    anyone who unsubscribes here can still be mailed from there.

    Use this to close that loop: read the list, then write the opt-out back through the
    source system's own tools (in Odoo, blacklisting the address or setting the partner's
    opt-out flag). Returns each address with its reason and timestamp, newest first.
    Addresses are returned in full because that is required to act on them, so do not
    paste the output anywhere it does not belong.
    """
    records = load_suppression_records(settings())
    capped = clamp(limit, 1, SUPPRESSION_EXPORT_MAX)
    return {
        "total": len(records),
        "returned": min(len(records), capped),
        "suppressions": records[:capped],
        "note": "Mirror these into the source system with its own tools. Nothing here "
                "updates Odoo, your CRM, or the SMTP relay's own suppression list.",
    }


@server.tool()
@guarded
def unsubscribe(email: str, reason: str = "") -> dict[str, Any]:
    """Add an address to the local suppression list so no future send reaches it.

    Use it when someone asks to unsubscribe or opt out, or when the operator asks you to.
    Every later send_email and send_campaign run skips suppressed addresses. This updates only
    the local list in STATE_DIR. It does NOT update the SMTP relay's own suppression or
    unsubscribe list, so the operator must also update the relay, or the relay may still send.
    Opt-outs must be honoured promptly. `reason` is an optional note for the record (for
    example "replied STOP").
    """
    address = email.strip().lower()
    if not valid_email(address):
        return error_result(
            f"'{email}' is not a valid email address.",
            "Pass a single address, for example name@example.com.",
        )
    added = add_suppression(settings(), address, reason)
    return {
        "email": address,
        "status": "suppressed" if added else "already_suppressed",
        "reason": reason or None,
        "note": "Recorded in the local suppression list only. The SMTP relay's suppression list "
        "was not changed.",
    }


@server.tool()
@guarded
def send_log(limit: int = 20) -> dict[str, Any]:
    """Show recent sending activity from the audit log (state/send_log.jsonl), newest first.

    Returns up to `limit` records (default 20, maximum 200), each a run or send event. Also
    returns hourly_sent, the number of messages sent in the trailing hour, and
    max_messages_per_hour, the configured cap. Use it to answer "what went out?" and to check
    how close the next send is to the hourly cap. Read-only.
    """
    cfg = settings()
    records = read_log(cfg, limit=clamp(limit, 1, LOG_RECORDS_MAX))
    return {
        "hourly_sent": hourly_count(cfg),
        "max_messages_per_hour": cfg.max_messages_per_hour,
        "records_returned": len(records),
        "records": jsonable(records),
    }


@server.tool()
@guarded
def config_status() -> dict[str, Any]:
    """Show the effective configuration with secrets redacted, for troubleshooting.

    Covers the SMTP host, port and STARTTLS setting, the SMTP login (masked, e.g.
    'po***@mg.example.com'), whether a password is set (its value is never shown), the From
    name and address, Reply-To, the template, contact and state directories, the send caps,
    DRY_RUN_DEFAULT, and whether an unsubscribe base URL is configured. `warnings` lists
    problems to fix before a real send. Read-only; it opens no network connection.
    """
    cfg = settings()
    warnings: list[str] = []
    if not cfg.unsubscribe_base_url:
        warnings.append(NO_UNSUBSCRIBE_WARNING)
    if cfg.allow_send_without_unsubscribe:
        warnings.append(
            "ALLOW_SEND_WITHOUT_UNSUBSCRIBE is on: real sends are permitted with no "
            "unsubscribe link. Only acceptable for a test to an address you control."
        )
    # The example config ships FILL_IN markers; say so plainly rather than letting an
    # unedited placeholder look like a working setting.
    placeholders = sorted(
        name
        for name, value in (("SMTP_PASSWORD", cfg.smtp_password), ("SMTP_USER", cfg.smtp_user),
                            ("SMTP_HOST", cfg.smtp_host), ("FROM_EMAIL", cfg.from_email))
        if any(marker in str(value).upper() for marker in PLACEHOLDER_MARKERS)
    )
    if placeholders:
        warnings.append(
            f"Still unedited in the Claude config: {', '.join(placeholders)}. "
            "Replace the placeholder value(s) and restart Claude."
        )
    return {
        "smtp_host": cfg.smtp_host,
        "smtp_port": cfg.smtp_port,
        "smtp_starttls": cfg.smtp_starttls,
        "smtp_user": mask_login(cfg.smtp_user),
        "smtp_password_set": bool(cfg.smtp_password),
        "from_email": cfg.from_email,
        "from_name": cfg.from_name,
        "reply_to": cfg.reply_to,
        "template_dir": jsonable(cfg.template_dir),
        "contacts_dir": jsonable(cfg.contacts_dir),
        "state_dir": jsonable(cfg.state_dir),
        "max_recipients_per_run": cfg.max_recipients_per_run,
        "max_messages_per_hour": cfg.max_messages_per_hour,
        "batch_size": cfg.batch_size,
        "batch_pause_seconds": cfg.batch_pause_seconds,
        "dry_run_default": cfg.dry_run_default,
        "unsubscribe_url_configured": bool(cfg.unsubscribe_base_url),
        "tls_trust_source": trust_source(cfg),
        "real_sends_allowed": bool(cfg.unsubscribe_base_url
                                   or cfg.allow_send_without_unsubscribe),
        "warnings": warnings,
    }


def main() -> None:
    """Console-script entry point: serve MCP over stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
