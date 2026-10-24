# Deliverability background for campaign sends

Read this when a sending domain is first set up, when results show bounces or complaints, or
when mail lands in spam. The send workflow in SKILL.md assumes these basics are in place.

Where a figure is provider-specific, it is marked as such. Provider rules change, so check the
current published guidance before relying on any number here.

## 1. What this server does and does not do for deliverability

| Handled by this server | Not handled by this server |
| --- | --- |
| Builds a multipart message with a plain-text part derived from the HTML | Sender authentication DNS records (SPF, DKIM, DMARC) |
| Adds an unsubscribe footer and `List-Unsubscribe` headers when `UNSUBSCRIBE_BASE_URL` is set | Bounce and complaint handling from the relay |
| Keeps a local suppression list and skips suppressed addresses | Syncing opt-outs from the unsubscribe endpoint into that list (you do this by hand) |
| Pauses between batches (`BATCH_SIZE`, `BATCH_PAUSE_SECONDS`) and caps volume | Warming a new domain, list hygiene, content review |
| Logs every send to `state/send_log.jsonl` | Seeing whether mail reached an inbox or a spam folder |
| Refuses a campaign when any targeted recipient has a missing or blank merge value | Checking that merge values are correct (placeholder text, wrong names) |
| Builds every message in the dry run, so header faults such as line breaks in a subject fail the dry run | Confirming SMTP acceptance or inbox placement; only a seed send shows that |

## 2. Sender authentication: SPF, DKIM, DMARC

The `From` address is `rose@starsupportus.com`. Receiving servers check three things about the
domain in `From`:

- **SPF** is a TXT record on the domain listing the servers allowed to send for it. A receiver
  checks that the connecting relay is on that list. Without SPF, or with a second `v=spf1` record
  added alongside the first, the check fails. Keep one SPF record per domain. Use the exact include
  the relay gives you (see the README "Sending through a relay" section).
- **DKIM** is a cryptographic signature added by the relay, with a public key published in DNS. It
  stays valid when a message is forwarded, which SPF does not. Create the CNAME or TXT records the
  relay shows, then wait for the relay to mark them verified.
- **DMARC** is a TXT record at `_dmarc.<domain>`. It says what to do when a message fails, and it
  requires that the domain in `From` aligns with the domain that passed SPF or DKIM. Start with
  `p=none` and a `rua` address to collect aggregate reports. Check that real mail passes. Then tighten
  to `quarantine`, and later to `reject`.

What breaks without them:

- Mail to Gmail, Yahoo, and Outlook is increasingly filtered or rejected when authentication is
  missing or does not align.
- A relay that signs for a different domain than `From` gives a DMARC alignment failure, even when
  the signature is valid. This is why the domain verified with the relay must be `starsupportus.com`
  or a subdomain used in `From`.

How to check a real message: open the seed message's full headers (Gmail: "Show original") and look
for `spf=pass`, `dkim=pass`, and `dmarc=pass`, with the `header.from` aligned to `starsupportus.com`.
Check the `List-Unsubscribe` header is present when the footer is.

## 3. Reputation follows the domain, not the relay

A high-volume relay (Mailgun, SendGrid) gives you infrastructure: sending servers, bounce handling,
dashboards. It does not give you trust. Receivers judge the sending domain (the DKIM `d=` domain and
the `From` domain) and the sending IP or IP pool. Shared IP pools carry the reputation of everyone
sharing them. Your reputation is built from your own behaviour: what you send, to whom, how often,
and how recipients respond.

So switching relays does not fix a domain that has been sending to stale or unengaged lists, or
that has high complaint rates. The fix is in the sending behaviour and the list.

## 4. Warming a new or dormant sending domain

A domain with little or no sending history has no reputation to draw on, so receivers apply
caution. Ramp volume gradually:

- Start with the recipients most likely to open and reply: recent sign-ups, recent customers, people
  who opened recent mail.
- Increase the size of each wave over several sends, watching bounces, complaints, and inbox
  placement between waves.
- Keep sends regular. A quiet domain that suddenly sends a large batch looks like a spam pattern.
- Pause the ramp on any sign of trouble: a rise in bounces, complaints, or deferrals, or a drop in
  opens.

No safe daily number is given here. The right ramp depends on the list, the provider, and the
domain's history. Provider guidance on volume and ramp changes over time, so check current docs. The
operator sets the wave sizes. The `limit` argument and the reconciliation step in SKILL.md are the
mechanism for doing that.

## 5. Bounces and complaints

- **Hard bounce** (permanent, 5xx SMTP code, for example `5.1.1` unknown mailbox): the address will
  never accept mail. Remove it from future sends. Repeated hard bounces hurt reputation.
- **Soft bounce** (temporary, 4xx code, for example a full mailbox or a greylisting response): the
  address may work later. Retry later, not immediately, and stop after a few attempts.
