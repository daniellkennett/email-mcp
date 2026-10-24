#!/usr/bin/env python3
"""Normalize a messy contact export into a clean CSV for the email-mcp server.

Standard library only, so it runs on a bare system python3 without the project venv.
It reads one export, writes the cleaned CSV to -o, and prints a report to stderr.
Nothing is printed to stdout. Addresses in the report are masked (ro***@example.com)
and at most a few examples are shown per category, so running this does not copy the
list into a transcript.

Exit codes: 0 clean run, 1 blockers (no output written), 2 usage or format error.
"""

import argparse
import csv
import io
import re
import sys
from collections import Counter
from pathlib import Path

EXIT_CLEAN = 0
EXIT_BLOCKED = 1
EXIT_USAGE = 2

# Copied from src/email_mcp/config.py (EMAIL_RE) and src/email_mcp/contacts.py (254-char cap).
# Keep in sync so this script never accepts an address the server would reject.
EMAIL_RE = re.compile(r"[^\s@<>,;:\"()\[\]]+@[^\s@]+\.[^\s@]+")
MAX_EMAIL_LENGTH = 254
DISPLAY_NAME_RE = re.compile(r"^[^<]*<([^<>]+)>\s*$")
DELIMITERS = [",", ";", "\t", "|"]
DELIMITER_LABELS = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
TARGET_NAME_RE = re.compile(r"[a-z0-9_]+")
ZERO_WIDTH = str.maketrans("", "", "​‌‍﻿")
UTF8_BOM = b"\xef\xbb\xbf"
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
EXAMPLE_LIMIT = 5
ROLE_LOCAL_PARTS = frozenset([
    "info", "noreply", "no-reply", "postmaster", "abuse", "admin", "support",
    "sales", "help", "donotreply", "mailer-daemon",
])
# Keys are header text after lowercasing and treating spaces, hyphens and underscores alike.
ALIASES = {
    "email": ["email", "email address", "emailaddress", "e mail", "mail"],
    "first_name": ["first", "firstname", "first name", "given name", "givenname", "forename"],
    "last_name": ["last", "lastname", "last name", "surname", "family name"],
    "company": ["company", "company name", "account", "account name",
                "organization", "organisation"],
}
ALIAS_LOOKUP = {alias: target for target, aliases in ALIASES.items() for alias in aliases}


class UsageError(Exception):
    """Bad arguments, or an input the script cannot parse. Exit code 2."""


class Tally:
    """Counts and capped, masked examples for the report."""

    def __init__(self):
        self.drop_counts = Counter()
        self.drop_examples = {}
        self.blockers = []
        self.notes = []
        self.warnings = []


def say(text=""):
    print(text, file=sys.stderr)


def mask(value):
    """Partially hide an address so it can be printed safely."""
    if not value:
        return "(blank)"
    if "@" in value:
        local, _, domain = value.rpartition("@")
        keep = local[:2] if len(local) > 2 else local[:1]
        return f"{keep}***@{domain}"
    return value[:2] + "***" if len(value) > 2 else "***"


def clean_cell(value):
    return value.translate(ZERO_WIDTH).strip()


def header_key(name):
    return re.sub(r"[\s_\-]+", " ", name.strip().lower()).strip()


def sanitize_name(name):
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")


def normalize_column(value, flag):
    """Validate a target column name given on the command line. Refuse, do not guess."""
    name = value.strip().lower()
    if not TARGET_NAME_RE.fullmatch(name):
        raise UsageError(
            f"{flag} column {value.strip()!r} must use only letters, digits and underscores "
            f"(for example first_name)."
        )
    return name


def parse_map(item):
    source, sep, target = item.rpartition("=")
    if not sep or not source.strip() or not target.strip():
        raise UsageError(f"--map expects SOURCE=TARGET, got {item!r}")
    return source, target


def parse_default(item):
    column, sep, value = item.partition("=")
    if not sep or not column.strip() or not value.strip():
        raise UsageError("--default expects COLUMN=VALUE (both sides non-empty)")
    return normalize_column(column, "--default"), value.strip()


