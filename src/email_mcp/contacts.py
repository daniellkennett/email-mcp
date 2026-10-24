"""Contact-list CSVs and the suppression store."""

import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from email_mcp.config import EMAIL_RE, Settings, safe_child_path

SUPPRESSION_FILENAME = "suppressions.csv"
SUPPRESSION_HEADER = ["email", "reason", "added_at"]


class ContactListNotFound(Exception):
    """Contact list name is invalid or no such CSV exists."""


@dataclass(frozen=True)
class Contact:
    """One valid recipient row."""

    email: str
    fields: dict[str, str]     # all CSV columns incl. email, keys lowercased/stripped


@dataclass(frozen=True)
class ContactList:
    """A parsed contact CSV with rejected rows recorded."""

    name: str
    path: str
    count: int
    columns: list[str]
    contacts: list[Contact]
    invalid: list[dict]        # [{"row": int, "email": str, "reason": str}]


def valid_email(value: str) -> bool:
    """Return True when the value looks like a deliverable address."""
    candidate = value.strip()
    return len(candidate) <= 254 and EMAIL_RE.fullmatch(candidate) is not None


def list_contact_lists(settings: Settings) -> list[dict]:
    """Summarise each .csv in contacts_dir (non-recursive). A bad file shows an `error`."""
    folder = settings.contacts_dir
    if not folder.is_dir():
        return []
    listing: list[dict] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".csv":
            continue
        try:
            loaded = load_contact_list(settings, path.name)
        except (ValueError, ContactListNotFound) as exc:
            listing.append({"name": path.stem, "path": str(path), "count": 0,
                            "columns": [], "error": str(exc)})
            continue
        listing.append({"name": loaded.name, "path": loaded.path,
                        "count": loaded.count, "columns": loaded.columns})
    return listing


def load_contact_list(settings: Settings, name: str) -> ContactList:
    """Parse a contact CSV (name with or without .csv), requiring an email column."""
    filename = name if name.lower().endswith(".csv") else f"{name}.csv"
    path = safe_child_path(settings.contacts_dir, filename)
    if path is None:
        raise ContactListNotFound(f"Invalid contact list name {name!r}: use a plain filename.")
    if not path.is_file():
        raise ContactListNotFound(f"No contact list named {name!r} in {settings.contacts_dir}.")

    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.reader(handle)
        header = next(rows, [])
        columns = [column.strip().lower() for column in header]
        if "email" not in columns:
            raise ValueError(f"{path.name} has no 'email' column. Headers found: {header}")
        email_index = columns.index("email")

        contacts: list[Contact] = []
        invalid: list[dict] = []
        seen: set[str] = set()
        # Row numbers are 1-based CSV records; the header is row 1.
        for row_number, row in enumerate(rows, start=2):
            padded = row + [""] * (len(columns) - len(row))
            fields = {key: value.strip() for key, value in zip(columns, padded) if key}
            email = padded[email_index].strip().lower()
            fields["email"] = email

            reason: str | None = None
            if not email:
                reason = "blank email"
            elif not valid_email(email):
                reason = "invalid email"
            elif email in seen:
                reason = "duplicate email"

            if reason is not None:
                invalid.append({"row": row_number, "email": email, "reason": reason})
                continue
            seen.add(email)
            contacts.append(Contact(email=email, fields=fields))

    return ContactList(
        name=path.stem,
        path=str(path),
        count=len(contacts),
        columns=list(dict.fromkeys(column for column in columns if column)),
        contacts=contacts,
        invalid=invalid,
    )


def suppression_path(settings: Settings) -> Path:
    """Location of the suppression store."""
    return settings.state_dir / SUPPRESSION_FILENAME


def ensure_suppression_store(settings: Settings) -> Path:
    """Create the suppression CSV with its header if missing; return its path."""
    path = suppression_path(settings)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(SUPPRESSION_HEADER)
    return path


def load_suppressions(settings: Settings) -> set[str]:
    """Return the lowercased set of suppressed addresses."""
    path = ensure_suppression_store(settings)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.reader(handle)
        header = [column.strip().lower() for column in next(rows, [])]
        index = header.index("email") if "email" in header else 0
        addresses = {row[index].strip().lower() for row in rows if len(row) > index}
    addresses.discard("")
    return addresses


def load_suppression_records(settings: Settings) -> list[dict[str, str]]:
    """Full suppression rows (email, reason, added_at), newest first, for mirroring opt-outs."""
    path = suppression_path(settings)
    if not path.exists():
        return []
    rows: list[dict[str, str]] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                address = (row.get("email") or "").strip().lower()
                if address:
                    rows.append({"email": address,
                                 "reason": (row.get("reason") or "").strip(),
                                 "added_at": (row.get("added_at") or "").strip()})
    except OSError:
        return []
    rows.reverse()
    return rows


def add_suppression(settings: Settings, email: str, reason: str = "") -> bool:
    """Suppress an address. Returns False if it was already suppressed."""
    address = email.strip().lower()
    if not valid_email(address):
        raise ValueError(f"Not a valid email address: {email!r}")
    if address in load_suppressions(settings):
        return False
    path = ensure_suppression_store(settings)
    with path.open("a", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow([address, reason, datetime.now(UTC).isoformat()])
    return True
