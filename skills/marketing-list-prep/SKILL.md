---
name: marketing-list-prep
description: Turn a messy real-world contact export (CRM dump, spreadsheet, signup-form export) into a clean CSV that the local email-marketing MCP server can load and merge into templates, treating the list as personal data throughout. Use it before any campaign when the source is not already an email-first CSV, to confirm consent, map columns onto the template's variables, drop invalid, duplicate, role and suppressed addresses, and verify the result through the MCP server. Triggers on "import my subscriber list", "clean up this CSV", "prep a contact list", "add a mailing list", "convert this export for email", "dedupe my list".
---

# Marketing List Prep

Prepares one contact export for the email-mcp server. The output is a CSV that follows the contract in `contacts/README.md`: an `email` column plus merge columns named for the template. The mechanical work is done by `scripts/normalize_list.py` (absolute path: `/Users/daniel.kennett/projects/email-mcp/skills/marketing-list-prep/scripts/normalize_list.py`). Your job is the judgment around it: consent, column mapping, and what to do about each flagged row.

A contact list is personal data. Apply the PII rules below to every step, including what you say in your own narration.

## PII rules (apply throughout)

- **Do not echo a list into the conversation.** Work from row counts, column names, and samples of 3 to 5 rows. Mask addresses you show (`ro***@example.com`), and never show a name next to an address.
- **Do not `cat`, `Read`, or `head` the data rows of an export.** Read only the header line and line count. The normalizer's report already masks addresses, so use the report instead of printing the file.
- **Sample only through the server.** Use `read_contact_list` with `limit` 3 to 5. Use `preview_campaign` with `sample_size` 1 to 3. Both return names, so summarize them and do not paste them.
- **Keep scratch copies outside the repo** (for example in `mktemp -d`). Delete them when you are done. Do not leave exports in Downloads or in the repo.
- **Do not put a real list into the repo, a template, an issue, or a commit message.**

### Where live lists should live

- **Recommended: outside the repo**, for example `/Users/daniel.kennett/email-lists/`. Point the server at it with `CONTACTS_DIR`, set in the repo's `.env` (which is gitignored and read by `load_settings()`) or in the MCP launch environment. Use an absolute path. A relative value resolves against the project root.
  - Tradeoff: a file outside the repo cannot be committed by accident, but it has no version history and no backup from the repo. Back it up on its own terms.
- **Inside `contacts/`** also works. `.gitignore` excludes `contacts/*.csv` and force-allows only the two fake fixtures (`!contacts/subscribers.csv`, `!contacts/vip_customers.csv`). This is a safety net, not a licence. A file with another extension, or one in a subfolder, is not excluded. Use this location only if the user accepts that.
- The suppression store is `state/suppressions.csv` under `STATE_DIR`, and `state/` is gitignored. Never delete or hand-edit it. Add an opt-out with the `unsubscribe` tool, not by writing the file.

## Intake: ask before touching the file

Ask these in one message, unless the user has already answered them:

1. **Source.** Where did the export come from (which CRM, signup form, vendor, or spreadsheet)?
2. **Consent.** Did these people opt in to marketing email from you? How was that recorded: a form checkbox, double opt-in, a prior purchase, a business card, a scrape, a purchased or rented list?
3. **Columns that matter.** Which template will send, and which fields does it need? (Check the template first; see the schema section.)
4. **Replace or add.** Does this replace an existing list, or add to it? If it adds, which list name?

### Consent is a gate, not a cleanup step

