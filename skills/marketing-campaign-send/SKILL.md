---
name: marketing-campaign-send
description: Drive the safe send workflow for BULK and MARKETING email through the local email-marketing MCP server, using template plus contact-list campaigns. Covers config check, preview, dry run, seed send, staged real send, send_log verification, and partial-failure recovery. Use it whenever someone wants to send a template, newsletter, or promo to a subscriber or contact list, or to dry-run one. Do not use it for an individual personal email or Gmail triage; those belong to the email-followups skill. Triggers on "send the summer promo", "send a campaign", "email my subscriber list", "blast the newsletter", "send marketing email", "dry run a campaign", "stage a send to the members list".
---

# Marketing campaign send

Bulk email through this server cannot be recalled once it is sent. Your job is to make a
careless blast hard and a correct send routine. Run the steps in order. Do not skip a step
because the operator seems in a hurry.

**Status of this server.** No real SMTP send has ever been performed with it, and no Claude
client has been connected to it end to end. Treat the first real send as the first ever.
Current behaviour is covered by unit tests and dry runs only. Say so if asked.

## Stop conditions (all must hold before any real send)

Do not pass `dry_run=false` until every item is true. If one is not, stop and report which.

1. `config_status` shows no `warnings` and `unsubscribe_url_configured` is true. Without an
   unsubscribe URL the server adds no footer and no List-Unsubscribe header, so the send would
   be non-compliant. Refuse. The server cannot test that the URL is live, so ask the operator
   to confirm the endpoint really records opt-outs.
2. Opt-outs are current. **Manual step, no tool support:** the server never reads the opt-outs
   recorded at `UNSUBSCRIBE_BASE_URL`. Ask the operator for that endpoint's current opt-out list,
   run `unsubscribe(email, reason)` for each address not already suppressed, and report how many
   were added. Skipping this means people who opted out will be mailed.
3. The `company_address` value is a real postal address that the operator supplied. The server
   refuses a blank value, but it accepts any non-blank string. Confirm the value yourself. Refuse
   the sample address `100 Example Way, Springfield, IL 62701` from `templates/README.md`, and
   refuse any value containing "example", "TODO", "placeholder", or square or curly brackets.
4. A dry run with the exact same arguments returned without an error, `failed` is 0 (or each
   failure is explained and resolved), and the run record's `warnings` is empty (step 6). A clean
   dry run catches rendering faults, blank or missing merge values, and header problems such as
   line breaks in the subject. It does **not** prove SMTP authentication, relay acceptance, or
   what the email looks like in a client. Only the seed send does that.
5. A seed send reached an address the operator controls, and the operator checked it in a real
   mail client (step 7).
6. The operator has explicitly approved the final recipient count for this send, after seeing the
   dry-run numbers, in this conversation.
7. For any staged or repeated send, the send log has been reconciled (step 10).

A real send is irreversible. Approval of a dry run, a preview, or a seed is not approval of the
list. Never infer approval for a real send from an earlier "go ahead" that referred to something
else. Ask explicitly, for example: "Approve a real send of N recipients from list X with template
Y, stage 1 of M?" and wait for a clear yes.

## Tools (names and arguments from `src/email_mcp/server.py`)

- `config_status()`
- `list_templates()`, `read_template(name)`
- `list_contact_lists()`, `read_contact_list(name, limit=25)` (limit max 200)
- `preview_campaign(template, subject, variables=None, contact_list=None, sample_size=3)`
- `send_campaign(template, contact_list, subject, variables=None, dry_run=None, limit=None)`
- `send_email(recipients, subject, html_body, text_body=None, dry_run=None)`
- `unsubscribe(email, reason="")`
- `send_log(limit=20)` (limit max 200)

`dry_run=None` means `DRY_RUN_DEFAULT`, which is true unless the operator changed it. Always
pass `dry_run` explicitly, true or false, so the call is unambiguous.

## Ordered workflow

1. **`config_status`.** Check stop conditions 1 and 2. Note `dry_run_default`,
   `smtp_password_set`, `unsubscribe_url_configured`, `max_recipients_per_run`,
   `max_messages_per_hour`, `contacts_dir`, and `state_dir`.
2. **`list_templates`.** Pick the template and note its variables. Call `read_template` only when
   you need its markup. Confirm you can supply every variable it lists, including `company_address`.
3. **`list_contact_lists`, then `read_contact_list(name, limit=25)`.** Check `total`, `columns`,
   and `invalid_rows`. The sample is always the first rows of the file. Confirm the values in the
   columns the template uses are correct. The server now refuses blank cells in those columns, but
   a blank cell is not the only data fault: check for placeholder text, wrong names, and wrong
   values.
4. **Draft the subject and shared `variables`.** An explicit `variables` value overrides a CSV
   column with the same name. Put the real `company_address` here, or in the CSV. The subject is a
   template, so `{{ first_name }}` works in it.
