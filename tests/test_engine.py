"""Offline tests for the engine layer: config, templates, contacts, audit, mailer."""

import dataclasses
import json
import os
import smtplib
import ssl
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import formataddr
from pathlib import Path
from unittest import mock

from email_mcp import audit, config, contacts, mailer, templates, tls
from email_mcp.config import ConfigError, Settings

PASSWORD = "hunter2-secret"


def make_settings(root: Path, **overrides: object) -> Settings:
    """Build Settings pointing at directories under root (created)."""
    values: dict[str, object] = {
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_user": "mailer",
        "smtp_password": PASSWORD,
        "smtp_starttls": True,
        "from_email": "rose@starsupportus.com",
        "from_name": "Star Support",
        "reply_to": "replies@example.test",
        "template_dir": root / "templates",
        "contacts_dir": root / "contacts",
        "state_dir": root / "state",
        "max_recipients_per_run": 2000,
        "max_messages_per_hour": 5000,
        "batch_size": 50,
        "batch_pause_seconds": 0.0,
        "unsubscribe_base_url": "https://example.test/unsub",
        "dry_run_default": True,
    }
    values.update(overrides)
    settings = Settings(**values)  # type: ignore[arg-type]
    for folder in (settings.template_dir, settings.contacts_dir, settings.state_dir):
        folder.mkdir(parents=True, exist_ok=True)
    return settings


class FakeSMTP:
    """Stand-in for smtplib.SMTP that records what would have been sent."""

    created: list["FakeSMTP"] = []
    refuse: set[str] = set()
    disconnect_on: set[str] = set()

    def __init__(self, host: str, port: int = 0, timeout: float | None = None) -> None:
        self.host = host
        self.logins: list[tuple[str, str]] = []
        self.sent: list = []
        self.closed = False
        FakeSMTP.created.append(self)

    def starttls(self, context: object = None) -> None:
        """No-op TLS upgrade."""

    def login(self, user: str, password: str) -> None:
        """Record the login attempt."""
        self.logins.append((user, password))

    def send_message(self, msg: object) -> dict:
        """Record delivery, or raise to simulate refusal / a dropped connection."""
        recipient = msg["To"]  # type: ignore[index]
        if recipient in FakeSMTP.refuse:
            raise smtplib.SMTPRecipientsRefused({recipient: (550, b"no such user")})
        if recipient in FakeSMTP.disconnect_on:
            FakeSMTP.disconnect_on.discard(recipient)
            raise smtplib.SMTPServerDisconnected("connection lost")
        self.sent.append(msg)
        return {}

    def quit(self) -> tuple[int, bytes]:
        """Mark the connection closed."""
        self.closed = True
        return (221, b"bye")

    def close(self) -> None:
        """Mark the connection closed."""
        self.closed = True