def read_export(path, warnings):
    """Return (text, encoding label). UTF-8 first, then latin-1 with a warning naming the line."""
    raw = path.read_bytes()
    if raw.startswith(UTF16_BOMS):
        raise UsageError("the file is UTF-16. Re-save the export as UTF-8 CSV and rerun.")
    bom = raw.startswith(UTF8_BOM)
    if bom:
        raw = raw[len(UTF8_BOM):]
    try:
        return raw.decode("utf-8"), "utf-8" + (", BOM stripped" if bom else "")
    except UnicodeDecodeError:
        pass
    for number, line in enumerate(raw.split(b"\n"), start=1):
        try:
            line.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append(
                f"not valid UTF-8: first non-UTF-8 bytes are on line {number}. Read the whole "
                f"file as latin-1 instead. Check accented names in the output."
            )
            break
    return raw.decode("latin-1"), "latin-1 (fallback)"


def sniff_delimiter(text):
    """Pick the candidate that splits the header line into the most fields. Ties go to comma."""
    first = text.split("\n", 1)[0].rstrip("\r")
    best, best_count = ",", 0
    for candidate in DELIMITERS:
        count = len(next(csv.reader([first], delimiter=candidate), []))
        if count > best_count:
            best, best_count = candidate, count
    return best


def parse_records(text, delimiter):
    try:
        return list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter,
                               skipinitialspace=True))
    except csv.Error as exc:
        raise UsageError(f"could not parse the file as CSV: {exc}")


def plan_columns(headers, maps, tally):
    """Return [(file_index, final_name)] in file order. Every rename goes into tally.notes."""
    explicit = {}
    for source, target in maps:
        wanted = source.strip().lower()
        matches = [i for i, header in enumerate(headers) if header.lower() == wanted]
        if not matches:
            raise UsageError(f"--map source {source.strip()!r} is not a column. Headers seen: {headers}")
        if len(matches) > 1:
            raise UsageError(f"--map source {source.strip()!r} matches {len(matches)} columns. "
                             "Make the header names unique in the export first.")
        explicit[matches[0]] = normalize_column(target, "--map")

    planned = []
    for index, header in enumerate(headers):
        if not header:
            tally.notes.append(f"dropped unnamed column at position {index + 1}")
            continue
        key = header_key(header)
        if index in explicit:
            name, how = explicit[index], "--map"
        elif key in ALIAS_LOOKUP:
            name, how = ALIAS_LOOKUP[key], "auto alias"
        else:
            name, how = sanitize_name(header), "auto rename"
        if not name:
            tally.notes.append(f"dropped column {header!r}: its name has no usable characters")
            continue
        if how == "--map" or name != header.lower():
            tally.notes.append(f"{how}: {header!r} -> {name}")
        planned.append((index, name))

    first_seen = {}
    for index, name in planned:
        if name in first_seen:
            raise UsageError(
                f"columns {headers[first_seen[name]]!r} and {headers[index]!r} both become "
                f"{name!r}. Use --map to choose one, or drop one from the export."
            )
        first_seen[name] = index
    if "email" not in first_seen:
        raise UsageError(
            f"no email column found. Headers seen: {headers}. Name the address column "
            f"'email', or map it: --map \"Your Column=email\"."
        )
    return planned


def load_suppressions(path, tally):
    """Return (set of lowercased addresses, description). Reads only the email column."""
    if path is None:
        tally.warnings.append("no --suppressions file given: suppressed addresses were NOT "
                              "removed. Pass state/suppressions.csv.")
        return set(), "none given"
    if not path.is_file():
        tally.warnings.append(f"suppression file not found at {path}: no addresses were removed. "
                              "Check the path before sending.")
        return set(), f"not found at {path}"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise UsageError(f"suppression file {path} is not UTF-8")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        return set(), f"empty ({path})"
    header = [clean_cell(cell).lower() for cell in rows[0]]
    index = header.index("email") if "email" in header else 0
    addresses = {clean_cell(row[index]).lower() for row in rows[1:] if len(row) > index}
    addresses.discard("")
    return addresses, f"{len(addresses)} address(es) from {path}"


def add_drop(tally, category, row_number, value):
    tally.drop_counts[category] += 1
    examples = tally.drop_examples.setdefault(category, [])
    if len(examples) < EXAMPLE_LIMIT:
        examples.append(f"row {row_number} {mask(value)}")


