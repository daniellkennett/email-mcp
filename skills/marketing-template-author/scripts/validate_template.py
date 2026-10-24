"""Check HTML email templates against the email-marketing server's template contract.

Standalone: standard library only, and nothing is imported from the email_mcp project,
so it runs with the system python3 (3.9+) without the project virtualenv.

    python3 scripts/validate_template.py                    # the repo's templates/ directory
    python3 scripts/validate_template.py templates/x.html   # one or more files
    python3 scripts/validate_template.py path/to/dir        # non-recursive .html/.htm files

Each check prints PASS, FAIL or WARN. The exit status is 0 only when no check FAILs in any
file. Warnings never change the exit status.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

MAX_BYTES = 100 * 1024
SUBJECT_HINT_MAX_LINE = 20
MAX_SHOWN = 4
TEMPLATE_SUFFIXES = (".html", ".htm")
DEFAULT_DIR = Path(__file__).resolve().parents[3] / "templates"

# Shared brand palette (see references/email-html-constraints.md), plus pure white and black.
ALLOWED_COLORS = frozenset({
    "0F6B63", "1F2933", "4A5563", "E7F3F1", "E4E9EE", "EEF1F4", "134E48", "FFFFFF", "000000",
})
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
    "source", "track", "wbr",
})
FORBIDDEN_TAGS = frozenset({
    "button", "script", "form", "input", "select", "textarea", "iframe", "object", "embed",
    "video", "audio",
})

# Same placeholder reading as email_mcp.templates.PLACEHOLDER_RE, so the validator and the
# server agree on what counts as a placeholder.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
PLACEHOLDER_FULL_RE = re.compile(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}")
NAME_FULL_RE = re.compile(r"[A-Za-z0-9_]+")
BRACE_TOKEN_RE = re.compile(r"\{\{|\}\}")
SUBJECT_RE = re.compile(r"<!--\s*subject\s*:\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
COMMENT_BODY_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
HEX_RE = re.compile(r"(?<![&\w])#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})(?![0-9A-Za-z_])")
VARIABLE_HEADER_RE = re.compile(r"\s*Variables\b")
ENTRY_RE = re.compile(r"^\s+([A-Za-z0-9_]+)\s{2,}\S")


@dataclass
class Check:
    """One PASS, FAIL or WARN line for one file."""

    status: str
    name: str
    message: str


def line_of(text: str, offset: int) -> int:
    """1-based line number of a character offset."""
    return text.count("\n", 0, offset) + 1


def blank_comments(text: str) -> str:
    """Replace each HTML comment with spaces, keeping newlines so offsets and lines hold."""
    return COMMENT_RE.sub(lambda match: re.sub(r"[^\n]", " ", match.group(0)), text)


def problem_check(
    name: str, problems: List[Tuple[int, str]], ok_message: str, status: str = "FAIL"
) -> Check:
    """PASS with ok_message when there are no problems, otherwise one line naming the first few."""
    if not problems:
        return Check("PASS", name, ok_message)
    shown = "; ".join(f"line {line}: {message}" for line, message in problems[:MAX_SHOWN])
    if len(problems) > MAX_SHOWN:
        shown += f" (+{len(problems) - MAX_SHOWN} more)"
    return Check(status, name, shown)


def scan_braces(text: str) -> Tuple[Set[str], List[Tuple[int, str]], List[Tuple[int, str]], List[Tuple[int, str]]]:
    """Walk every {{ and }} in the raw text.

    Returns (valid names, unbalanced braces, nested or empty placeholders, invalid names).
    """
    names: Set[str] = set()
    unbalanced: List[Tuple[int, str]] = []
    shape: List[Tuple[int, str]] = []
    bad_names: List[Tuple[int, str]] = []
    open_at: Optional[Tuple[int, int]] = None  # (offset, line) of the open '{{'
    for match in BRACE_TOKEN_RE.finditer(text):
        line = line_of(text, match.start())
        if match.group(0) == "{{":
            if open_at is None:
                open_at = (match.start(), line)
            else:
                shape.append((line, "'{{' opens inside the placeholder started on line "
                              + str(open_at[1])))
        elif open_at is None:
            unbalanced.append((line, "'}}' with no matching '{{'"))
        else:
            start, start_line = open_at
            open_at = None
            inner = text[start + 2:match.start()]
            if "{{" in inner:
                continue  # already reported as nested
            stripped = inner.strip()
            if not stripped:
                shape.append((start_line, "empty placeholder '{{ }}'"))
            elif NAME_FULL_RE.fullmatch(stripped):
                names.add(stripped)
            else:
                bad_names.append((start_line, "'{{ " + stripped + " }}' is not [A-Za-z0-9_]+; "
                                  "the server leaves it in the email as literal text. Rename it"))
    if open_at is not None:
        unbalanced.append((open_at[1], "'{{' is never closed"))
    return names, unbalanced, shape, bad_names


class Scanner(HTMLParser):
    """Collect tags, meta, images, links and anchors, and track tag balance."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open_tags: List[Tuple[str, int]] = []
        self.tag_problems: List[Tuple[int, str]] = []
        self.seen_tags: List[Tuple[str, int]] = []
        self.metas: List[Dict[str, str]] = []
        self.imgs: List[Tuple[int, Dict[str, str]]] = []
        self.links: List[Tuple[int, Dict[str, str]]] = []
        self.anchors: List[Tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        """Record the tag and push it unless it is void."""
        line = self.getpos()[0]
        values = {name: (value or "") for name, value in attrs}
        self.seen_tags.append((tag, line))
        if tag == "meta":
            self.metas.append(values)
        elif tag == "img":
            self.imgs.append((line, values))
        elif tag == "link":
            self.links.append((line, values))
        elif tag == "a" and "href" in values:
            self.anchors.append((line, values["href"]))
        if tag not in VOID_TAGS:
            self.open_tags.append((tag, line))

    def handle_startendtag(self, tag, attrs):
        """Treat <tag/> as open then close, or as a lone void tag."""
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        """Match the end tag against the open stack and report what it crosses or lacks."""
        if tag in VOID_TAGS:
            return
        line = self.getpos()[0]
        names = [name for name, _ in self.open_tags]
        if tag not in names:
            self.tag_problems.append((line, f"</{tag}> has no matching open <{tag}>"))
            return
        index = len(names) - 1 - names[::-1].index(tag)
        for name, opened_line in self.open_tags[index + 1:]:
            self.tag_problems.append((opened_line, f"<{name}> opened on line {opened_line} "
                                      f"is not closed before </{tag}> on line {line}"))
        del self.open_tags[index:]

    def finish(self) -> None:
        """Report every tag still open at the end of the document."""
        for name, opened_line in self.open_tags:
            self.tag_problems.append((opened_line, f"<{name}> opened on line {opened_line} "
                                      "is never closed"))
        self.open_tags = []
        self.tag_problems.sort()


def scan_markup(text: str) -> Scanner:
    """Parse the whole document and return the collected scanner."""
    scanner = Scanner()
    scanner.feed(text)
    scanner.close()
    scanner.finish()
    return scanner


def variable_block(text: str) -> Optional[Tuple[List[str], List[str], int]]:
    """Read the first non-subject comment that has a 'Variables' header.

    Returns (documented names, names with no 'Example:' on their line, comment line) or None.
    """
    for match in COMMENT_BODY_RE.finditer(text):
        body = match.group(1)
        if body.strip().lower().startswith("subject"):
            continue
        if not any(VARIABLE_HEADER_RE.match(line) for line in body.splitlines()):
            continue
        documented: List[str] = []
        no_example: List[str] = []
        in_block = False
        for line in body.splitlines():
            if not in_block:
                in_block = bool(VARIABLE_HEADER_RE.match(line))
                continue
            entry = ENTRY_RE.match(line)
            if entry:
                documented.append(entry.group(1))
                if "Example:" not in line:
                    no_example.append(entry.group(1))
        return documented, no_example, line_of(text, match.start())
    return None


def subject_check(text: str) -> Check:
    """Exactly one single-line, non-empty subject hint, parsed the way the server parses it."""
    matches = list(SUBJECT_RE.finditer(text))
    if not matches:
        return Check("FAIL", "subject-hint", "no <!-- subject: ... --> comment; add one on line 1")
    if len(matches) > 1:
        lines = ", ".join(str(line_of(text, m.start())) for m in matches)
        return Check("FAIL", "subject-hint", f"{len(matches)} subject hints (lines {lines}); keep exactly one")
    match = matches[0]
    line = line_of(text, match.start())
    if "\n" in match.group(0):
        end = line_of(text, match.end())
        return Check("FAIL", "subject-hint", f"subject hint spans lines {line}-{end}; it must be one line")
    if not match.group(1).strip():
        return Check("FAIL", "subject-hint", f"subject hint on line {line} is empty")
    if line > SUBJECT_HINT_MAX_LINE:
        return Check("WARN", "subject-hint", f"on line {line}; put it near the top of the file "
                     f"(within the first {SUBJECT_HINT_MAX_LINE} lines)")
    return Check("PASS", "subject-hint", f"line {line}: {match.group(1).strip()!r}")


def check_file(path: Path) -> List[Check]:
    """Run every check against one template file."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return [Check("FAIL", "read", f"cannot read file: {exc}")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [Check("FAIL", "utf-8", f"file is not valid UTF-8 (byte {exc.start})")]

    checks: List[Check] = []
    stripped = blank_comments(text)  # same length and line breaks; comments blanked out

    # Placeholder syntax.
    names, unbalanced, shape, bad_names = scan_braces(text)
    used = set(PLACEHOLDER_RE.findall(text))
    checks.append(problem_check("braces-balanced", unbalanced,
                                f"{len(names)} distinct placeholder name(s), all braces paired"))
    checks.append(problem_check("no-nested-or-empty", shape, "no nested or empty placeholders"))
    checks.append(problem_check("placeholder-names", bad_names,
                                "every placeholder name matches [A-Za-z0-9_]+"))

    # Subject hint.
    checks.append(subject_check(text))

    # Document shape.
    body_opens = [line_of(stripped, m.start()) for m in re.finditer(r"<body[\s>]", stripped, re.I)]
    body_closes = [line_of(stripped, m.start()) for m in re.finditer(r"</body\s*>", stripped, re.I)]
    if len(body_opens) == 1 and len(body_closes) == 1:
        checks.append(Check("PASS", "body-tags", f"one <body> (line {body_opens[0]}) and one "
                            f"</body> (line {body_closes[0]})"))
    else:
        checks.append(Check("FAIL", "body-tags", f"found {len(body_opens)} <body> and "
                            f"{len(body_closes)} </body>; need exactly one of each, because the "
                            "server inserts its unsubscribe footer before </body>"))
    if re.search(r"<!doctype\s+html\b", stripped, re.I):
        checks.append(Check("PASS", "doctype", "<!doctype html> present"))
    else:
        checks.append(Check("FAIL", "doctype", "no <!doctype html> declaration"))

    scan = scan_markup(text)
    charset_found = any(
        "charset" in meta or "charset=" in meta.get("content", "").lower() for meta in scan.metas
    )
    checks.append(Check("PASS" if charset_found else "FAIL", "charset-meta",
                        "<meta charset> present" if charset_found
                        else "no <meta charset=\"utf-8\">"))
    viewport_found = any(meta.get("name", "").lower() == "viewport" for meta in scan.metas)
    checks.append(Check("PASS" if viewport_found else "FAIL", "viewport-meta",
                        "viewport meta present" if viewport_found
                        else "no <meta name=\"viewport\"> tag"))
    checks.append(problem_check("tag-balance", scan.tag_problems,
                                f"{len(scan.seen_tags)} tags, every opened tag is closed"))

    # Images and links.
    img_problems: List[Tuple[int, str]] = []
    for line, attrs in scan.imgs:
        alt = attrs.get("alt", "").strip()
        src = attrs.get("src", "").strip()
        if not alt:
            img_problems.append((line, "<img> has no non-empty alt text"))
        if not (src.startswith("https://") or PLACEHOLDER_FULL_RE.fullmatch(src)):
            img_problems.append((line, f"<img> src {src[:60]!r} is not an absolute https:// URL "
                                 "or a {{ placeholder }}"))
    checks.append(problem_check("img-alt-and-src", img_problems,
                                f"{len(scan.imgs)} image(s), each with alt text and an https src"))

    link_problems: List[Tuple[int, str]] = []
    for line, href in scan.anchors:
        value = href.strip()
        if value.lower().startswith(("https://", "mailto:", "tel:", "#")) or value.startswith("{{"):
            continue
        link_problems.append((line, f"link {value[:60]!r} is not absolute https://"))
    checks.append(problem_check("link-urls", link_problems,
                                f"{len(scan.anchors)} link(s) use https, mailto, tel or a placeholder",
                                status="WARN"))

    # Forbidden elements and CSS.
    checks.append(problem_check(
        "forbidden-elements",
        [(line, f"<{tag}> is not reliable in email clients; see the constraints reference")
         for tag, line in scan.seen_tags if tag in FORBIDDEN_TAGS],
        "no forms, buttons, scripts or media elements"))
    css_problems = [(line, "external stylesheet <link>; use inline style attributes")
                    for line, attrs in scan.links if "stylesheet" in attrs.get("rel", "").lower()]
    css_problems += [(line_of(stripped, m.start()), "@import loads external CSS; use inline styles")
                     for m in re.finditer(r"@import", stripped, re.I)]
    checks.append(problem_check("external-css", css_problems, "no external stylesheets or @import"))
    checks.append(problem_check(
        "web-fonts",
        [(line_of(stripped, m.start()), "@font-face; web fonts do not load reliably")
         for m in re.finditer(r"@font-face", stripped, re.I)],
        "no @font-face web fonts"))
    checks.append(problem_check(
        "layout-css",
        [(line_of(stripped, m.start()), "flexbox or grid is not supported; use nested tables")
         for m in re.finditer(r"display\s*:\s*(?:inline-)?(?:flex|grid)\b", stripped, re.I)],
        "no flexbox or grid layout"))

    # Compliance and preview.
    address = re.search(r"\{\{\s*company_address\s*\}\}", stripped)
    if address:
        checks.append(Check("PASS", "company-address",
                            f"{{{{ company_address }}}} on line {line_of(stripped, address.start())}"))
    else:
        checks.append(Check("FAIL", "company-address",
                            "no {{ company_address }} in the body; CAN-SPAM requires a postal "
                            "address and the server's footer does not supply one"))
    preheader = re.search(r"display\s*:\s*none[^>]*>\s*\{\{\s*preheader\s*\}\}", stripped, re.I)
    if preheader:
        checks.append(Check("PASS", "preheader",
                            f"hidden element holds {{{{ preheader }}}} on line "
                            f"{line_of(stripped, preheader.start())}"))
    else:
        checks.append(Check("FAIL", "preheader",
                            "no display:none element containing {{ preheader }}; without it the "
                            "inbox preview shows the first body text"))
    unsubscribe_lines = sorted({line_of(stripped, m.start())
                                for m in re.finditer(r"unsubscribe", stripped, re.I)})
    checks.append(problem_check(
        "no-author-unsubscribe",
        [(line, "unsubscribe text in the markup; the server adds its own footer before </body>, "
          "so remove this") for line in unsubscribe_lines],
        "no unsubscribe link in the markup (the server adds one)", status="WARN"))

    # Variables: the Variables block must match the placeholders actually used.
    block = variable_block(text)
    if block is None:
        checks.append(Check("FAIL", "variable-block", "no 'Variables' comment block; add one right "
                            "after the subject hint"))
    else:
        documented, no_example, block_line = block
        documented_set = set(documented)
        first_line: Dict[str, int] = {}
        for match in PLACEHOLDER_RE.finditer(text):
            first_line.setdefault(match.group(1), line_of(text, match.start()))
        checks.append(Check("PASS", "variable-block", f"Variables block on line {block_line} lists "
                            f"{len(documented_set)} name(s)"))
        checks.append(problem_check(
            "variables-documented",
            [(first_line[name], "{{ " + name + " }} is used but not listed in the Variables block")
             for name in sorted(used - documented_set)],
            "every placeholder used is documented"))
        checks.append(problem_check(
            "variables-unused",
            [(block_line, "'" + name + "' is listed in the Variables block but never used; remove it "
              "from the block or from the template") for name in sorted(documented_set - used)],
            "every documented variable is used"))
        checks.append(problem_check(
            "variable-examples",
            [(block_line, "'" + name + "' has no 'Example:' value") for name in no_example],
            "every documented variable has an example", status="WARN"))

    # Size and palette.
    size = len(raw)
    if size > MAX_BYTES:
        checks.append(Check("WARN", "file-size", f"{size / 1024:.1f} KB; Gmail clips messages "
                            "above about 100 KB"))
    else:
        checks.append(Check("PASS", "file-size", f"{size / 1024:.1f} KB (limit 100 KB)"))
    off_palette: List[Tuple[int, str]] = []
    color_uses = 0
    for match in HEX_RE.finditer(stripped):
        value = match.group(1).upper()
        if len(value) == 3:
            value = "".join(char * 2 for char in value)
        color_uses += 1
        if value not in ALLOWED_COLORS:
            off_palette.append((line_of(stripped, match.start()),
                                f"#{value} is outside the shared palette"))
    checks.append(problem_check("palette", off_palette,
                                f"{color_uses} color use(s), all in the shared palette",
                                status="WARN"))
    return checks


def collect(paths: List[str]) -> Tuple[List[Path], List[str]]:
    """Expand arguments into files (directories are non-recursive). Returns (files, missing)."""
    files: List[Path] = []
    missing: List[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(
                entry for entry in path.iterdir()
                if entry.is_file() and entry.suffix.lower() in TEMPLATE_SUFFIXES
            ))
        elif path.is_file():
            files.append(path)
        else:
            missing.append(raw)
    return list(dict.fromkeys(files)), missing


def main(argv: Optional[List[str]] = None) -> int:
    """Check each file, print a summary per file and a totals line, and return the exit status."""
    parser = argparse.ArgumentParser(
        description="Validate HTML email templates for the email-marketing MCP server.",
        epilog="Exit 0 only when no check FAILs in any file. Warnings do not change the exit status.",
    )
    parser.add_argument("paths", nargs="*", metavar="PATH",
                        help="template file(s) or directories; default: the repo's templates/")
    args = parser.parse_args(argv)
    paths = args.paths or [str(DEFAULT_DIR)]

    files, missing = collect(paths)
    totals = {"PASS": 0, "FAIL": 0, "WARN": 0}
    for name in missing:
        print(f"{name}\n  FAIL  path           no such file or directory")
        totals["FAIL"] += 1
    if not files and not missing:
        print(f"No .html or .htm files found in: {', '.join(paths)}")
        return 1

    for path in files:
        checks = check_file(path)
        print(f"\n{path}")
        counts = {"PASS": 0, "FAIL": 0, "WARN": 0}
        for check in checks:
            print(f"  {check.status:<4}  {check.name:<22} {check.message}")
            counts[check.status] += 1
        verdict = "FAIL" if counts["FAIL"] else "OK"
        print(f"  summary: {counts['PASS']} passed, {counts['FAIL']} failed, "
              f"{counts['WARN']} warning(s) -> {verdict}")
        for status, count in counts.items():
            totals[status] += count

    print(f"\nTOTAL: {len(files)} file(s), {totals['PASS']} PASS, {totals['FAIL']} FAIL, "
          f"{totals['WARN']} WARN -> {'FAIL' if totals['FAIL'] else 'OK'}")
    return 1 if totals["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