5. **`preview_campaign`** with the template, subject, variables, and contact list. Read the rendered
   subject and HTML for the sampled recipients. Look for leftover `{{`, wrong values, and empty
   fields. Check `recipients_missing_variables` (it covers every eligible recipient, not just the
   sample), `suppressed_count`, `recipient_count`, and `ready_to_send`. The preview does not show
   the unsubscribe footer, because the server adds it at send time.
6. **Dry run:** `send_campaign(..., dry_run=True, limit=<if staged>)`. Reconcile the counts:
   `recipients_targeted` = `total` = `sent + skipped + failed`. In a dry run, `sent` means "would be
   sent". `suppressed_excluded` is the suppression-list drop. `skipped` should normally be 0 for
   campaigns, because invalid and duplicate CSV rows are dropped at load and suppressed addresses
   are removed before dispatch. A non-zero `skipped` reason (`invalid_email`, `duplicate_in_batch`,
   `suppressed`) needs an explanation before you go on. Any non-zero `failed` in a dry run is a stop:
   read the reasons (see "Reading a SendResult"). Then read the `warnings` field on this run's
   record in `send_log` (see step 8). Warnings must be empty, or each one resolved.
7. **Seed send.** Write a one-row seed list in `contacts_dir` with the operator's own address and the
   same columns as the real list. Get approval for this single real send. Run
   `send_campaign(template, seed_list, subject, variables, dry_run=False)`. Then have the operator
   open the message in a real client, with images on and with images blocked. Check the subject with
   variables filled in, the body, every link target, the footer and its unsubscribe link, the real
   postal address, and the plain-text part. Check the headers for SPF, DKIM and DMARC pass and for
   `List-Unsubscribe`. Delete the seed file when done. If the operator's address is on the
   suppression list, the send is refused with "No recipients left". Report that to the operator and
   do not work around it.
8. **Staged real send.** Get approval for the stage size. Read this stage's dry-run `warnings` from
   `send_log`. If it contains `would_exceed_hourly_cap`, do not send: the real run would be refused
   in full, not in part, with a rate-limit error. Otherwise run
   `send_campaign(..., dry_run=False, limit=N)` and verify it (step 9). Do not reuse the same
   arguments for the next stage; see step 10.
9. **Verify with `send_log`.** Each recipient gets a `send` record and each run gets a `run` record.
   The tool returns the newest 200 records at most. Confirm the run's `sent` and `failed` counts match
   the result, and that `warnings` does not contain `no_unsubscribe_url`. For a run larger than 200
   records, read the JSONL file directly, read-only (see References).
10. **Reconcile before any later wave (required).** `limit` takes the first N eligible contacts and
    does not remember who was sent to. An unlimited run after a limited run re-mails the first N.
    Every later wave must be built from the remainder:
    - Collect emails the log shows as really sent:
      `jq -r 'select(.event=="send" and .dry_run==false and .status=="sent") | .email' <state_dir>/send_log.jsonl | sort -u`
    - Read the contact list with Python's `csv` module, not `cut`, and drop every address in that set.
    - Write the remainder to `contacts_dir` as a new CSV with the original header. Preview it, dry-run
      it, check its `warnings`, get the approved count, then send from it with no `limit`.
    - Do not use the suppression list for this. `unsubscribe` records an opt-out, which is a compliance
      record.

## Reading a SendResult

Fields: `run_id`, `dry_run`, `total`, `sent`, `skipped`, `failed`, `duration_seconds`, and
`failure_reasons` (the first five, as `email: reason`). Dry runs add a `note`. Campaigns add
`recipients_targeted` and `suppressed_excluded`.