def process_rows(records, planned, defaults, required, suppressed, drop_roles, tally, stats):
    """Apply every per-row rule. Returns (kept rows as dicts, extra-field row numbers)."""
    header_count = len(records[0])
    seen = set()
    kept = []
    extra_rows = []
    for row_number, record in enumerate(records[1:], start=2):
        if not any(clean_cell(cell) for cell in record):
            stats["blank lines skipped"] += 1
            continue
        stats["data rows"] += 1
        if len(record) > header_count:
            extra_rows.append(row_number)

        values = {}
        for index, name in planned:
            values[name] = clean_cell(record[index]) if index < len(record) else ""

        address_text = values["email"]
        match = DISPLAY_NAME_RE.match(address_text)
        if match:
            address_text = match.group(1)
            stats["display-name addresses unwrapped"] += 1
        address = clean_cell(address_text).lower()
        values["email"] = address

        if not address:
            add_drop(tally, "invalid", row_number, "")
            continue
        if len(address) > MAX_EMAIL_LENGTH or not EMAIL_RE.fullmatch(address):
            add_drop(tally, "invalid", row_number, address)
            continue
        if address in seen:
            add_drop(tally, "duplicate", row_number, address)
            continue
        seen.add(address)

        local = address.split("@", 1)[0]
        if local in ROLE_LOCAL_PARTS:
            if drop_roles:
                add_drop(tally, "role account", row_number, address)
                continue
            stats["role-looking addresses kept"] += 1

        if address in suppressed:
            add_drop(tally, "suppressed", row_number, address)
            continue

        for column, value in defaults:
            if not values.get(column):
                values[column] = value
                stats[f"defaults filled: {column}"] += 1

        missing = [column for column in required if not values.get(column)]
        if missing:
            tally.blockers.append((row_number, address, missing))
            continue
        kept.append(values)
    return kept, extra_rows


def write_output(path, columns, rows):
    try:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore",
                                    lineterminator="\n")
            writer.writeheader()
            for values in rows:
                writer.writerow({column: values.get(column, "") for column in columns})
    except OSError as exc:
        raise UsageError(f"could not write {path}: {exc}")


def examples_text(items):
    return ", ".join(items)


def print_report(input_path, encoding, delimiter, headers, notes, suppression_note, stats,
                 tally, extra_rows, missing_required, kept_count, output_path, blocked):
    say(f"normalize_list: {input_path}")
    say(f"  encoding: {encoding}")
    say(f"  delimiter: {DELIMITER_LABELS.get(delimiter, repr(delimiter))}")
    say(f"  columns in file: {', '.join(repr(h) for h in headers)}")
    if notes:
        say("  column mapping:")
        for note in notes:
            say(f"    {note}")
    say(f"  suppression list: {suppression_note}")
    say(f"  data rows: {stats['data rows']} (blank lines skipped: {stats['blank lines skipped']})")
    if stats["display-name addresses unwrapped"]:
        say(f"  display-name addresses unwrapped: {stats['display-name addresses unwrapped']}")
    if stats["role-looking addresses kept"]:
        say(f"  role-looking addresses kept: {stats['role-looking addresses kept']} "
            "(pass --drop-role-accounts to drop them)")
    for key in sorted(k for k in stats if k.startswith("defaults filled: ")):
        column = key.replace("defaults filled: ", "")
        say(f"  default filled for {column}: {stats[key]} cell(s)")

    say("  dropped:")
    for category in ["invalid", "duplicate", "role account", "suppressed"]:
        count = tally.drop_counts[category]
        if count:
            say(f"    {category}: {count}   e.g. {examples_text(tally.drop_examples[category])}")
        else:
            say(f"    {category}: 0")
    if extra_rows:
        say(f"  warning: {len(extra_rows)} rows have more values than the header; extra values "
            f"were ignored. Often an unquoted delimiter. Rows e.g. "
            f"{', '.join(str(n) for n in extra_rows[:EXAMPLE_LIMIT])}")

    if tally.blockers:
        say(f"  BLOCKERS: {len(tally.blockers)} row(s) missing a required value. Nothing "
            "is written until these are fixed or defaulted.")
        shown = [f"row {row} {mask(address)} missing {', '.join(missing)}"
                 for row, address, missing in tally.blockers[:EXAMPLE_LIMIT]]
        say(f"    e.g. {examples_text(shown)}")
    for column in missing_required:
        say(f"  BLOCKER: required column {column!r} is not in the file and has no --default. "
            "Every row would be blank.")

    for warning in tally.warnings:
        say(f"  warning: {warning}")

    say(f"  rows {'that would be written' if blocked else 'written'}: {kept_count}")
    if blocked:
        say("result: BLOCKED. No output file was written. Fix the blockers above and rerun.")
    elif output_path is None:
        say(f"result: report only. Nothing written. {kept_count} rows would be written.")
    else:
        say(f"result: clean. Wrote {kept_count} rows to {output_path}")


