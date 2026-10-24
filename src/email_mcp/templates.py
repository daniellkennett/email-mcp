"""HTML email templates: discovery, safe reading, placeholder rendering, plaintext fallback."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape as html_escape
from html.parser import HTMLParser

from email_mcp.config import Settings, safe_child_path

TEMPLATE_SUFFIXES = frozenset({".html", ".htm"})
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
SUBJECT_RE = re.compile(r"<!--\s*subject\s*:\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL)
SKIP_TAGS = frozenset({"head", "style", "script", "title"})
# Tags with no end tag, so they never open a nesting level.
VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input",
                       "link", "meta", "source", "track", "wbr"})
HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
# A loose match for anything that looks like a placeholder, used to catch the
# near-misses that PLACEHOLDER_RE deliberately ignores.
LOOSE_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
VALID_INNER_RE = re.compile(r"\s*[A-Za-z0-9_]+\s*\Z")
BLOCK_TAGS = frozenset({
    "p", "div", "tr", "table", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "blockquote", "hr",
})


class TemplateNotFound(Exception):
    """Template name is invalid or no such template exists."""


@dataclass(frozen=True)
class TemplateInfo:
    """Metadata about one template file."""

    name: str          # filename incl. .html
    path: str
    size_bytes: int
    variables: list[str]   # sorted unique {{var}} names found
    subject_hint: str | None
    modified: str      # ISO 8601


class TextExtractor(HTMLParser):
    """Collect readable text from HTML, dropping head/style/script content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.depth = 0
        self.hidden_at: int | None = None
        self.links: list[tuple[int, str]] = []  # (index into parts where link text starts, href)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track skipped regions, hidden subtrees, line breaks and link starts."""
        if tag not in VOID_TAGS:
            self.depth += 1
            if self.hidden_at is None:
                style = dict(attrs).get("style") or ""
                if HIDDEN_STYLE_RE.search(style):
                    self.hidden_at = self.depth
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag == "br" or tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.links.append((len(self.parts), href.strip()))

    def handle_endtag(self, tag: str) -> None:
        """Close skipped regions, line breaks and links (rendered as 'label (href)')."""
        if tag not in VOID_TAGS:
            if self.hidden_at is not None and self.depth <= self.hidden_at:
                self.hidden_at = None
            self.depth = max(self.depth - 1, 0)
        if tag in SKIP_TAGS:
            self.skip_depth = max(self.skip_depth - 1, 0)
        elif tag == "a" and self.links:
            start, href = self.links.pop()
            label = "".join(self.parts[start:]).strip()
            del self.parts[start:]
            if href and not href.startswith("#") and href != label:
                label = f"{label} ({href})" if label else href
            self.parts.append(label)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Keep text outside skipped regions, collapsing source whitespace."""
        if self.skip_depth == 0 and self.hidden_at is None:
            self.parts.append(re.sub(r"\s+", " ", data))


def find_variables(html: str) -> list[str]:
    """Return sorted unique placeholder names used in the HTML."""
    return sorted(set(PLACEHOLDER_RE.findall(html)))


def malformed_placeholders(html: str) -> list[str]:
    """Placeholder-looking spans PLACEHOLDER_RE will not match, such as '{{ first name }}'.

    Those are not substituted, so the braces would reach the recipient. Callers must refuse
    to send when this returns anything.
    """
    found = [match.group(0) for match in LOOSE_PLACEHOLDER_RE.finditer(html)
             if not VALID_INNER_RE.match(match.group(1))]
    return sorted(set(found))


def missing_variables(html: str, variables: Mapping[str, str]) -> list[str]:
    """Return sorted placeholder names used in the HTML but absent from variables."""
    return sorted(set(find_variables(html)) - set(variables))


def blank_variables(html: str, variables: Mapping[str, str]) -> list[str]:
    """Return placeholder names whose supplied value is empty or whitespace.

    Strict rendering only checks that a key exists, so a blank CSV cell renders as nothing:
    "Hi ," rather than "Hi Dana". Callers merging contact data must treat this as a data fault.
    """
    used = set(find_variables(html))
    return sorted(name for name in used
                  if name in variables and not str(variables[name]).strip())


def render(
    html: str, variables: Mapping[str, str], strict: bool = True, escape: bool = True
) -> str:
    """Substitute {{ var }} placeholders.

    strict raises ValueError listing missing names; otherwise they are blanked.
    escape HTML-escapes substituted values (template markup is untouched); use
    escape=False for plaintext targets such as subject lines.
    """
    missing = missing_variables(html, variables)
    if strict and missing:
        raise ValueError(f"Template is missing variables: {', '.join(missing)}")

    def substitute(match: re.Match[str]) -> str:
        value = str(variables.get(match.group(1), ""))
        return html_escape(value, quote=True) if escape else value

    return PLACEHOLDER_RE.sub(substitute, html)


def parse_subject_hint(html: str) -> str | None:
    """Return the text of an <!-- subject: ... --> comment, if present."""
    match = SUBJECT_RE.search(html)
    if match is None or not match.group(1).strip():
        return None
    return match.group(1).strip()


def html_to_text(html: str) -> str:
    """Convert HTML to a readable plaintext fallback."""
    extractor = TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = [" ".join(line.split()) for line in "".join(extractor.parts).split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return text.strip()


def list_templates(settings: Settings) -> list[TemplateInfo]:
    """List .html/.htm files in template_dir, sorted by name (non-recursive)."""
    folder = settings.template_dir
    if not folder.is_dir():
        return []
    infos: list[TemplateInfo] = []
    for path in sorted(folder.iterdir(), key=lambda entry: entry.name):
        if not path.is_file() or path.suffix.lower() not in TEMPLATE_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        stat = path.stat()
        infos.append(
            TemplateInfo(
                name=path.name,
                path=str(path),
                size_bytes=stat.st_size,
                variables=find_variables(text),
                subject_hint=parse_subject_hint(text),
                modified=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            )
        )
    return infos


def read_template(settings: Settings, name: str) -> str:
    """Read a template by filename, rejecting any name that escapes template_dir."""
    path = safe_child_path(settings.template_dir, name)
    if path is None:
        raise TemplateNotFound(f"Invalid template name {name!r}: use a plain filename.")
    if path.suffix.lower() not in TEMPLATE_SUFFIXES or not path.is_file():
        raise TemplateNotFound(f"No template named {name!r} in {settings.template_dir}.")
    return path.read_text(encoding="utf-8")