- **Complaint** (the recipient pressed "report spam"): reported back to the sender by mailbox
  providers through feedback loops or webhooks. Complaints are one of the strongest negative signals.
  Remove the address, and find out why the recipient complained (old list, unclear sign-up, content
  that did not match the promise).

Why the relay's webhooks still matter even though this server keeps a local list: this server only
sees what happened at SMTP acceptance time. It does not see later bounces, deferrals, or complaints.
Mailgun and SendGrid both offer event webhooks for these outcomes. Their event names differ, so check
the provider's current docs. Wire the bounce, complaint, and unsubscribe events to a process that adds
those addresses to the relay's suppression list. Also copy the important ones (opt-outs, hard
bounces) into the local list with `unsubscribe`, with a reason. Without this, the next send re-tries
addresses the relay already knows are bad.

## 6. Content triggers

Filters score content and structure as well as sender reputation. Common triggers:

- A subject line that misleads, such as a fake "Re:" or "Fwd:", or a fake alert. Subject and content
  must match. This is also a CAN-SPAM accuracy issue.
- Image-only emails, or an image that carries all the text. Many clients block images by default, so
  the copy must still read without them. The shipped templates are built for this.
- Link shorteners and redirects that hide the destination.
- Link text that names one domain while the href points to another.
- ALL CAPS, runs of exclamation marks, and "FREE!!!" style wording.
- Broken or unclosed HTML, and very large HTML (keep templates well under the size the templates
  README recommends).
- A missing or poor plain-text part. This server generates one from the HTML. Check the generated text
  in the seed message: it should read sensibly, and the unsubscribe line should be there.

No single trigger decides delivery, and no specific score is published that you can rely on. Fix the
pattern, then measure the result with seed sends.

## 7. List hygiene

- Mailing old, unengaged, or never-confirmed addresses is the most common cause of bounces, complaints,
  and spam-trap hits.
- Spam traps are addresses kept by providers or blocklists that exist only to catch senders who do
  not manage their lists. Hitting one can damage the domain's reputation for a long time.
- Set a re-engagement and removal policy that the operator approves, and apply it before each large
  send. Define "unengaged" in the operator's terms. The numbers are a business decision, not a fixed
  rule.
- Deduplicate by lowercased email (the server does this, but check the source file too).
- Check the CSV for blank cells in merge columns. The server now refuses a campaign when any targeted
  recipient has a blank value for a variable the template uses, and reports it as `blank`, so "Hi ,"
  cannot reach a real send. A blank is still a data fault to fix at the source. The check is not a
  substitute for confirming that the values are right.
- Keep the list's provenance: where each address came from and when the person consented. This is
  needed to answer a complaint or a regulator.

## 8. Provider bulk-sender rules (provider-specific, verify current docs)

Major mailbox providers publish requirements for senders above certain volumes. As of the last check
(not re-verified for this document):

- Gmail's published bulk-sender requirements cover SPF or DKIM authentication, DMARC, one-click
  unsubscribe for marketing mail, timely processing of unsubscribes, and a spam-complaint rate
  ceiling. The published ceiling is 0.3 percent, with a lower target recommended. The volume threshold
  that triggers the requirements is also provider-specific.
- Yahoo publishes similar sender requirements.

Look up the current Google Postmaster Tools and Yahoo sender guidance before relying on any figure.
Postmaster Tools shows the domain's spam-complaint rate and authentication results for Gmail mail,
once enough mail has been sent to Gmail.

## 9. When mail lands in spam: diagnostic sequence

Work through these in order. Do not change several things at once, or you will not know what fixed it.

1. **Authentication.** Check the seed's headers for SPF, DKIM, and DMARC pass and alignment. A failure
   here outranks everything else.
2. **Domain and IP reputation.** Check the relay dashboard for the sending domain and IP pool, and
   check Postmaster Tools (Gmail) for the domain. Look for a drop in reputation or a rise in complaints.
3. **Blocklists.** Check the sending domain and the relay's sending IPs against public blocklists. Use
   a lookup tool such as MXToolbox, and check each listing's own requirements.
4. **Recent sends.** Compare the volume and timing of recent sends against normal patterns. Look for a
   sudden large batch to a cold segment.
5. **Recipients.** Check the bounce and complaint rates for the last few sends. Look for a rise in
   hard bounces, which points to a stale list, and in complaints, which points to consent or content.
6. **Content.** Re-read the subject, the links, the image-to-text ratio, the footer, and the plain-text
   part. Compare with the last campaign that landed in the inbox.
7. **Seed test.** Send a seed to a few mailboxes at different providers, with the change in place. Note
   where each lands. Ask the recipients (or check the folder) before drawing conclusions.
8. **Recovery.** Reduce volume to the most engaged recipients, fix the pattern, and re-warm if needed.
   Do not send a large catch-up batch to recover.
