# email-mcp

A local MCP server that lets Claude draft, preview, and send template-based email campaigns through an SMTP relay (Ionos, Mailgun, or SendGrid). It runs on your Mac, reads HTML templates and CSV contact lists, and sends through the relay.

Nothing goes out by accident. Sends are dry runs by default, capped per run and per hour, filtered against a suppression list, and logged.

The paths in this README use `/Users/daniel.kennett/projects/email-mcp`. Substitute your own clone path wherever it appears.

**Status.** No live send has completed. The TLS handshake against `smtp.ionos.com` succeeds. Authentication was checked only by confirming that a deliberately wrong password is rejected with 535. No message has been delivered. 62 unit tests pass. The author has not yet connected a Claude client end to end.

## How a campaign runs

You ask Claude something like "Send the spring offer to the members list." Claude then works through these steps:

1. **Discover.** `list_templates` and `list_contact_lists` show what exists.
2. **Read.** `read_template` and `read_contact_list` show the markup, the `{{ variables }}`, and the list's columns.
3. **Draft and merge.** Claude writes the subject and any shared values, such as a promo code. Each recipient's CSV columns are merged in automatically.
4. **Preview.** `preview_campaign` renders sample recipients, counts recipients and suppressed contacts, and reports missing variables. Nothing is sent.
5. **Dry run.** `send_campaign` or `send_email` with `dry_run=true` validates everything, opens no SMTP connection, and returns a run ID and counts. For a staged send, add `limit=10` here and in the real run.
6. **Send.** After you approve, the same call with `dry_run=false` sends through the relay. Claude reports the run ID, the sent, skipped and failed counts, and any failure reasons. `send_log` shows the audit trail.

## Quick start

### 1. Install