**Warnings.** `SendResult.warnings` carries run-level warnings: `no_unsubscribe_url`, and
`would_exceed_hourly_cap: ...` (a dry run's forecast that a real run would be refused). The
`warnings` field is written to the `run` record in `send_log` for every run, dry or real. **The tool
response does not currently include `warnings`**, so read them from the `run` record for that
`run_id`. If a later version of the tool returns them, read them there too. An empty list is the
only clean result.

**Refusals.** A campaign refused for data reasons returns `error` with a `missing` list and a `blank`
list of recipients. Both mean nothing was sent. Treat them as data faults: fix the list or the
`variables` value, then preview again. Do not drop the placeholder from the template.

Only five failure reasons come back. For the full list, filter the log for that `run_id` and
`status: "failed"`. Common reasons:

- `connection failed: SMTP authentication failed (code 535)`: credentials are wrong, and nothing was
  sent. Every recipient fails the same way. Stop, and have the operator fix `.env` and restart the
  server.
- `connection failed: attempt N: ...`: the relay could not be reached (after one retry). Check host,
  port, and network with the operator. Retry only after reconciling (see Partial failure).
- A refusal shown as a dict, for example `{'x@example.com': (550, b'5.1.1 ...')}`: the relay refused
  that recipient. Read the three-digit code. A 4xx code is temporary (a soft bounce). A 5xx code is
  permanent (a hard bounce), so drop that address from future waves. `5.1.1` means the mailbox does
  not exist. `5.7.x` usually means a policy or reputation block. Exact wording is provider-specific.
- `message build failed: ...` (dry run or real): the message could not be built, for example a subject
  containing a line break. Fix the cause; no SMTP connection was opened for it in a dry run.
- Text like `timed out`, `Server disconnected`, or `Connection reset`: ambiguous. The relay may have
  accepted the message before the connection dropped. Do not retry it. Check the relay's activity log
  first.
- `unknown error`: the exception had no text. Treat as ambiguous.
- An `error` dict with no `run_id`: an unexpected exception was caught by the server wrapper. The run
  may have partly executed. Treat it as a partial send.

## Partial failure

1. Find the run. Use the `run_id` from the result. If there is no result, search the log by time
   window and `subject`. A run that has `send` lines but no `run` line was interrupted.
2. Build three sets for that run from the log: `sent` (dry_run false, status sent), `failed` with the
   reason for each, and `unsettled` (in the target list but absent from the log).
3. `sent` means the relay accepted the message, not that it reached an inbox. Treat `unsettled` and the
   ambiguous failures as possibly sent. Check the relay's own activity log for those addresses before
   anything else.
4. Never re-run the whole campaign. Retry at most the clearly not-sent subset (for example, failures
   before any message was accepted, or 4xx refusals), and only with approval, from a new list containing
   just those addresses. Exclude hard (5xx) failures.
5. Report the three counts to the operator. Do not describe the send as complete until they are
   reconciled.

## Unsubscribes

- When someone opts out, call `unsubscribe(email, reason)` the same day. Use a reason such as "replied
  STOP" or "footer link". The tool returns `suppressed` or `already_suppressed`.
- **The unsubscribe endpoint is not synced.** The server does not read opt-outs recorded at
  `UNSUBSCRIBE_BASE_URL`. Before every real send, copy the endpoint's current opt-outs into the local list
  with `unsubscribe` (stop condition 2). This is a manual step with no tool support, and it is the
  largest open compliance gap in this setup.
- `unsubscribe` changes only the local list in `state_dir`. The tool response says so. The operator must
  also remove the address from the relay's own suppression list, in the relay dashboard or API. Do not
  assume the relay honours the List-Unsubscribe header for you.
- CAN-SPAM requires honouring opt-outs within 10 business days. Do it the same day.

## What this skill will not do

- Send to a list the operator has not confirmed in this conversation, with a count.
- Send while `unsubscribe_url_configured` is false.
- Send while the endpoint's opt-outs have not been copied into the local list.
- Accept a placeholder-looking `company_address`, even though the server accepts it.
- Loosen rendering to get a campaign out. Do not remove a placeholder from a template to clear an
  error, do not change `escape` behaviour, and do not drop CSV rows to pass a check without the
  operator's approval. A `missing` or `blank` refusal is a data problem: add the column, fill the
  cell, supply the value in `variables`, or ask the operator, then preview again.
- Raise `MAX_*` caps, change `DRY_RUN_DEFAULT`, or edit `.env`, templates, or code to get a send
  through. Those are operator decisions.
- Use `send_email` to blast a list. It takes pasted addresses and bypasses template merge and the
  company-address check. Use `send_campaign` for lists. `send_email` is for hand-picked one-off
  messages, and you must type the postal address into the body yourself.
- Use `limit` as a resume mechanism, or re-run a limited send without reconciling (step 10).
- Retry a whole campaign after a partial failure.
- Add recipients to the suppression list as a rollout tool.

## References

- `references/deliverability.md`: SPF, DKIM, DMARC; sender reputation; warming; bounces and complaints;
  content triggers; list hygiene; diagnosis when mail lands in spam. Read it when a sending domain is
  first set up, when results show bounces or complaints, or when mail is landing in spam.
- `/Users/daniel.kennett/projects/email-mcp/README.md`, the "Safety and compliance" section
  (guardrails, CAN-SPAM obligations, and the known open gaps). "How a campaign runs" outlines the
  same flow as the steps below.
- Send log: `<state_dir>/send_log.jsonl`, where `state_dir` is from `config_status`. The default is
  `/Users/daniel.kennett/projects/email-mcp/state/`. Read it with `jq` or Python, read-only. Each
  per-recipient record has `event`, `run_id`, `dry_run`, `subject`, `email`, `status`, `reason`, and
  `ts`. Each run record has `event: "run"`, `run_id`, `dry_run`, `total`, `sent`, `skipped`, `failed`,
  `warnings`, and `ts`. Dry runs are written to the same file, so filter on `dry_run == false` for real
  sends. The log records the subject, not the template or list name.