class RenderTests(unittest.TestCase):
    def test_strict_render_substitutes_with_inner_whitespace(self) -> None:
        html = "Hi {{ first_name }} / {{last_name}}"
        out = templates.render(html, {"first_name": "Ann", "last_name": "Lee"})
        self.assertEqual(out, "Hi Ann / Lee")

    def test_strict_render_lists_every_missing_variable(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            templates.render("{{ b }} {{ a }} {{ c }}", {"c": "1"})
        self.assertIn("a, b", str(ctx.exception))

    def test_non_strict_render_blanks_unknowns(self) -> None:
        out = templates.render("x{{ a }}y{{ b }}z", {"a": "1"}, strict=False)
        self.assertEqual(out, "x1yz")

    def test_values_are_html_escaped_by_default(self) -> None:
        value = 'Smith & Sons <script>"x"</script>'
        out = templates.render("<td>{{ company }}</td>", {"company": value})
        self.assertEqual(out, "<td>Smith &amp; Sons &lt;script&gt;&quot;x&quot;&lt;/script&gt;</td>")

    def test_escape_false_inserts_values_verbatim(self) -> None:
        value = 'Smith & Sons <b>"x"</b>'
        out = templates.render("Subject: {{ company }}", {"company": value}, escape=False)
        self.assertEqual(out, f"Subject: {value}")

    def test_missing_variables_and_find_variables(self) -> None:
        html = "{{ b }} {{ a }} {{ c }} {{ a }}"
        self.assertEqual(templates.find_variables(html), ["a", "b", "c"])
        self.assertEqual(templates.missing_variables(html, {"c": "1"}), ["a", "b"])


class TemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = make_settings(self.root)

    def test_subject_hint_is_case_insensitive_and_optional(self) -> None:
        (self.settings.template_dir / "promo.html").write_text(
            "<html><!-- Subject: Spring   offer --><body>{{ name }}</body></html>")
        (self.settings.template_dir / "plain.htm").write_text("<p>no hint {{ x }}</p>")
        (self.settings.template_dir / "notes.txt").write_text("ignored")
        infos = templates.list_templates(self.settings)
        self.assertEqual([info.name for info in infos], ["plain.htm", "promo.html"])
        by_name = {info.name: info for info in infos}
        self.assertIsNone(by_name["plain.htm"].subject_hint)
        self.assertEqual(by_name["promo.html"].subject_hint, "Spring   offer")
        self.assertEqual(by_name["promo.html"].variables, ["name"])
        self.assertEqual(templates.parse_subject_hint("<!--SUBJECT:Hi-->"), "Hi")

    def test_read_template_rejects_path_traversal(self) -> None:
        (self.root / "secret.html").write_text("secret")
        for name in ("../secret.html", "sub/../../secret.html", "..\\secret.html",
                     "/etc/passwd", "sub/x.html", ""):
            with self.subTest(name=name), self.assertRaises(templates.TemplateNotFound):
                templates.read_template(self.settings, name)

    def test_read_template_rejects_symlink_escaping_template_dir(self) -> None:
        outside = self.root / "outside.html"
        outside.write_text("secret")
        os.symlink(outside, self.settings.template_dir / "link.html")
        with self.assertRaises(templates.TemplateNotFound):
            templates.read_template(self.settings, "link.html")

    def test_read_template_returns_content(self) -> None:
        (self.settings.template_dir / "ok.html").write_text("<p>{{ a }}</p>")
        self.assertEqual(templates.read_template(self.settings, "ok.html"), "<p>{{ a }}</p>")
        with self.assertRaises(templates.TemplateNotFound):
            templates.read_template(self.settings, "missing.html")


class HtmlToTextTests(unittest.TestCase):
    def test_basics(self) -> None:
        html = (
            "<html><head><style>p{color:red}</style><title>Title</title></head><body>"
            "<p>Hello &amp; welcome</p><script>alert(1)</script>"
            "<div>Line <a href='https://ex.test/x'>here</a></div>"
            "<br>After<br><br><br><br>End</body></html>"
        )
        text = templates.html_to_text(html)
        self.assertIn("Hello & welcome", text)
        self.assertIn("Line here (https://ex.test/x)", text)
        self.assertIn("After", text)
        self.assertTrue(text.endswith("End"))
        for hidden in ("color", "alert", "Title"):
            self.assertNotIn(hidden, text)
        self.assertNotIn("\n\n\n", text)


class ConfigTests(unittest.TestCase):
    def test_parse_dotenv(self) -> None:
        text = (
            "# comment\n"
            "SMTP_HOST=smtp.example.test\n"
            'SMTP_USER="quoted user"\n'
            "export SMTP_PASSWORD='pw # not a comment'\n"
            "FROM_NAME=Star Support  # inline comment\n"
            "garbage line\n"
        )
        parsed = config.parse_dotenv(text)
        self.assertEqual(parsed["SMTP_HOST"], "smtp.example.test")
        self.assertEqual(parsed["SMTP_USER"], "quoted user")
        self.assertEqual(parsed["SMTP_PASSWORD"], "pw # not a comment")
        self.assertEqual(parsed["FROM_NAME"], "Star Support")
        self.assertNotIn("garbage line", parsed)

    def test_load_settings_reads_dotenv_without_overriding_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text(
                "SMTP_HOST=from-dotenv.test\nSMTP_USER=u\nSMTP_PASSWORD=p\nSMTP_PORT=465\n")
            with mock.patch.object(config, "PROJECT_ROOT", root), \
                    mock.patch.dict(os.environ, {"SMTP_HOST": "from-env.test"}, clear=True):
                settings = config.load_settings()
                self.assertEqual(settings.smtp_host, "from-env.test")
                self.assertEqual(settings.smtp_port, 465)
                self.assertTrue(settings.state_dir.is_dir())
                self.assertTrue(settings.state_dir.is_absolute())

    def test_load_settings_names_missing_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(config, "PROJECT_ROOT", Path(tmp)), \
                mock.patch.dict(os.environ, {"SMTP_HOST": "h", "SMTP_USER": "u"}, clear=True):
            with self.assertRaises(ConfigError) as ctx:
                config.load_settings()
            self.assertIn("SMTP_PASSWORD", str(ctx.exception))

    def test_load_settings_rejects_bad_from_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(config, "PROJECT_ROOT", Path(tmp)), \
                mock.patch.dict(os.environ, {"SMTP_HOST": "h", "SMTP_USER": "u",
                                             "SMTP_PASSWORD": "p", "FROM_EMAIL": "nope"},
                                clear=True):
            with self.assertRaises(ConfigError) as ctx:
                config.load_settings()
            self.assertIn("FROM_EMAIL", str(ctx.exception))


class ContactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = make_settings(self.root)

    def write_csv(self, name: str, text: str) -> None:
        (self.settings.contacts_dir / name).write_text(text, encoding="utf-8")

    def test_missing_email_column_raises_with_headers(self) -> None:
        self.write_csv("bad.csv", "Name,Company\nAnn,Acme\n")
        with self.assertRaises(ValueError) as ctx:
            contacts.load_contact_list(self.settings, "bad")
        self.assertIn("bad.csv", str(ctx.exception))
        self.assertIn("Name", str(ctx.exception))

    def test_invalid_and_duplicate_rows_are_recorded(self) -> None:
        self.write_csv(
            "list.csv",
            " Email , First Name \n"
            "Ann@Example.test , Ann\n"
            "not-an-email, Bob\n"
            "  ,Blank\n"
            "ann@example.TEST,Dup\n"
            "carl@example.test,Carl\n",
        )
        loaded = contacts.load_contact_list(self.settings, "list.csv")
        self.assertEqual(loaded.name, "list")
        self.assertEqual(loaded.count, 2)
        self.assertEqual([c.email for c in loaded.contacts],
                         ["ann@example.test", "carl@example.test"])
        self.assertEqual(loaded.contacts[0].fields["first name"], "Ann")
        self.assertEqual(loaded.columns, ["email", "first name"])
        reasons = {(entry["row"], entry["reason"]) for entry in loaded.invalid}
        self.assertEqual(reasons, {(3, "invalid email"), (4, "blank email"),
                                   (5, "duplicate email")})

    def test_list_names_accept_with_or_without_extension(self) -> None:
        self.write_csv("people.csv", "email\na@example.test\n")
        self.assertEqual(contacts.load_contact_list(self.settings, "people.csv").count, 1)
        self.assertEqual(contacts.load_contact_list(self.settings, "people").count, 1)
        listing = contacts.list_contact_lists(self.settings)
        self.assertEqual(listing[0]["name"], "people")
        self.assertEqual(listing[0]["count"], 1)

    def test_contact_list_traversal_rejected(self) -> None:
        (self.root / "secret.csv").write_text("email\nx@example.test\n")
        for name in ("../secret", "../secret.csv", "sub/people", "..\\secret"):
            with self.subTest(name=name), self.assertRaises(contacts.ContactListNotFound):
                contacts.load_contact_list(self.settings, name)

    def test_suppression_add_idempotent_and_load(self) -> None:
        store = contacts.suppression_path(self.settings)
        self.assertFalse(store.exists() and store.read_text().strip() != "email,reason,added_at")
        self.assertEqual(contacts.load_suppressions(self.settings), set())
        self.assertEqual(store.read_text().splitlines()[0], "email,reason,added_at")
        self.assertTrue(contacts.add_suppression(self.settings, "Opt@Out.test", "unsubscribed"))
        self.assertFalse(contacts.add_suppression(self.settings, "opt@out.test", "again"))
        self.assertEqual(contacts.load_suppressions(self.settings), {"opt@out.test"})
        with self.assertRaises(ValueError):
            contacts.add_suppression(self.settings, "not an address")


class UnsubscribeAndComplianceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))

    def test_unsubscribe_url_without_query(self) -> None:
        self.assertEqual(mailer.unsubscribe_url(self.settings, "a+b@ex.test"),
                         "https://example.test/unsub?email=a%2Bb%40ex.test")

    def test_unsubscribe_url_with_existing_query(self) -> None:
        settings = make_settings(Path(self.tmp.name), unsubscribe_base_url="https://x.test/u?list=1")
        self.assertEqual(mailer.unsubscribe_url(settings, "a@ex.test"),
                         "https://x.test/u?list=1&email=a%40ex.test")

    def test_unsubscribe_url_none_without_base(self) -> None:
        settings = make_settings(Path(self.tmp.name), unsubscribe_base_url=None)
        self.assertIsNone(mailer.unsubscribe_url(settings, "a@ex.test"))
        self.assertEqual(mailer.apply_compliance(settings, "<body>x</body>", "a@ex.test"),
                         ("<body>x</body>", {}))

    def test_apply_compliance_inserts_footer_before_body_close(self) -> None:
        html, headers = mailer.apply_compliance(self.settings, "<html><body>Hi</body></html>",
                                                "a@ex.test")
        self.assertLess(html.index("Unsubscribe"), html.index("</body>"))
        self.assertEqual(headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertTrue(headers["List-Unsubscribe"].startswith("<https://example.test/unsub?"))


class BuildMimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))

    def test_structure_and_headers(self) -> None:
        html, headers = mailer.apply_compliance(self.settings, "<html><body><p>Hello</p></body></html>",
                                                "ann@ex.test")
        message = mailer.Message(to_email="ann@ex.test", subject="Hi", html=html, headers=headers)
        mime = mailer.build_mime(self.settings, message)

        self.assertTrue(mime.is_multipart())
        self.assertEqual(mime.get_content_type(), "multipart/alternative")
        plain = mime.get_body(preferencelist=("plain",))
        rich = mime.get_body(preferencelist=("html",))
        self.assertIsNotNone(plain)
        self.assertIsNotNone(rich)
        self.assertIn("Hello", plain.get_content())
        self.assertIn("Hello", rich.get_content())
        self.assertEqual(mime["From"], formataddr(("Star Support", "rose@starsupportus.com")))
        self.assertEqual(mime["To"], "ann@ex.test")
        self.assertEqual(mime["Subject"], "Hi")
        self.assertEqual(mime["Reply-To"], "replies@example.test")
        self.assertTrue(mime["Message-ID"].endswith("@starsupportus.com>"))
        self.assertIsNotNone(mime["Date"])
        self.assertIn("<https://example.test/unsub?", mime["List-Unsubscribe"])
        self.assertEqual(mime["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_message_headers_override_compliance_headers(self) -> None:
        message = mailer.Message(to_email="a@ex.test", subject="S", html="<p>x</p>",
                                 headers={"X-Campaign": "spring", "Reply-To": "other@ex.test"})
        mime = mailer.build_mime(self.settings, message)
        self.assertEqual(mime.get_all("Reply-To"), ["other@ex.test"])
        self.assertEqual(mime["X-Campaign"], "spring")


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))

    def test_log_event_stamps_ts_and_survives_malformed_records(self) -> None:
        audit.log_event(self.settings, {"event": "x", "payload": object()})
        audit.log_event(self.settings, {("tuple", "key"): 1})  # non-str key
        audit.log_event(self.settings, "not a dict")  # type: ignore[arg-type]
        records = audit.read_log(self.settings, limit=10)
        self.assertEqual(len(records), 3)
        self.assertIn("ts", records[2])
        self.assertIn("repr", records[1])  # fallback for the non-str key case
        self.assertIn("payload", records[2])

    def test_read_log_skips_corrupt_lines_most_recent_first(self) -> None:
        path = audit.log_path(self.settings)
        path.write_text(
            json.dumps({"event": "a", "n": 1}) + "\n"
            "{not json\n"
            "\n"
            + json.dumps({"event": "b", "n": 2}) + "\n"
            "[1, 2, 3]\n"
        )
        self.assertEqual([r["n"] for r in audit.read_log(self.settings)], [2, 1])
        self.assertEqual(len(audit.read_log(self.settings, limit=1)), 1)


class SendBulkDryRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name), unsubscribe_base_url=None)
        contacts.add_suppression(self.settings, "blocked@ex.test", "unsubscribed")

    def message(self, address: str) -> mailer.Message:
        return mailer.Message(to_email=address, subject="S", html="<p>Hi</p>")

    def test_dry_run_accounting_and_no_connection(self) -> None:
        batch = [
            self.message("ok@ex.test"),
            self.message("Blocked@ex.test"),
            self.message("not-an-address"),
            self.message("OK@ex.test"),
            self.message("ann@ex.test"),
        ]
        with mock.patch("smtplib.SMTP") as smtp_cls, \
                mock.patch("smtplib.SMTP_SSL") as ssl_cls:
            result = mailer.send_bulk(self.settings, batch, dry_run=True)
        smtp_cls.assert_not_called()
        ssl_cls.assert_not_called()

        self.assertTrue(result.dry_run)
        self.assertEqual((result.total, result.sent, result.skipped, result.failed),
                         (5, 2, 3, 0))
        statuses = [(d["email"], d["status"], d["reason"]) for d in result.details]
        self.assertEqual(statuses, [
            ("ok@ex.test", "sent", "dry_run"),
            ("blocked@ex.test", "skipped", "suppressed"),
            ("not-an-address", "skipped", "invalid_email"),
            ("ok@ex.test", "skipped", "duplicate_in_batch"),
            ("ann@ex.test", "sent", "dry_run"),
        ])

        records = audit.read_log(self.settings, limit=10)
        self.assertEqual(records[0]["event"], "run")
        self.assertEqual(records[0]["warnings"], ["no_unsubscribe_url"])
        self.assertEqual(sum(1 for r in records if r["event"] == "send"), 5)
        self.assertEqual(audit.hourly_count(self.settings), 0)  # dry runs never count

    def test_over_cap_raises_send_error_before_connecting(self) -> None:
        settings = make_settings(Path(self.tmp.name), max_recipients_per_run=2)
        with mock.patch("smtplib.SMTP") as smtp_cls, \
                self.assertRaises(mailer.SendError) as ctx:
            mailer.send_bulk(settings, [self.message(f"u{i}@ex.test") for i in range(3)],
                             dry_run=False)
        smtp_cls.assert_not_called()
        self.assertIn("3", str(ctx.exception))
        self.assertIn("2", str(ctx.exception))

    def test_empty_batch_raises_send_error(self) -> None:
        with self.assertRaises(mailer.SendError):
            mailer.send_bulk(self.settings, [], dry_run=True)

    def test_rate_limit_from_seeded_send_log(self) -> None:
        settings = make_settings(Path(self.tmp.name), max_messages_per_hour=3)
        now = datetime.now(UTC)
        seeded = [
            {"event": "send", "dry_run": False, "status": "sent", "ts": now.isoformat()},
            {"event": "send", "dry_run": False, "status": "sent",
             "ts": (now - timedelta(minutes=10)).isoformat()},
            {"event": "send", "dry_run": False, "status": "failed",
             "ts": now.isoformat()},  # does not count
            {"event": "send", "dry_run": False, "status": "sent",
             "ts": (now - timedelta(hours=2)).isoformat()},  # outside window
            {"event": "send", "dry_run": True, "status": "sent",
             "ts": now.isoformat()},  # dry run, does not count
            {"event": "run", "dry_run": False, "status": "sent", "ts": now.isoformat()},
        ]
        audit.log_path(settings).write_text(
            "\n".join(json.dumps(r) for r in seeded) + "\nbroken{\n")
        self.assertEqual(audit.hourly_count(settings), 2)

        with mock.patch("smtplib.SMTP") as smtp_cls:
            with self.assertRaises(mailer.RateLimitExceeded) as ctx:
                mailer.send_bulk(settings, [self.message("a@ex.test"), self.message("b@ex.test")],
                                 dry_run=False)
            smtp_cls.assert_not_called()
        self.assertIn("1 remaining", str(ctx.exception))

        # A dry run is not subject to the hourly cap.
        result = mailer.send_bulk(settings, [self.message("a@ex.test"), self.message("b@ex.test")],
                                  dry_run=True)
        self.assertEqual(result.sent, 2)


class SendBulkLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name), batch_size=2)
        FakeSMTP.created = []
        FakeSMTP.refuse = set()
        FakeSMTP.disconnect_on = set()
        patcher = mock.patch("smtplib.SMTP", FakeSMTP)
        patcher.start()
        self.addCleanup(patcher.stop)

    def message(self, address: str) -> mailer.Message:
        return mailer.Message(to_email=address, subject="S", html="<html><body>Hi</body></html>")

    def test_single_connection_and_per_recipient_refusal(self) -> None:
        FakeSMTP.refuse = {"bob@ex.test"}
        batch = [self.message(f"{name}@ex.test") for name in ("ann", "bob", "carl")]
        progress_calls: list[tuple[int, int]] = []

        result = mailer.send_bulk(self.settings, batch, dry_run=False,
                                  progress=lambda done, total: progress_calls.append((done, total)))

        self.assertEqual((result.sent, result.failed, result.skipped), (2, 1, 0))
        self.assertEqual(len(FakeSMTP.created), 1)
        connection = FakeSMTP.created[0]
        self.assertEqual(connection.logins, [("mailer", PASSWORD)])
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.sent), 2)
        self.assertEqual(progress_calls, [(1, 3), (2, 3), (3, 3)])
        self.assertIn("550", result.details[1]["reason"])

        records = audit.read_log(self.settings, limit=10)
        self.assertEqual(records[0]["event"], "run")
        self.assertEqual(sum(1 for r in records if r["event"] == "send" and not r["dry_run"]), 3)
        self.assertNotIn(PASSWORD, audit.log_path(self.settings).read_text())

    def test_connection_drop_reconnects_and_continues(self) -> None:
        FakeSMTP.disconnect_on = {"bob@ex.test"}
        batch = [self.message(f"{name}@ex.test") for name in ("ann", "bob", "carl")]
        result = mailer.send_bulk(self.settings, batch, dry_run=False)
        self.assertEqual((result.sent, result.failed), (2, 1))
        self.assertEqual(result.details[1]["status"], "failed")
        self.assertEqual(len(FakeSMTP.created), 2)
        self.assertTrue(all(conn.closed for conn in FakeSMTP.created))
        self.assertEqual(len(FakeSMTP.created[1].sent), 1)

    def test_suppressed_recipients_never_reach_smtp(self) -> None:
        contacts.add_suppression(self.settings, "ann@ex.test", "unsubscribed")
        result = mailer.send_bulk(self.settings, [self.message("ann@ex.test")], dry_run=False)
        self.assertEqual((result.sent, result.skipped), (0, 1))
        self.assertEqual(FakeSMTP.created, [])

    def test_unsubscribe_headers_reach_the_wire(self) -> None:
        mailer.send_bulk(self.settings, [self.message("ann@ex.test")], dry_run=False)
        sent = FakeSMTP.created[0].sent[0]
        self.assertIn("List-Unsubscribe", sent)
        self.assertIn("email=ann%40ex.test", sent["List-Unsubscribe"])