- An address being in an export does **not** mean the person opted in to your marketing. A customer record, a contact-form submission, or a conference badge scan is not automatically marketing consent.
- **Purchased, rented, scraped, or harvested lists are a hard stop for sending.** Mailing people who never asked damages the sending domain's reputation (complaints and blocklisting affect every message you send afterwards) and creates legal exposure under laws such as CAN-SPAM, GDPR/PECR, and CASL. Do not place such a list in `CONTACTS_DIR` and do not prepare it for a campaign. Say so plainly, and offer legitimate routes: a double opt-in sign-up you control, re-permissioning your existing opted-in audience, or a compliance review for a borderline case.
- If the user cannot say how consent was obtained, you may clean the file for review, but do not place it in `CONTACTS_DIR` until consent is confirmed and recorded in the user's own words.
- A legitimate, consented list is ordinary work. Do the job.

## Contacts can come from Odoo or another MCP server

Some audiences live in a CRM, not in a file. The email-marketing server offers a second source path next to the CSV path. Use the CSV path (this skill's normalizer) for spreadsheet and signup-form exports. Use the API path below when the contacts live in Odoo or another system you reach through its own MCP server. The two paths coexist. This does not replace the file route.

Two MCP tools on the email-marketing server support the API path:
- `import_contacts(rows, name, email_field="email", replace=False)` writes a list of flat JSON objects into a reusable contact list CSV. The campaign path then works unchanged: per-recipient merge, suppression, caps, and the audit log.
- `export_suppressions(limit=500)` returns locally suppressed addresses with reason and timestamp, newest first. It returns full addresses, because acting on them needs them. Treat that output as personal data.

### The ordered flow

1. **Ask which filter or tag defines the audience** in the CRM (for example, a tag on `res.partner`, a category, or a saved filter). Do not guess it. Confirm the count with the user before fetching.
2. **Check the template's variables first** with `list_templates`. Request exactly the fields the template needs from the CRM, so the fetch matches the list.
3. **Fetch with the CRM MCP's own search/read tool**, against `res.partner` or the equivalent model. **Explicitly request the opt-out fields** (`is_blacklisted`, `opt_out`, and any of `email_blacklisted`, `opted_out`, `unsubscribed` the model has) **along with the data fields.** See the caution below.
4. **Call `import_contacts`** with the fetched rows and a plain list name. Use `replace=True` only when the user has said this replaces an existing list. The tool returns counts, skipped reasons, and `columns_with_blanks`.
5. **Read `columns_with_blanks` against the template's variables.** Any template variable that appears there has blank cells. Fix it before sending (see the blanks note below).
6. **Confirm with `read_contact_list`**, using a `limit` of 3 to 5. Check the count against the `contacts_written` figure from step 4.
7. **Then hand off to the send skill** (`marketing-campaign-send`). Run the same pre-send gate as the file path.

### Caution: request the opt-out fields, or the exclusion cannot happen

**If you do not request `is_blacklisted`, `opt_out` and the other opt-out fields from the CRM, `import_contacts` cannot filter on them.** An absent field is not treated as opted out. A contact who unsubscribed in the CRM will then be imported and become mailable. This is the easiest way to email someone who opted out, and it is silent. Always include the opt-out fields in the fetch, and check the returned row set has them, before you call `import_contacts`.

What the tool does with them:
- A row with any truthy `is_blacklisted`, `email_blacklisted`, `opt_out`, `opted_out`, or `unsubscribed` is dropped **and** written to the local suppression list. The exclusion survives a later import, even if the CRM flag is cleared afterwards. A re-import of a previously blacklisted address with `is_blacklisted: False` is still refused.
- The opt-out flag columns are not written to the CSV. They are not merge variables.
- It also skips invalid addresses, in-batch duplicates (case-insensitive), and anything already suppressed locally.

The result reports `rows_in`, `contacts_written`, `columns`, `merge_variables`, skipped counts by reason, `skipped_examples` (masked, at most 5 per reason), `newly_suppressed`, and `columns_with_blanks`.

### Reading the CRM's values

- Field names are normalized to snake_case, so `First Name` and Odoo's `function` both arrive usable. Every non-email field becomes a merge variable `{{ field_name }}`. Only request fields the template uses, per the data-minimization rule above.
- Many-to-one values arrive as `[id, "Display Name"]` pairs. The tool flattens them to the readable half. For example, `parent_id: [7, "Engines Ltd"]` becomes `Engines Ltd`.
- Empty CRM values arrive as `False`. The tool turns them into blank cells.

### Blanks from the CRM

`columns_with_blanks` is the API path's equivalent of the normalizer's `--require`. Empty CRM values become blank cells, and `send_campaign` refuses a run when a needed value is blank. Check `columns_with_blanks` against the template's variables before sending. You can fix a blank in the CRM and re-fetch. If the blank is acceptable, the template should not need that variable. Do not loosen the template to tolerate blanks.

### The round trip: opt-outs must go back to the CRM

**A local `unsubscribe` does not reach Odoo or any other CRM.** Someone who unsubscribes through this server stays mailable from the CRM until the opt-out is written back there. Nothing syncs automatically.

- Call `export_suppressions` and get the list of locally suppressed addresses.
- Write each opt-out back into the CRM with the CRM MCP's write tools. Either blacklist the address, or set the partner's opt-out flag, whichever the CRM uses. Do this for every entry.
- Record that the sync happened, and say what was written back. Do this before the next fetch, or the CRM will still list those contacts as mailable.

## Target schema

- **`email` is required.** The server matches the header case-insensitively and lowercases values. The normalizer writes `email` in lowercase.
- **Every other column is a merge variable**, available to templates as `{{ column_name }}`. Names must use only letters, digits, and underscores (`first_name`, not `First Name`).
- **Check the template's variables first.** Run the `list_templates` tool and read the `variables` field for the template you plan to send. Shape the list to that set and nothing more. Do not invent columns the template never uses.
- **`company_address` is not a contact column.** Every shipped template requires it, but it is a campaign-level value. Pass it in the `variables` argument of `preview_campaign` and `send_campaign`. Do not add it to the CSV.
- **Explicit `variables` win over CSV columns** with the same name, for every recipient. Do not pass `first_name` in `variables` unless you mean it for everyone.

### Worked mapping example

A CRM export has these headers: `Email Address, First, Account Name, Phone, Lead Source`. The template uses `first_name` and `company`.

| Source column | Target | Action |
|---|---|---|
| `Email Address` | `email` | auto-mapped by the normalizer |
| `First` | `first_name` | auto-mapped by the normalizer |
| `Account Name` | `company` | auto-mapped by the normalizer |
| `Phone` | (none) | dropped: no template uses it |
| `Lead Source` | (none) | dropped: no template uses it |

```bash
python3 /Users/daniel.kennett/projects/email-mcp/skills/marketing-list-prep/scripts/normalize_list.py \
  /path/to/crm_export.csv \
  -o /Users/daniel.kennett/email-lists/crm_2026_10.csv \
  --keep first_name --keep company \
  --require first_name --default first_name=there \
  --suppressions /Users/daniel.kennett/projects/email-mcp/state/suppressions.csv \
  --drop-role-accounts
```

`--keep` implements data minimization. `--default first_name=there` fills blank first names with a neutral greeting, so the rendered text reads "Hi there,". `--require` still blocks any row that remains blank after the default. Both flags catch blanks in list prep, which is cheaper than a refused `send_campaign`.

If the source has no `first_name` column at all, `--default` adds the column for every row.

## The ordered workflow

1. **Inspect the raw export without reading its rows.**
   - `file -I /path/to/export.csv` shows the charset and line endings.
   - `wc -l /path/to/export.csv` gives a rough row count.
   - `head -n 1 /path/to/export.csv` shows the header only.
   - Note the delimiter. Commas, semicolons, tabs, and pipes are all detected automatically, but you should know which one it is.
2. **Map source columns onto the target schema.** Use the mapping table from the worked example. Confirm the template's variables with `list_templates` first. The normalizer auto-maps common aliases (`Email Address`, `E-mail`, `Mail` to `email`; `First`, `Given Name` to `first_name`; `Surname` to `last_name`; `Account`, `Organization` to `company`). It reports every rename it makes. Use `--map "Source=target"` only for names it does not recognise.
3. **Run the normalizer.** Start with `--report-only` to see the counts without writing anything, then run it again to write. Always pass `--suppressions` pointing at the live `state/suppressions.csv`. Without it, previously unsubscribed addresses are not removed.
4. **Review the report.** It lists the column mapping, the drop counts by reason with a few masked examples, the blockers, and warnings. Confirm the numbers make sense before you trust the output.
5. **Fix what the report flags.** Work from the row numbers, not from the data. Ask the user before choosing a default for anything that changes what the message says.
6. **Place the file.** Write it into `CONTACTS_DIR` (or the outside directory that `CONTACTS_DIR` points to) under a plain name: letters, digits, underscores, or hyphens. The list name is the filename stem.
7. **Verify through the MCP server.**
   - `list_contact_lists` shows the new list with a `count` and `columns`. An `error` field means it did not parse.
   - `read_contact_list` with `limit` 3 shows the sample, the total, and `invalid_rows`. `invalid_rows` should be 0 for a cleaned file.
8. **Cross-check the suppression list.** Run `preview_campaign` with the intended template, subject, and `contact_list`, and `sample_size` 1. See the pre-send gate below.

## Exit codes and what they mean

| Exit | Meaning | What to do |
|---|---|---|
| `0` | Clean run. The output was written. | Continue to verification. |
| `1` | Blockers: rows with a blank required value, a required column missing from the file, or no rows left. **No output file is written.** | Fill the value with `--default`, fix the source, or drop the column from `--require`. Then rerun. |
| `2` | Usage or format error: no email column, a bad `--map` name, an unreadable file, output path equal to input path, or UTF-16. **Nothing is written.** | Read the message. It lists the headers the script saw. |

Warnings appear in the report and do not change the exit code. Read them.

## Common messes and the fix for each

- **BOM or odd first-column header** (`ï»¿email`). The normalizer strips a UTF-8 BOM. A UTF-8 BOM in the file is harmless. The report says "BOM stripped."
- **Non-UTF-8 export** (Excel "CSV (Windows)" is usually cp1252). The normalizer falls back to latin-1 and names the first bad line in a warning. Accented letters usually survive. Check a few names for odd characters. If they are wrong, re-export from Excel as "CSV UTF-8".
- **UTF-16 export** (exit 2). Re-save as UTF-8 CSV.
- **Semicolon or tab delimiter.** Detected automatically. Confirm the reported delimiter matches what you expect.
- **Quoted fields with embedded commas** (`"Engines, Ltd"`). The normalizer parses these correctly, including quotes preceded by spaces. If the report warns that rows have more values than the header, the export has unquoted delimiters inside values. Those rows are shifted and their values cannot be trusted. Re-export with proper quoting, or drop the affected rows.
- **"Name" as one column.** Do not split it automatically. Splitting full names is lossy and often wrong: mononyms, multi-part surnames, initials, titles, and "Last, First" order all break it. Ask the source for separate fields. If that is impossible, choose a template that does not greet by name, or use `--default first_name=there`, and tell the user the choice.
- **Duplicate addresses that differ only in case.** The first occurrence is kept and the others are reported. Check whether the duplicates carry different names or companies, because the first one wins.
- **Leading or trailing whitespace, and non-breaking or zero-width spaces.** Stripped automatically.
- **Display-name forms** (`Rose Hart <rose@example.com>`). Unwrapped automatically. The report counts them.
- **Role accounts** (`info@`, `noreply@`, `postmaster@`, `abuse@`, `admin@`, `support@`, `sales@`, `help@`, `donotreply@`, `mailer-daemon@`, `no-reply@`). The normalizer keeps them unless you pass `--drop-role-accounts`, and reports how many it kept. Drop them by default for marketing. A role inbox is not one consenting person. If the user wants to keep one, ask why.
- **Empty cells in a column the template needs.** This is the most important one. `send_campaign` now REFUSES the whole run when any targeted recipient has a blank value for a variable the template or subject uses. Nothing is sent. The refusal lists the affected addresses under `blank`. A blank cell would otherwise render as nothing ("Hi ,"), so the refusal is deliberate. Catch blanks here in list prep with `--require` and `--default`, which is cheap. Discovering them from a refused campaign costs a round trip. Fill the blank with a sensible `--default`, or choose a template that does not need the variable. Never loosen the template to tolerate blanks.
- **Column names with spaces or punctuation** (`Discount Code`). The normalizer converts them to `discount_code`, reports the rename, and refuses a collision.
- **Two columns that map to the same name** (`Mail` and `E-mail`). Exit 2. Remove one from the export, or use `--map` to choose.

## Reading a send refusal

`send_campaign` refuses the whole run, and sends nothing, when any targeted recipient lacks a value for a variable the template or subject uses. The error has two separate lists. Read them as follows.

- **`blank`**: the column exists, but the cell is empty for that recipient. Fix it one of three ways:
  - Fill the cell in the source export and rerun the normalizer.
  - Rerun the normalizer with `--default COL=VALUE` for a neutral fallback, such as `--default first_name=there`.
  - Pass a value in the `variables` argument of `send_campaign`. This applies to every recipient, so use it only for a value that is genuinely shared.
- **`missing`**: no such column exists in the list, and no `variables` entry supplies it. Add the column to the export, use `--default COL=VALUE` to add it for every row, or pass a shared value in `variables`.

Do not work around a refusal by editing the template to drop the variable, unless the user agrees that the message no longer needs it. After any fix, rerun the normalizer, then the verification steps. Do not retry `send_campaign` blindly.

## Pre-send gate

Do not hand the list to the send step until every check passes. Report the numbers, not the rows.

1. **Counts reconcile.** Data rows minus the drops reported by the normalizer equals the rows it wrote. That number equals `list_contact_lists` `count`, which equals `read_contact_list` `total`, which equals `preview_campaign` `recipient_count`.
2. **Invalid rows are zero.** `read_contact_list` `invalid_rows` is 0.
3. **No required value is blank.** The normalizer ran with `--require` for every per-recipient variable the template uses, and exited 0. `send_campaign` would refuse a blank value anyway, but catch it here.
4. **Suppressions re-checked.** The normalizer ran with `--suppressions` pointing at the live `state/suppressions.csv`. `preview_campaign` reports `suppressed_count` of 0 for the cleaned list. A non-zero value means the list was cleaned against a different suppression file. Rerun the normalizer against the live one.
5. **A sample renders correctly.** `preview_campaign` with `sample_size` 1 to 3 gives a correct subject and greeting. `recipients_missing_variables` is 0 and `ready_to_send` is true. Check the rendered greeting for a blank name (`Hi ,`) before you accept it.
6. **Consent confirmed** in the user's own words, and the source recorded.
7. **Hand off to the send step.** Use the `marketing-campaign-send` skill. It runs a dry run first and needs explicit operator confirmation before any real send.

### Re-imports and prior opt-outs

- A reimport does not clear the suppression list. The suppression store is separate from the list file. Every reimport must be run with `--suppressions`, so that an address someone unsubscribed from never reappears in the new list.
- **Never clean an address back onto a list when someone opted out.** If an opted-out address reappears in a new export, drop it and say so. Do not "restore" it because the export contains it.
- Never remove a line from `state/suppressions.csv`. That file is the record of opt-outs.

## Related

- `contacts/README.md` in the repo is the CSV contract this skill produces. Read it for the server's own rules. This skill does not repeat them.
- The `marketing-campaign-send` skill covers the send itself, including dry runs, rate limits, and staged rollout with `send_campaign`'s `limit`.
