---
name: marketing-template-author
description: Author or edit an HTML email template for the local email-marketing MCP server (email-mcp). Covers choosing the closest shipped template (summer_promo, newsletter, product_announcement, plain_update), defining a small set of {{ placeholders }} that the sender must supply, staying inside the shared brand palette, passing the template validator, and confirming the result through list_templates and preview_campaign. Use when the operator wants a new campaign layout, a promo, product launch, newsletter or minimal update design, or a change to a template file under templates/. Triggers on "create an email template", "new marketing template", "add a template", "design a promo email", "edit an email template".
---

# Marketing template author

Builds one HTML template file under `templates/` for the email-mcp server. A template is a
reusable shell. Campaign copy and per-recipient values arrive at send time through variables.
This skill writes and checks the file. It does not send anything. Sending belongs to the
`marketing-campaign-send` skill.

Read these before you start:

- `templates/README.md`: the shipped templates, each one's variable table, and the footer
  contract. This skill does not repeat them.
- `references/email-html-constraints.md`: the reason behind each rule below, the palette with
  contrast ratios, and how the server transforms a template. Open it whenever a rule seems
  arbitrary or you hit a rendering question.

## Workflow

1. **Get the job.** Ask the operator what the email must do, in one sentence, and what changes
   per send: names, copy, images, a code, a date, a link. Ask only what you need to choose a
   starting point and a variable set. Do not put campaign copy into the template.

2. **Copy the closest shipped template. Do not write from scratch.**

   | The email is... | Start from |
   |---|---|
   | An offer or promo: hero image, code callout, one button | `summer_promo.html` |
   | A single product or feature launch, one button | `product_announcement.html` |
   | Several content blocks, then a closing button | `newsletter.html` |
   | Minimal and text-first, for account notices, where deliverability matters most | `plain_update.html` |

   Copy it to `templates/<snake_case_name>.html`. If that file already exists, stop and ask
   the operator before overwriting it.

3. **Choose the smallest variable set.** Keep a placeholder only when the sender supplies its
   value for every recipient. Fixed text stays as literal text in the template: the sign-off,
   "Star Support", and the footer labels. Delete any variable the copy does not use. When you
   copy `plain_update.html`, drop its hard-coded sign-off name. A person's name does not belong in
   a shared template.

4. **Edit the file.** Keep the structure: subject hint on line 1, Variables block, doctype,
   head, body with the preheader, layout tables, footer with `{{ company_address }}`, then
   `</body>`. Change the copy, the variable set and colours from the palette. Do not add
   scripts, forms, buttons, external CSS, or flexbox.

5. **Run the validator** from the repo root:

   ```
   python3 skills/marketing-template-author/scripts/validate_template.py templates/<name>.html
   ```

   It must exit 0 with no FAIL. Read every WARN and either fix it or say in your reply why it
   stays. Run it again after each change. It is standard-library only and runs with the
   system `python3` (3.9+), so it does not need the project venv. Run it with no arguments to
   check every template in `templates/`.

6. **Verify through the server.** The server reads template files on every call, so an edit
   shows up without a restart. Use the email-marketing MCP tools:
   - `list_templates`: the new file appears, its `variables` match the Variables block exactly,
     and `subject_hint` is what you wrote.
   - `preview_campaign` with `template`, `subject`, and `contact_list` (a name from
     `list_contact_lists`). Check `unresolved_variables` is empty, `recipients_missing_variables`
     is 0, and the greeting, headline and code show real values. Read them yourself: a blank cell
     shows as an empty field here, and `ready_to_send` does not catch it.
   - Without `contact_list`, the preview renders from `variables` alone. `ready_to_send` is
     false and the counts are null. That is expected. It is a quick copy check with one set of
     values.
   - **Never call `send_campaign` or `send_email` from this skill.** Sending is the send
     skill's job, and it needs operator approval.

## Naming variables

- `snake_case`: letters, digits and underscores only. Names must match `[A-Za-z0-9_]+`, or the
  server will not substitute them.
- Name the content, not the layout. `hero_image_url`, not `img1`. Repeated blocks may carry an
  ordinal, as in `section_2_blurb`.
- Per-recipient values use the same names as the CSV columns, such as `first_name`. For
  `send_campaign`, CSV columns merge in automatically. Shared values go through `variables`.