class BlankVariableTests(unittest.TestCase):
    """A supplied-but-empty value passes strict rendering and renders as nothing."""

    def test_blank_variables_found_and_missing_unaffected(self) -> None:
        html = "<p>Hi {{ first_name }} at {{ company }}</p>"
        supplied = {"first_name": "   ", "company": "Acme"}
        self.assertEqual(templates.blank_variables(html, supplied), ["first_name"])
        self.assertEqual(templates.missing_variables(html, supplied), [])

    def test_blank_value_renders_empty_under_strict(self) -> None:
        # Documents the behaviour that makes blank_variables necessary.
        html = "<p>Hi {{ first_name }}</p>"
        self.assertEqual(templates.render(html, {"first_name": ""}, strict=True), "<p>Hi </p>")

    def test_absent_key_is_not_reported_as_blank(self) -> None:
        html = "<p>{{ a }}{{ b }}</p>"
        self.assertEqual(templates.blank_variables(html, {"a": "x"}), [])


class DryRunValidationTests(unittest.TestCase):
    """A dry run must build the MIME message and flag the hourly cap."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))

    def test_dry_run_reports_build_failure_without_connecting(self) -> None:
        bad = mailer.Message(to_email="a@ex.test", subject="S\nInjected: evil", html="<p>x</p>")
        with mock.patch.object(mailer.smtplib, "SMTP") as smtp:
            result = mailer.send_bulk(self.settings, [bad], dry_run=True)
        smtp.assert_not_called()
        self.assertEqual((result.sent, result.failed), (0, 1))
        self.assertIn("message build failed", result.details[0]["reason"])

    def test_clean_dry_run_still_passes(self) -> None:
        good = mailer.Message(to_email="a@ex.test", subject="Clean", html="<p>x</p>")
        result = mailer.send_bulk(self.settings, [good], dry_run=True)
        self.assertEqual((result.sent, result.failed), (1, 0))

    def test_dry_run_warns_when_hourly_cap_would_be_exceeded(self) -> None:
        settings = make_settings(Path(self.tmp.name) / "capped", max_messages_per_hour=2)
        for index in range(2):
            audit.log_event(settings, {"event": "send", "dry_run": False,
                                       "status": "sent", "email": f"x{index}@ex.test"})
        result = mailer.send_bulk(
            settings, [mailer.Message("a@ex.test", "S", "<p>x</p>")], dry_run=True)
        self.assertTrue(any(w.startswith("would_exceed_hourly_cap") for w in result.warnings))

    def test_dry_run_is_silent_when_within_cap(self) -> None:
        result = mailer.send_bulk(
            self.settings, [mailer.Message("a@ex.test", "S", "<p>x</p>")], dry_run=True)
        self.assertEqual(
            [w for w in result.warnings if w.startswith("would_exceed")], [])


class MalformedPlaceholderTests(unittest.TestCase):
    """A placeholder the substitution regex ignores would ship braces to the recipient."""

    def test_detects_near_misses(self) -> None:
        html = "{{ a-b }} {{ ok }} {{ two words }} {{}} {{ fine_name }}"
        self.assertEqual(templates.malformed_placeholders(html),
                         ["{{ a-b }}", "{{ two words }}", "{{}}"])

    def test_clean_template_reports_nothing(self) -> None:
        self.assertEqual(templates.malformed_placeholders("<p>{{ first_name }}</p>"), [])

    def test_malformed_name_is_not_substituted(self) -> None:
        # The reason this matters: render leaves it as literal text.
        out = templates.render("<p>{{ first-name }}</p>", {"first-name": "Dana"}, strict=True)
        self.assertIn("{{ first-name }}", out)


class HiddenTextTests(unittest.TestCase):
    """The preheader is display:none and must not open the plaintext part."""

    def test_hidden_subtree_is_dropped(self) -> None:
        html = ('<body><div style="display:none;max-height:0">PREHEADER</div>'
                '<p>Visible</p></body>')
        self.assertEqual(templates.html_to_text(html), "Visible")

    def test_nested_hidden_content_is_dropped(self) -> None:
        html = '<body><div style="display: none"><span>DEEP</span> tail</div><p>Shown</p></body>'
        text = templates.html_to_text(html)
        self.assertNotIn("DEEP", text)
        self.assertNotIn("tail", text)
        self.assertIn("Shown", text)

    def test_visible_content_after_hidden_block_survives(self) -> None:
        html = '<body><div style="display:none">X</div><p>One</p><p>Two</p></body>'
        self.assertEqual(templates.html_to_text(html), "One\n\nTwo")


class SuppressionRecordTests(unittest.TestCase):
    """Mirroring opt-outs back to a source system needs reason and timestamp, not just the set."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))

    def test_records_are_newest_first_with_reasons(self) -> None:
        contacts.add_suppression(self.settings, "first@ex.test", "replied STOP")
        contacts.add_suppression(self.settings, "second@ex.test", "odoo blacklist")
        records = contacts.load_suppression_records(self.settings)
        self.assertEqual([r["email"] for r in records], ["second@ex.test", "first@ex.test"])
        self.assertEqual(records[0]["reason"], "odoo blacklist")
        self.assertTrue(records[0]["added_at"])

    def test_missing_store_is_empty_not_an_error(self) -> None:
        self.assertEqual(contacts.load_suppression_records(self.settings), [])


