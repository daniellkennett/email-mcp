"""Environment-driven settings for the email MCP engine."""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EMAIL_RE = re.compile(r"[^\s@<>,;:\"()\[\]]+@[^\s@]+\.[^\s@]+")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

# Mailbox hosts throttle outbound mail far below what a bulk relay allows, and overrunning
# the limit gets messages rejected partway through a campaign. Default to a figure that
# stays under it, so a minimal config is safe without naming the cap.
DEFAULT_HOURLY_CAP = 5000
IONOS_HOURLY_CAP = 400


class ConfigError(Exception):
    """Required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration."""

    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_starttls: bool          # True -> port 587 STARTTLS; False -> implicit SSL (465)
    from_email: str
    from_name: str
    reply_to: str | None
    template_dir: Path
    contacts_dir: Path
    state_dir: Path
    max_recipients_per_run: int
    max_messages_per_hour: int
    batch_size: int
    batch_pause_seconds: float
    unsubscribe_base_url: str | None
    dry_run_default: bool
    # Optional PEM bundle for SMTP TLS. None means use the OS trust store (see tls.py).
    ca_bundle: Path | None = None
    # Escape hatch for a seed send to an address you control, before an unsubscribe
    # page exists. Never leave this on for a campaign.
    allow_send_without_unsubscribe: bool = False


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines, skipping blanks and # comments."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def load_dotenv(path: Path) -> None:
    """Load a .env file into os.environ without overriding existing variables."""
    if not path.is_file():
        return
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        os.environ.setdefault(key, value)


def read_env(name: str) -> str | None:
    """Return a stripped env value, or None when unset or blank."""
    return os.environ.get(name, "").strip() or None


def require_env(name: str) -> str:
    """Return a required env value or raise ConfigError naming it."""
    value = read_env(name)
    if value is None:
        raise ConfigError(
            f"Missing required setting {name}. Add it to the \"env\" block of the "
            f"email-marketing entry in your Claude config:\n"
            f"    Claude Desktop: ~/Library/Application Support/Claude/"
            f"claude_desktop_config.json\n"
            f"    Claude Code:    ~/.claude.json\n"
            f"See claude_desktop_config.example.json for the full list of settings. "
            f"A {PROJECT_ROOT / '.env'} file still works as a fallback, but the "
            f"Claude config is the primary place now."
        )
    return value


def parse_bool(raw: str) -> bool:
    """Parse a true/false spelling; raises ValueError for anything else."""
    lowered = raw.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ValueError(raw)


def env_value[T](name: str, default: T, parse: Callable[[str], T], expected: str) -> T:
    """Return the env value parsed by `parse`, or `default` when unset or blank."""
    raw = read_env(name)
    if raw is None:
        return default
    try:
        return parse(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be {expected}, got {raw!r}.") from exc


def env_path(name: str, default_dir: str) -> Path:
    """Absolute, resolved directory from env; relative paths anchor at the project root."""
    raw = read_env(name)
    path = Path(raw).expanduser() if raw else Path(default_dir)
    return (PROJECT_ROOT / path).resolve()


def safe_child_path(root: Path, name: str) -> Path | None:
    """Return root/name if name is a plain filename resolving inside root, else None."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    try:
        root_resolved = root.resolve()
        candidate = (root_resolved / name).resolve()
    except (OSError, ValueError):
        return None
    if candidate.parent != root_resolved:
        return None
    return candidate


def default_hourly_cap(smtp_host: str) -> int:
    """Hourly send cap to assume when MAX_MESSAGES_PER_HOUR is not set."""
    return IONOS_HOURLY_CAP if "ionos" in smtp_host.lower() else DEFAULT_HOURLY_CAP


def load_settings() -> Settings:
    """Load settings from the environment and the project .env file."""
    load_dotenv(PROJECT_ROOT / ".env")

    unsubscribe_base = read_env("UNSUBSCRIBE_BASE_URL")
    if unsubscribe_base and not unsubscribe_base.lower().startswith(("http://", "https://")):
        raise ConfigError(
            f"UNSUBSCRIBE_BASE_URL must be a full web address starting with https://, "
            f"got {unsubscribe_base!r}. Leave it empty until you have an unsubscribe page: "
            f"an empty value safely blocks real sending, whereas a placeholder would put a "
            f"broken unsubscribe link into real email."
        )

    from_email = read_env("FROM_EMAIL") or "rose@starsupportus.com"
    if not EMAIL_RE.fullmatch(from_email):
        raise ConfigError(f"FROM_EMAIL must be an email address, got {from_email!r}.")

    bundle_raw = read_env("SMTP_CA_BUNDLE")
    ca_bundle = Path(bundle_raw).expanduser().resolve() if bundle_raw else None
    if ca_bundle is not None and not ca_bundle.is_file():
        raise ConfigError(f"SMTP_CA_BUNDLE does not point at a file: {ca_bundle}")

    smtp_host = require_env("SMTP_HOST")
    settings = Settings(
        smtp_host=smtp_host,
        smtp_port=env_value("SMTP_PORT", 587, int, "an integer"),
        smtp_user=require_env("SMTP_USER"),
        smtp_password=require_env("SMTP_PASSWORD"),
        smtp_starttls=env_value("SMTP_STARTTLS", True, parse_bool, "true or false"),
        from_email=from_email,
        from_name=read_env("FROM_NAME") or "Star Support",
        reply_to=read_env("REPLY_TO"),
        template_dir=env_path("TEMPLATE_DIR", "templates"),
        contacts_dir=env_path("CONTACTS_DIR", "contacts"),
        state_dir=env_path("STATE_DIR", "state"),
        max_recipients_per_run=env_value("MAX_RECIPIENTS_PER_RUN", 2000, int, "an integer"),
        max_messages_per_hour=env_value("MAX_MESSAGES_PER_HOUR", default_hourly_cap(smtp_host),
                                        int, "an integer"),
        batch_size=env_value("BATCH_SIZE", 50, int, "an integer"),
        batch_pause_seconds=env_value("BATCH_PAUSE_SECONDS", 2.0, float, "a number"),
        unsubscribe_base_url=unsubscribe_base,
        dry_run_default=env_value("DRY_RUN_DEFAULT", True, parse_bool, "true or false"),
        ca_bundle=ca_bundle,
        allow_send_without_unsubscribe=env_value(
            "ALLOW_SEND_WITHOUT_UNSUBSCRIBE", False, parse_bool, "true or false"),
    )

    for name, value in (
        ("MAX_RECIPIENTS_PER_RUN", settings.max_recipients_per_run),
        ("MAX_MESSAGES_PER_HOUR", settings.max_messages_per_hour),
        ("BATCH_SIZE", settings.batch_size),
    ):
        if value < 1:
            raise ConfigError(f"{name} must be at least 1, got {value}.")
    if settings.batch_pause_seconds < 0:
        raise ConfigError("BATCH_PAUSE_SECONDS must not be negative.")

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    return settings