- Conventions: `*_url` is an absolute `https://` URL, `*_alt` is alt text, and `company_address`
  is required.
- The Variables block opens the file, directly after the subject hint. It has a header line
  (`Variables (all required; ...)`), then one line per name: the name, a one-line description,
  and `Example: <value>`. Write the names without braces so they are never mistaken for
  placeholders.

## Hard rules

The validator checks every item tagged with a check name. Items without a tag are checked by
hand.

- One `<!-- subject: ... -->` on one line, near the top, non-empty. `[subject-hint]`
- Every `{{ name }}` uses `[A-Za-z0-9_]+`, and braces are balanced. No nested or empty
  placeholders. `[placeholder-names] [braces-balanced] [no-nested-or-empty]`
- The Variables block lists exactly the placeholders used, each with an example, in both
  directions. `[variables-documented] [variables-unused] [variable-examples]`
- `<!doctype html>`, `<meta charset="utf-8">` and the viewport meta. `[doctype] [charset-meta] [viewport-meta]`
- Exactly one `<body>` and one `</body>`, with tags balanced. `[body-tags] [tag-balance]`
- The footer contains `{{ company_address }}`. CAN-SPAM requires a postal address, and the server's footer does not supply one. `[company-address]`
- A hidden preheader: a `display:none` element holding `{{ preheader }}`. `[preheader]`
- Every `<img>` has a non-empty `alt`, and its `src` is absolute `https://` or a placeholder. `[img-alt-and-src]`
- Every link is absolute `https://`, or a placeholder, `mailto:` or `tel:`. `[link-urls]` (WARN)
- No `<button>`, `<form>`, `<input>`, `<select>`, `<textarea>`, `<script>`, `<iframe>`, `<object>`, `<video>` or `<audio>`. `[forbidden-elements]`
- No external stylesheet, `@import` or `@font-face`. `[external-css] [web-fonts]`
- No flexbox or grid. `[layout-css]`
- No unsubscribe link or unsubscribe text in the markup. The server adds one before `</body>`. `[no-author-unsubscribe]` (WARN)
- Colours come only from the shared palette (see the reference). Any other hex colour is a WARN. `[palette]`
- Text and background pairs meet 4.5:1. Stay on the palette pairs listed in the reference. Checked by hand.
- The layout is 600px centred tables, each with `role="presentation"`. Checked by hand.
- The file is under 100 KB. `[file-size]`
- Every image has alt text, and the email reads correctly with images blocked. Checked by hand.

**Values are plain text.** The server HTML-escapes every substituted value, so a value like
`Tom & Jerry` is safe, and `<b>x</b>` appears as literal text. Do not expect a value to supply
markup. The subject is not escaped, and it is plain text.

**Plaintext is generated from the HTML.** `<a href="X">Y</a>` becomes `Y (X)`, so write link
labels that read well that way. "Shop the sale" reads well. "Click here" does not. The preheader
is not hidden from the plaintext, so it becomes the first line. Write it as a full sentence.
Table cells join with spaces, so do not rely on side-by-side columns to carry meaning.

## Pre-ship checklist

- [ ] The validator exits 0 with no FAIL, and every WARN is fixed or explained.
- [ ] `list_templates` shows the variables you intended and nothing extra.
- [ ] `preview_campaign` with a real contact list: `recipients_missing_variables` is 0, and no
      greeting, headline, code or link is empty.
- [ ] The full message renders locally. Follow the procedure in
      `references/email-html-constraints.md` section 15. Open it with images blocked: the copy, the
      button label and the code still read.
- [ ] Dark mode: open it in a dark-mode client, such as Apple Mail or the Gmail app with a dark
      theme. No text disappears.
- [ ] Every placeholder is intentional, and the operator can fill each one for every recipient.
- [ ] The file is under 100 KB.
- [ ] Before a real send, the send skill runs a test to a seed address the operator controls.

## Pointers

- `templates/README.md`: per-template variable tables, the footer and unsubscribe contract, and
  the pre-send checklist.
- `references/email-html-constraints.md`: the reason for each rule, the palette with contrast
  ratios, server behaviour (escaping, strict and blank-value checks, plaintext generation,
  footer injection), and the local render procedure.