class HourlyCapDefaultTests(unittest.TestCase):
    """A mailbox host throttles far below a bulk relay, so the default must follow the host."""

    def test_ionos_host_gets_the_mailbox_cap(self) -> None:
        for host in ("smtp.ionos.com", "SMTP.IONOS.CO.UK", "smtp.ionos.de"):
            self.assertEqual(config.default_hourly_cap(host), 400, host)

    def test_relay_hosts_keep_the_bulk_default(self) -> None:
        for host in ("smtp.mailgun.org", "smtp.sendgrid.net"):
            self.assertEqual(config.default_hourly_cap(host), 5000, host)

    def test_explicit_setting_still_wins(self) -> None:
        env = {"SMTP_HOST": "smtp.ionos.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
               "MAX_MESSAGES_PER_HOUR": "50", "STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertEqual(config.load_settings().max_messages_per_hour, 50)

    def test_ionos_cap_applies_through_load_settings(self) -> None:
        env = {"SMTP_HOST": "smtp.ionos.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
               "STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MAX_MESSAGES_PER_HOUR", None)
            self.assertEqual(config.load_settings().max_messages_per_hour, 400)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)


class UnsubscribeUrlValidationTests(unittest.TestCase):
    """A placeholder must never become a broken unsubscribe link in real mail."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = {"SMTP_HOST": "smtp.ionos.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
                     "STATE_DIR": self.tmp.name}

    def test_non_url_value_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {**self.base, "UNSUBSCRIBE_BASE_URL": "FILL_IN"}):
            with self.assertRaises(config.ConfigError) as caught:
                config.load_settings()
        self.assertIn("https://", str(caught.exception))

    def test_empty_value_is_allowed_and_means_unset(self) -> None:
        with mock.patch.dict(os.environ, {**self.base, "UNSUBSCRIBE_BASE_URL": ""}):
            self.assertIsNone(config.load_settings().unsubscribe_base_url)

    def test_real_url_is_kept(self) -> None:
        url = "https://starsupportus.com/unsubscribe"
        with mock.patch.dict(os.environ, {**self.base, "UNSUBSCRIBE_BASE_URL": url}):
            self.assertEqual(config.load_settings().unsubscribe_base_url, url)


class TlsTrustTests(unittest.TestCase):
    """Python ignores the macOS Keychain, which breaks SMTP behind a TLS-inspecting proxy."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))

    def test_os_trust_store_is_the_default(self) -> None:
        self.assertEqual(tls.trust_source(self.settings), "os_trust_store")
        self.assertIsInstance(tls.tls_context(self.settings), ssl.SSLContext)

    def test_explicit_ca_bundle_wins(self) -> None:
        bundle = Path(self.tmp.name) / "corp.pem"
        bundle.write_bytes(Path(ssl.get_default_verify_paths().cafile).read_bytes())
        settings = dataclasses.replace(self.settings, ca_bundle=bundle)
        self.assertEqual(tls.trust_source(settings), f"ca_bundle:{bundle}")
        self.assertIsInstance(tls.tls_context(settings), ssl.SSLContext)

    def test_verification_is_never_disabled(self) -> None:
        for settings in (self.settings,
                         dataclasses.replace(self.settings, ca_bundle=None)):
            context = tls.tls_context(settings)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)

    def test_missing_ca_bundle_is_a_config_error(self) -> None:
        env = {"SMTP_HOST": "smtp.ionos.com", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
               "SMTP_CA_BUNDLE": str(Path(self.tmp.name) / "nope.pem"),
               "STATE_DIR": self.tmp.name}
        with mock.patch.dict(os.environ, env, clear=False):
            with self.assertRaises(config.ConfigError):
                config.load_settings()


