# Contacts

CSV files of recipients. The email-marketing MCP server reads a list from here, validates each row, and uses the other columns as merge variables when it renders a template.

**The two files in this directory are fake sample data.** `subscribers.csv` and `vip_customers.csv` contain invented names and only `example.com`, `example.org`, and `example.net` addresses. They exist so the server can be run and tested without real recipients. Replace them with real lists only in a location outside the repository (see below).

## CSV contract

- UTF-8, comma-delimited, with a header row.
- **An `email` column is required.** The header match is case-insensitive, so `email`, `Email`, and `EMAIL` all work.
- **Every other column becomes a merge variable** available to templates as `{{ column_name }}`. For example, a `discount_code` column fills `{{ discount_code }}`.
- Column names should use only letters, digits, and underscores. Placeholder names must match `[A-Za-z0-9_]+`, so a column named `Discount Code` or `account-tier` cannot be referenced from a template.
- Keep columns that a template needs and drop the rest. Extra columns are harmless but are data you are copying around for no reason.
- **Invalid rows are skipped, not fatal.** A row with a blank or malformed `email`, or an address that appears more than once, is skipped and reported. The rest of the file still loads.

### Shapes in this directory

| File | Header |
|---|---|
| `subscribers.csv` | `email,first_name,last_name,company,city,discount_code` |
| `vip_customers.csv` | `email,first_name,last_name,company,account_tier` |

`subscribers.csv` includes two deliberately bad rows so the validation path can be exercised: one row with a blank email, and a duplicate of an earlier address. Expect the loader to report both and load the other ten rows. `vip_customers.csv` has five clean rows.

## Exporting a list from a CRM

1. Export contacts from the CRM as CSV with at least the email column plus the fields your templates use.
2. Rename the headers to the names your templates expect, using only letters, digits, and underscores (for example, `First Name` becomes `first_name`).
3. Remove columns you do not need, and any personal data you do not need. Data minimization is the default; add a field only when a template uses it.
4. Deduplicate on email before saving, so the loader does not have to report duplicates.
5. Save as UTF-8 CSV with a header row, and put the file outside this repository (see below).

## Real lists are personal data

A recipient list is personal data. Names, email addresses, and company or city details identify real people, and sending to them carries legal obligations for consent and unsubscribe handling.

- **Do not commit real lists to git.** This repository's `.gitignore` excludes `contacts/*.csv` and force-allows only the two fake sample files (`subscribers.csv`, `vip_customers.csv`), so a live list dropped in this directory is ignored by default. Verify with `git check-ignore -v contacts/your_list.csv` before trusting it. Better still, keep live lists outside the repository entirely and point `CONTACTS_DIR` at them — then no git rule stands between a subscriber list and a commit.
- **Recommended:** store live lists in a directory outside the repo (for example, `~/email-lists/`) and point the server at it with the `CONTACTS_DIR` environment variable. The repo then holds only the fake samples above.
- Do not paste real lists into chat, issue trackers, or templates.
- Delete exported copies from Downloads once they have been loaded.