You need macOS on Apple Silicon or Intel, and [uv](https://docs.astral.sh/uv/). Nothing needs configuring per architecture.

```sh
cd /Users/daniel.kennett/projects/email-mcp
uv sync --python 3.12
```

The project requires Python 3.12. The system `python3` is 3.9 and cannot run it, so always go through uv. uv uses the pin in `.python-version` and downloads 3.12 on first use if needed. Do not use `pip install` or `python3 -m email_mcp.server` against the system Python.

The server speaks MCP over stdin and stdout, so `uv run --directory /Users/daniel.kennett/projects/email-mcp email-mcp` prints nothing and appears to hang. That is correct. Press Ctrl-C to stop it.

### 2. Configure

The configuration is one file with three required settings. Create it in step 3 and fill in the values:

```json
{
  "mcpServers": {
    "email-marketing": {
      "command": "/Users/daniel.kennett/projects/email-mcp/bin/email-mcp-launch",
      "env": {
        "SMTP_HOST": "smtp.ionos.com",
        "SMTP_USER": "rose@starsupportus.com",
        "SMTP_PASSWORD": "put-the-mailbox-password-here"
      }
    }
  }
}
```

Replace the password. Everything else has a default: the sender is `rose@starsupportus.com`, the port is 587 with STARTTLS, dry runs are on, and the hourly cap is 400 because the host is Ionos. Other settings are in the Settings section.

### 3. Connect a client

Claude Desktop and Claude Code both read the same settings. Use either or both.

**Claude Desktop.** The config file is `~/Library/Application Support/Claude/claude_desktop_config.json`. Create the folder if it is missing, copy the example, and lock the file down, because it holds the relay password in plain text:

```sh
mkdir -p "$HOME/Library/Application Support/Claude"
cp /Users/daniel.kennett/projects/email-mcp/claude_desktop_config.example.json \
   "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
chmod 600 "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
```

Then replace the password. If the file already exists, add the `email-marketing` entry inside the existing `mcpServers` object. Do not replace the whole file. This file configures every MCP server, so a single misplaced comma removes all of them from Claude. Back up the file before editing, and validate it before restarting:

```sh
cp "$HOME/Library/Application Support/Claude/claude_desktop_config.json" \
   "$HOME/Library/Application Support/Claude/claude_desktop_config.json.bak"
python3 -m json.tool "$HOME/Library/Application Support/Claude/claude_desktop_config.json" >/dev/null && echo "valid JSON"
```

`command` points at `bin/email-mcp-launch`, not at `uv`. Claude Desktop starts MCP servers without a login shell, so it has almost no `PATH`. Homebrew also installs to a different prefix on each architecture: `/opt/homebrew` on Apple Silicon, `/usr/local` on Intel. The launcher finds `uv` itself, checking both Homebrew prefixes, `~/.local/bin`, `~/.cargo/bin`, and then `PATH`. It also derives the project directory from its own location, which is why the config needs no architecture-specific paths and no `args`. Its diagnostics go to stderr, because stdout carries the MCP protocol.

**Claude Code.** The same settings can be added from the command line. `-s user` makes the server available from any directory. The default `local` scope ties it to the current project.

```sh
claude mcp add email-marketing -s user \
  -e SMTP_HOST=smtp.ionos.com \
  -e SMTP_USER=rose@starsupportus.com \
  -e SMTP_PASSWORD=the-mailbox-password \
  -- /Users/daniel.kennett/projects/email-mcp/bin/email-mcp-launch
claude mcp list
```

Add other settings with more `-e KEY=value` flags. The values are written to the `env` block in `~/.claude.json`.

### 4. Verify

Fully quit the client with Cmd-Q, not just the window, and reopen it. Settings load once per server process, so every change needs this restart. Then ask Claude:

> Check the email configuration.

Claude calls `config_status`, which returns the effective settings with the password redacted, the `tls_trust_source` in use, and a `warnings` list. A warning that a setting is still unedited means the example value is still in place.

A successful dry run does not prove the credentials work, because a dry run never contacts the relay. Send one real message to yourself before a campaign (see Safety and compliance).

## Settings

This table is the only place settings are documented. In the client config they are JSON strings in the `env` block, for example `"DRY_RUN_DEFAULT": "true"`. Booleans accept `true`/`false`, `1`/`0`, `yes`/`no`, or `on`/`off`. Relative directory paths resolve from the project root. A `.env` file in the project root works as a fallback with the same names, and the client config `env` block overrides it.

| Setting | Default | What it does |
| --- | --- | --- |
| `SMTP_HOST` | none (required) | Relay host: `smtp.ionos.com`, `smtp.mailgun.org`, or `smtp.sendgrid.net`. A host containing `ionos` gets the 400/hour default cap. |
| `SMTP_USER` | none (required) | Relay login. Mailgun: `postmaster@<sending-domain>`. SendGrid: the literal `apikey`. Ionos: the full mailbox address. |
| `SMTP_PASSWORD` | none (required) | Mailgun: the domain's SMTP password, not the API key. SendGrid: an API key with Mail Send. Ionos: the mailbox password. |
| `SMTP_PORT` | `587` | Relay port. Use `465` only with `SMTP_STARTTLS` set to `false`. |
| `SMTP_STARTTLS` | `true` | `true` upgrades the connection with STARTTLS. `false` uses implicit TLS, which port 465 expects. |
| `SMTP_CA_BUNDLE` | unset (OS trust store) | Path to a PEM file that overrides the OS trust store for the relay's TLS certificate. See Troubleshooting. |
| `FROM_EMAIL` | `rose@starsupportus.com` | Sender address. It must be on a domain verified with the relay. |
| `FROM_NAME` | `Star Support` | Sender display name. |
| `REPLY_TO` | unset (replies go to `FROM_EMAIL`) | Reply-To address. |
| `UNSUBSCRIBE_BASE_URL` | unset | An `http(s)://` endpoint you control that records opt-outs. When set, mail gets an unsubscribe footer and `List-Unsubscribe` headers. When unset, neither is added and `config_status` warns. Never use a placeholder. See Safety and compliance. |
| `DRY_RUN_DEFAULT` | `true` | Whether send tools dry-run when the caller does not pass `dry_run`. Leave it `true`. |
| `MAX_RECIPIENTS_PER_RUN` | `2000` | A single run with more recipients than this is refused. |
| `MAX_MESSAGES_PER_HOUR` | `5000`, or `400` for an `ionos` host | Cap on real messages sent in any trailing hour. |
| `BATCH_SIZE` | `50` | Messages per SMTP batch. |
| `BATCH_PAUSE_SECONDS` | `2.0` | Pause between batches, in seconds. |
| `TEMPLATE_DIR` | `templates` | Folder of HTML templates. |
| `CONTACTS_DIR` | `contacts` | Folder of CSV contact lists. Point it outside the project to keep live subscriber data out of git. |
| `STATE_DIR` | `state` | Holds the suppression list and `send_log.jsonl`. Created on startup. |
| `EMAIL_MCP_UV` | unset (launcher searches standard locations) | Full path to `uv`, read by the launcher only. Set it when `uv` lives somewhere the launcher does not look. |

## Sending through a relay

Do not send campaigns directly from your Mac or a personal Gmail account. Use a relay.

### Ionos (current setup)

Ionos hosts the mail and DNS for `starsupportus.com`, so this needs no new account and no new DNS. Set `SMTP_HOST` to `smtp.ionos.com`, `SMTP_USER` to the full mailbox address, and `SMTP_PASSWORD` to the mailbox password. Port 587 with STARTTLS is the default.

Ionos limits outbound volume per mailbox, roughly 500 an hour on most plans. Check yours. The server defaults `MAX_MESSAGES_PER_HOUR` to 400 for any host containing `ionos`, so a large send is not rejected partway through. For more headroom, set `BATCH_PAUSE_SECONDS` to `5.0`.

Ionos suits small sends of a few hundred recipients, where the volume cap is not in play. It is the wrong choice for a real bulk list. A mailbox provider may throttle or suspend a mailbox that behaves like a bulk sender, and Ionos provides no bounce or complaint webhooks.

### Mailgun or SendGrid (real bulk volume)

Both provide bounce and complaint webhooks and have no per-mailbox cap.

**Mailgun**

1. Add and verify a sending domain, typically `mg.starsupportus.com`, and create the DNS records Mailgun shows. Mailgun does not send until the domain is verified.
2. Under the domain's SMTP credentials, copy the SMTP password. It is not the API key. The login is usually `postmaster@<sending-domain>`.
3. Set `SMTP_HOST` to `smtp.mailgun.org`, `SMTP_PORT` to `587`, `SMTP_USER` to `postmaster@mg.yourdomain.com`, and `SMTP_PASSWORD` to the SMTP password. Use `smtp.eu.mailgun.org` only for an EU-region account.
4. Sandbox domains deliver only to addresses you have authorised. Use a verified domain for campaigns.

**SendGrid**

1. Complete Sender Authentication for `starsupportus.com`. Domain authentication is preferred over single-sender verification. Create the DNS records SendGrid shows.
2. Create an API key with Mail Send permission. The key is the SMTP password.
3. Set `SMTP_HOST` to `smtp.sendgrid.net`, `SMTP_PORT` to `587`, `SMTP_USER` to `apikey`, and `SMTP_PASSWORD` to `SG.<your API key>`.

### DNS: SPF, DKIM, DMARC

Receiving servers check mail from `rose@starsupportus.com` against DNS records on the sending domain. Without them, mail lands in spam or is rejected. Gmail and Yahoo require sender authentication (SPF and/or DKIM), and DMARC for bulk senders.

- **SPF** is a TXT record naming the servers allowed to send for the domain. Use the exact `include:` the relay's dashboard gives you, such as `include:mailgun.org` or `include:sendgrid.net`.
- **DKIM** is a signature the relay adds to each message. The public key is published as CNAME or TXT records. Create them exactly as shown and wait for the relay to mark them verified. DKIM survives forwarding, and SPF does not.
- **DMARC** is a TXT record at `_dmarc.starsupportus.com`, for example `v=DMARC1; p=none; rua=mailto:<reports-address>`. Start with `p=none` to collect reports. When SPF and DKIM pass for real mail, tighten to `quarantine`, and later `reject`.

Receivers check that the `From` domain is authorised (SPF) and signed (DKIM), and that at least one of those aligns with `From` (DMARC). If the relay signs for a different domain, alignment fails and DMARC rejects the mail. So verify `starsupportus.com` or a subdomain with the relay, and publish the records there.

**A domain may have only one SPF record.** A second `v=spf1` TXT record makes SPF fail with a permanent error, which breaks all sending from the domain, including the Odoo mail that works today. Edit the existing record instead. For example, adding Mailgun to today's record gives `v=spf1 include:_spf.odoo.com include:_spf-us.ionos.com include:mailgun.org ~all`.

#### What is published for `starsupportus.com` today

Checked with `dig` on 2026-10-23. Re-check before trusting it:

```
MX      mx00.ionos.com, mx01.ionos.com          (mail hosted at Ionos)
SPF     v=spf1 include:_spf.odoo.com include:_spf-us.ionos.com ~all
DMARC   v=DMARC1; p=none;
DKIM    no record found at the selectors tested
```

Three things follow from that:

1. **SPF already authorises Odoo and Ionos.** Add a relay's `include:` to this existing record, as shown above. That is two `include:` lookups of the ten SPF allows, so there is room.
2. **No DKIM record was found.** Only common selector names were tested, and Ionos issues per-account selectors, so this is not proof. Confirm in the Ionos control panel. If DKIM really is off, it is the most valuable fix. DKIM keeps working when mail is forwarded, and DMARC needs SPF or DKIM to pass and align with `From`.
3. **DMARC is `p=none` with no `rua=`.** That means no enforcement and no reports, so nobody learns when mail fails authentication. A reporting address costs nothing: `v=DMARC1; p=none; rua=mailto:dmarc@starsupportus.com;`. Leave `p=none` until the reports come back clean, then tighten to `quarantine`.

All of these are edited in the Ionos DNS panel, since Ionos holds the zone.

## Tools

The server exposes 12 tools. Every tool returns JSON. A failure returns `{"error": "...", "hint": "..."}` instead of a traceback. Read-only tools never send mail.

| Tool | Arguments | What it does |
| --- | --- | --- |
| `list_templates` | none | Lists templates with name, variables, subject hint, size, and modified time. Read-only. |
| `read_template` | `name` | Returns the raw HTML, its variables, and its subject hint. Can be large. Read-only. |
| `list_contact_lists` | none | Lists CSV lists with row count and columns. An unparseable list shows an `error`. Read-only. |
| `read_contact_list` | `name`, `limit=25` (max 200) | Returns a sample, the total, the columns, and `invalid_rows` (which includes duplicates). Read-only. |
| `preview_campaign` | `template`, `subject`, `variables?`, `contact_list?`, `sample_size=3` (max 10) | Renders sample recipients, with HTML cut to about 4000 characters. Reports `recipient_count`, `suppressed_count`, `recipients_missing_variables`, `unresolved_variables`, and `ready_to_send`. Read-only. |
| `send_email` | `recipients` (list), `subject`, `html_body`, `text_body?`, `dry_run?` | Ad-hoc send to an explicit address list. Validates addresses, drops duplicates, and skips suppressed ones. |
| `send_campaign` | `template`, `contact_list`, `subject`, `variables?`, `dry_run?`, `limit?` | Template and contact-list merge send. Suppressed contacts are removed first. `limit` takes the first N eligible contacts. |
| `import_contacts` | `rows`, `name`, `email_field="email"`, `replace=false` | Writes rows from an external source, such as the Odoo MCP server, into a reusable CSV list. Writes files and the suppression list. Sends nothing. |
| `export_suppressions` | `limit=500` (max 2000) | Lists local opt-outs with reason and timestamp, for copying back into Odoo or a CRM. Read-only. |
| `unsubscribe` | `email`, `reason=""` | Adds an address to the local suppression list only. Does not change the relay's list. |
| `send_log` | `limit=20` (max 200) | Returns recent audit records, newest first, the trailing-hour count, and the hourly cap. Read-only. |
| `config_status` | none | Returns the effective settings with secrets redacted, the TLS trust source, and warnings. Opens no network connection. |

Send results include `run_id`, `dry_run`, `total`, `sent`, `skipped`, `failed`, `duration_seconds`, up to five `failure_reasons`, and `warnings`. `send_campaign` also returns `recipients_targeted` and `suppressed_excluded`. In a dry run, `sent` counts the messages that would be sent.

Merge rule: each recipient's variables are their CSV columns, overlaid by the `variables` argument. An explicit `variables` value wins over a CSV column with the same name.

## Templates and contact lists

**Templates** are HTML files in `templates/`. `templates/README.md` lists the shipped templates and their variables.

- Placeholders are `{{ name }}`. Spaces inside the braces are conventional, and both forms work. Names use only letters, digits, and underscores. Every variable a template uses must be supplied for every recipient, from a CSV column of that name or from the `variables` argument.
- The subject hint is a comment such as `<!-- subject: Your spring offers, {{ first_name }} -->`. It is a suggestion. The subject passed to the send tools is what is used. The subject is also a template.
- When `UNSUBSCRIBE_BASE_URL` is set, the engine adds a footer before `</body>` at send time, with the From name and an unsubscribe link, and adds `List-Unsubscribe` headers. Do not write an unsubscribe link into a template. Previews do not show the footer.
- Templates are self-contained HTML. Images must be hosted at a URL the recipient can load.

**Contact lists** are CSV files in `contacts/`. `contacts/README.md` has the full CSV contract.

- The header row must include an `email` column, matched case-insensitively. Other column names are lowercased and become merge variables. Invalid and duplicate rows are skipped and reported, not fatal. Check them with `read_contact_list` before a send.
- `contacts/*.csv` is gitignored except for two example files. Set `CONTACTS_DIR` to keep live lists outside the repository.

## Contacts from Odoo or another MCP server

Contacts do not have to start as a CSV. Fetch partners through your Odoo MCP server, then pass the rows to `import_contacts`. The result is a named list, so the normal campaign path applies: merge, suppression, recipient cap, and audit log.

```
Odoo MCP      read res.partner (include is_blacklisted and opt_out)
Claude        maps the fields the template needs
email-mcp     import_contacts(rows, name="odoo_newsletter")
email-mcp     preview_campaign -> dry run -> send_campaign
```

Opt-outs are enforced in both directions. A partner that Odoo marks blacklisted or opted out is dropped from the list and written to the local suppression list, so the exclusion survives later imports. To push local unsubscribes back, use `export_suppressions` and write them into Odoo with its own tools. Nothing syncs on its own.

One trap: `import_contacts` can only filter on opt-out fields that are present in the rows. If the Odoo fetch does not request `is_blacklisted` and `opt_out`, their absence is not treated as an opt-out. Always include them. The `marketing-list-prep` skill covers this path and the file-based one.

## Skills

Three skills in `skills/` drive this server: `marketing-template-author`, `marketing-list-prep`, and `marketing-campaign-send`. `skills/README.md` covers how to install them and what each ships.

## Safety and compliance

### Guardrails

- **Dry run by default.** With `DRY_RUN_DEFAULT` true, send tools dry-run unless the caller passes `dry_run=false`. A dry run renders and validates everything and opens no SMTP connection. It is written to the audit log with `dry_run` set, and it does not count toward the hourly cap. If the batch would exceed the cap, the dry run reports that as a warning.
- **Caps.** `MAX_RECIPIENTS_PER_RUN` refuses a run larger than the cap. `MAX_MESSAGES_PER_HOUR` refuses a real send when the trailing-hour count plus the batch would exceed it. Only successful real sends count. `send_log` shows the current count as `hourly_sent`.
- **Pacing.** `BATCH_SIZE` and `BATCH_PAUSE_SECONDS` spread sends out, so the relay sees a steady flow.
- **Suppression list.** Suppressed addresses are removed from campaigns and skipped by `send_email`. Add addresses with `unsubscribe`. The list is `suppressions.csv` in `STATE_DIR`.
- **Audit log.** Each recipient event is written to `state/send_log.jsonl` with its `run_id`, and dry runs are flagged. Treat it as the record of what went out.
- **Refused before any send.** `send_campaign` refuses the whole run if any targeted recipient is missing a variable or has a blank value, because a blank renders as "Hi ,". Preview reports the same problems and sets `ready_to_send` to false. A placeholder name with characters other than letters, digits, and underscores, such as `{{ first-name }}`, refuses the template or subject in preview and in both send tools. `send_email` also refuses unfilled placeholders and invalid addresses.
- **Partial sends are still possible.** The refusals above happen before anything is sent. SMTP-level failures do not: a dropped connection or a per-recipient rejection mid-run can leave the campaign partly delivered. Before retrying, use the `run_id` and `send_log` to establish who received it.
- **`limit` does not remember.** A later run without `limit` includes the same first contacts again. For a staged send, make the second run target only the remaining contacts, or check `send_log` before continuing.
- **Secrets.** Credentials live only in the client config `env` block or in `.env`, never in the repository. `config_status` never shows the password.

### Compliance obligations (CAN-SPAM)

These apply to every commercial email. The server helps, but the operator is responsible.

- **Working unsubscribe.** `UNSUBSCRIBE_BASE_URL` must point to an endpoint that actually records opt-outs and accepts the one-click POST that `List-Unsubscribe-Post` requests. This server stores suppressions only in `STATE_DIR`, and it runs no web endpoint.
- **Do not run real campaigns without it.** The server does not block a send when the URL is unset. It warns in `config_status` and in send results, and the mail then goes out with no footer and no `List-Unsubscribe` header. The `marketing-campaign-send` skill refuses a real send in that state.
- **Honour opt-outs promptly.** CAN-SPAM requires honouring them within 10 business days. Record each with `unsubscribe` the same day, then remove the address from the relay's own suppression list. Otherwise the relay may still send to it.
- **Real postal address.** Every commercial email must include the sender's valid postal address. The footer does not include one. The provided templates take it as the `company_address` variable, so pass the real address in `variables` for every campaign and check it with `preview_campaign`.
- **Accurate From and Subject.** The From name and address must identify the sender truthfully, and the subject must not mislead about the content.
- **Test before you send.** Send one message to yourself with `send_email`. Check the footer, the unsubscribe link, and the postal address in what arrives.

**Open gaps.** Nothing reads opt-outs back from `UNSUBSCRIBE_BASE_URL`, so opt-outs reach this server only through `unsubscribe` or `import_contacts`. The relay's own suppression list and its bounce and complaint webhooks are not wired either. This server does not see bounces or complaints, so until those webhooks feed suppression, hard bounces keep being mailed, which hurts deliverability and reputation.

## Troubleshooting

**The server does not appear in Claude.**
- In Claude Desktop, the usual cause is invalid JSON, which removes every MCP server. Validate the file as shown in Quick start, and restore the backup if needed.
- Confirm `command` points at `bin/email-mcp-launch`, not at `uv`.
- Fully quit with Cmd-Q and reopen. Logs are in `~/Library/Logs/Claude/`, in files whose names contain `mcp`.
- To test the launcher alone, run `/Users/daniel.kennett/projects/email-mcp/bin/email-mcp-launch` in Terminal. It should wait silently. Press Ctrl-C to stop it.
- In Claude Code, run `claude mcp list`. If the server is missing, re-run the `claude mcp add` command from Quick start.

**Configuration error.** The error names the missing setting (`SMTP_HOST`, `SMTP_USER`, or `SMTP_PASSWORD`). Add it to the `env` block of the `email-marketing` entry and restart the client.

**Authentication failures (SMTP 535, "authentication failed", "invalid credentials").**
- Run `config_status` to see the masked login and whether a password is set.
- Mailgun: `SMTP_PASSWORD` must be the domain's SMTP password, and `SMTP_USER` must be that domain's SMTP login.
- SendGrid: `SMTP_USER` must be exactly `apikey`, and `SMTP_PASSWORD` must be an API key with Mail Send permission.
- Confirm that `SMTP_HOST`, `SMTP_PORT`, and `SMTP_STARTTLS` match the provider's settings.

**TLS failure: "CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate chain".** The dry run passes and the real send fails at the TLS handshake. The mail server is fine.

Python does not read the macOS Keychain. A uv-managed interpreter verifies against the OpenSSL bundle at `/etc/ssl/cert.pem`, which carries only public roots. A TLS-inspecting proxy, normal on a corporate laptop, re-signs every connection with a private root CA. macOS trusts that root and the bundle does not, so verification fails even though the relay is fine.

The server verifies against the OS trust store through `truststore`, so any root the machine's management installed is honoured. `config_status` reports the source as `tls_trust_source`: `os_trust_store` normally, or `ca_bundle:/path` when `SMTP_CA_BUNDLE` overrides it.

If it still fails, the inspection root is missing from the OS store too. Get the PEM file from whoever runs the network and set `SMTP_CA_BUNDLE` to its path. Verification cannot be disabled, and there is deliberately no setting for it, because an unverified SMTP connection exposes the relay password on the wire. `Install Certificates.command` from the python.org installer does not apply to a uv-managed interpreter.

**Hourly cap reached.** A real send returns a rate-limit error. Run `send_log` to see the trailing-hour count, then wait for the window to clear. Raise `MAX_MESSAGES_PER_HOUR` only if the relay plan allows it.

**"Recipients exceeds the per-run cap."** Send in smaller batches with `limit`, or raise `MAX_RECIPIENTS_PER_RUN`.

**Unfilled or blank template variables.** The send is refused and nothing is sent. Preview's `unresolved_variables` lists the names. Add the column to the CSV, or pass the value in `variables`.

**`uv sync` cannot find Python 3.12.** Run `uv python install 3.12` once. That needs network access.

## Mac compatibility

Apple Silicon (M1 to M4) and Intel run the same configuration, and nothing changes when you move between them. The engine uses only the standard library, apart from `mcp` and `truststore`. Resolving the dependencies for either architecture gives the same 31 packages. The one difference is `cryptography`, which pins an older version on Intel because recent releases stopped publishing x86-64 macOS wheels. Both install from wheels, with no source build and no Rosetta. uv fetches a CPython build for the local architecture.

Verified on Apple Silicon (arm64): an MCP stdio handshake through the launcher, with `PATH` set to a nonexistent directory, listed all 12 tools. Intel was checked by cross-architecture dependency resolution only, not on Intel hardware.

## Layout and development

```
email-mcp/
  pyproject.toml                       package metadata; console script email-mcp
  .python-version                      3.12
  .env.example                         fallback config template
  claude_desktop_config.example.json   client config to copy
  bin/email-mcp-launch                 launcher used by both clients
  src/email_mcp/                       server.py (MCP tools), config.py, templates.py,
                                       contacts.py, mailer.py, audit.py, tls.py
  templates/                           HTML templates
  contacts/                            CSV contact lists
  skills/                              Claude skills for this server
  state/                               suppression list and send_log.jsonl (gitignored)
  tests/                               unit tests
```

- Run the tests with `uv run python -m unittest discover -s tests -t .`.
- After changing dependencies, run `uv sync --python 3.12`. `mcp` is pinned below 2.0 because the server uses `FastMCP` from `mcp.server.fastmcp`, which `mcp` 2.x removed.
- The MCP stdio transport uses stdout. Do not add `print` statements to the server or the engine. Use `logging`, which writes to stderr.
- Tool docstrings are the specification Claude reads. Keep them accurate when behaviour changes.