class FooterInsertionTests(unittest.TestCase):
    """lower() can change string length, so an index from it can corrupt the markup."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))

    def test_length_changing_character_does_not_corrupt_the_close_tag(self) -> None:
        # U+0130 lowercases to two code points, shifting every later index.
        html = "<html><body>\u0130<p>x</p></body></html>"
        out, _ = mailer.apply_compliance(self.settings, html, "a@ex.test")
        self.assertTrue(out.endswith("</body></html>"))
        self.assertNotIn("</div>/body>", out)

    def test_uppercase_close_tag_is_matched(self) -> None:
        out, _ = mailer.apply_compliance(self.settings, "<BODY>x</BODY>", "a@ex.test")
        self.assertIn("Unsubscribe", out)
        self.assertTrue(out.rstrip().endswith("</BODY>"))

    def test_missing_close_tag_appends(self) -> None:
        out, _ = mailer.apply_compliance(self.settings, "<p>x</p>", "a@ex.test")
        self.assertIn("Unsubscribe", out)


class SuppressionEncodingTests(unittest.TestCase):
    """A BOM-prefixed store still filtered sends but exported nothing to mirror back."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = make_settings(Path(self.tmp.name))

    def test_bom_prefixed_store_is_still_exportable(self) -> None:
        path = contacts.suppression_path(self.settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("email,reason,added_at\ngone@ex.test,odoo,2026-01-01T00:00:00+00:00\n",
                        encoding="utf-8-sig")
        self.assertEqual(contacts.load_suppressions(self.settings), {"gone@ex.test"})
        records = contacts.load_suppression_records(self.settings)
        self.assertEqual([r["email"] for r in records], ["gone@ex.test"])


class UnsubscribeGateTests(unittest.TestCase):
    """Sending marketing mail with no unsubscribe mechanism is refused, not warned about."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def message(self) -> mailer.Message:
        return mailer.Message(to_email="a@ex.test", subject="S", html="<p>x</p>")

    def test_real_send_refused_without_unsubscribe_url(self) -> None:
        settings = make_settings(Path(self.tmp.name), unsubscribe_base_url=None)
        with mock.patch.object(mailer.smtplib, "SMTP") as smtp:
            with self.assertRaises(mailer.SendError) as caught:
                mailer.send_bulk(settings, [self.message()], dry_run=False)
        smtp.assert_not_called()
        self.assertIn("UNSUBSCRIBE_BASE_URL", str(caught.exception))

    def test_dry_run_is_still_allowed_without_one(self) -> None:
        settings = make_settings(Path(self.tmp.name), unsubscribe_base_url=None)
        result = mailer.send_bulk(settings, [self.message()], dry_run=True)
        self.assertEqual(result.sent, 1)
        self.assertIn("no_unsubscribe_url", result.warnings)

    def test_override_permits_a_seed_send(self) -> None:
        settings = make_settings(Path(self.tmp.name), unsubscribe_base_url=None,
                                 allow_send_without_unsubscribe=True)
        FakeSMTP.created.clear()
        with mock.patch.object(mailer.smtplib, "SMTP", FakeSMTP):
            result = mailer.send_bulk(settings, [self.message()], dry_run=False)
        self.assertEqual(result.sent, 1)

    def test_configured_url_needs_no_override(self) -> None:
        settings = make_settings(Path(self.tmp.name))
        FakeSMTP.created.clear()
        with mock.patch.object(mailer.smtplib, "SMTP", FakeSMTP):
            result = mailer.send_bulk(settings, [self.message()], dry_run=False)
        self.assertEqual(result.sent, 1)


if __name__ == "__main__":
    unittest.main()