def run(args):
    tally = Tally()
    maps = [parse_map(item) for item in args.map]
    defaults = [parse_default(item) for item in args.default]
    required = [normalize_column(column, "--require") for column in args.require]
    default_names = [name for name, _ in defaults]
    if "email" in default_names:
        raise UsageError("--default cannot target the email column.")

    input_path = Path(args.input)
    if not input_path.is_file():
        raise UsageError(f"input file not found: {input_path}")
    output_path = None
    if args.report_only:
        if args.output:
            tally.warnings.append("-o is ignored with --report-only. Nothing is written.")
    else:
        if not args.output:
            raise UsageError("-o OUTPUT.csv is required unless --report-only is given.")
        output_path = Path(args.output)
        if output_path.resolve() == input_path.resolve():
            raise UsageError("the output path is the same as the input. Choose a different -o so "
                             "the raw export is never overwritten.")

    text, encoding = read_export(input_path, tally.warnings)
    if not text.strip():
        raise UsageError("the file is empty.")
    delimiter = sniff_delimiter(text)
    records = parse_records(text, delimiter)
    if not records:
        raise UsageError("the file has no header row.")
    headers = [clean_cell(cell) for cell in records[0]]

    planned = plan_columns(headers, maps, tally)
    if args.keep:
        keep = {normalize_column(column, "--keep") for column in args.keep} | {"email"} | set(default_names)
        for name in required:
            if name not in keep:
                raise UsageError(f"--require column {name!r} must also be listed in --keep.")
        dropped = [name for _, name in planned if name not in keep]
        planned = [(index, name) for index, name in planned if name in keep]
        if dropped:
            tally.notes.append(f"not kept (--keep): {', '.join(dropped)}")
    planned_names = [name for _, name in planned]
    output_names = planned_names + [name for name in default_names if name not in planned_names]
    missing_required = [name for name in required if name not in output_names]
    required_present = [name for name in required if name in output_names]

    suppressed, suppression_note = load_suppressions(
        Path(args.suppressions) if args.suppressions else None, tally)

    stats = Counter()
    kept, extra_rows = process_rows(records, planned, defaults, required_present, suppressed,
                                    args.drop_role_accounts, tally, stats)

    blocked = bool(tally.blockers) or bool(missing_required) or not kept
    if not kept and not tally.blockers and not missing_required:
        tally.warnings.append("no rows survived the cleaning rules.")

    columns = ["email"] + sorted(name for name in output_names if name != "email")
    if not blocked and output_path is not None:
        write_output(output_path, columns, kept)

    print_report(input_path, encoding, delimiter, headers, tally.notes,
                 suppression_note, stats, tally, extra_rows, missing_required, len(kept),
                 output_path, blocked)
    return EXIT_BLOCKED if blocked else EXIT_CLEAN


def build_parser():
    parser = argparse.ArgumentParser(
        description="Normalize a contact export into a clean CSV for the email-mcp server. "
                    "The report goes to stderr; addresses in it are masked.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 clean, 1 blockers (no output written), 2 usage or format error.",
    )
    parser.add_argument("input", help="raw export CSV. This file is never modified.")
    parser.add_argument("-o", "--output", help="cleaned CSV to write (required unless --report-only)")
    parser.add_argument("--map", action="append", default=[], metavar="SOURCE=TARGET",
                        help="rename a source column to a target column (case-insensitive source)")
    parser.add_argument("--default", action="append", default=[], metavar="COLUMN=VALUE",
                        help="fill blank cells in COLUMN with VALUE; adds COLUMN if missing")
    parser.add_argument("--require", action="append", default=[], metavar="COLUMN",
                        help="rows with a blank COLUMN are blockers (the server would not refuse them)")
    parser.add_argument("--keep", action="append", default=[], metavar="COLUMN",
                        help="keep only these columns in the output (email is always kept). "
                             "Use it for data minimization.")
    parser.add_argument("--drop-role-accounts", action="store_true",
                        help="drop info@, noreply@, postmaster@ and similar role addresses")
    parser.add_argument("--suppressions", metavar="PATH",
                        help="suppression CSV with an email column; listed addresses are dropped")
    parser.add_argument("--report-only", action="store_true",
                        help="analyse and report without writing the output file")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except UsageError as exc:
        say(f"error: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
