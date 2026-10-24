# Email HTML constraints: why each rule exists

SKILL.md lists the rules. This file gives the reason for each one, the shared palette with
its contrast ratios, and the server behaviour that shapes a template. The variable tables for
the shipped templates are in `templates/README.md`, not here.

Every claim about the server below was checked against `src/email_mcp/templates.py`,
`src/email_mcp/mailer.py` and `src/email_mcp/server.py`. Where the code and the wider email
ecosystem differ, that is noted.

## 1. Who renders the email

Recipients use Gmail (web and app), Outlook desktop (the Word rendering engine), Apple Mail
and iOS, and many others. Each one supports a different subset of HTML and CSS. The server
sends `multipart/alternative`: a plaintext part and an HTML part. Design for the weakest
engine, then check the strongest ones.

## 2. Layout: tables, 600px, centred

- Use an outer full-width table, then an inner `width="600"` table with `role="presentation"`,
  `cellpadding="0" cellspacing="0" border="0"`. Set both the `width` attribute and a
  `max-width` style.
- Why: Outlook's Word engine ignores `max-width` on `div` layouts but honours table `width`
  attributes. Nested tables are the only layout every client renders the same way.
- Why `role="presentation"`: screen readers would otherwise announce the layout table as data.

## 3. CSS: inline styles are what survives

- Put `style` on every element that matters. Gmail has historically dropped most `<style>`
  rules and the `<head>`, so anything that depends on a stylesheet can disappear.
- The one `<style>` block in the shipped templates is a media query for narrow screens. It is
  progressive enhancement. The email is still correct at 600px without it, so nothing may
  depend on it.
- Not supported, or unreliable across clients: flexbox, grid, `float`, `position`, external
  stylesheets (`<link rel="stylesheet">`), `@import`, and `@font-face` web fonts (they do not
  load reliably). Use the font stack `Helvetica,Arial,sans-serif`.
- Set `bgcolor="#..."` as an attribute and `background-color` in the style on coloured cells.
  Outlook honours the attribute more reliably than the style.

## 4. No interactive or active content

- No `<script>`, `<form>`, `<input>`, `<select>`, `<textarea>`, `<iframe>`, `<object>`,
  `<video>` or `<audio>`. Clients strip them or render nothing, and the reader loses the path
  to the action.
- No `<button>`. Many clients, Outlook especially, do not apply padding or background colour
  to a button element. Use a padded `<a>` inside a coloured table cell:

  ```html
  <td bgcolor="#0F6B63" style="border-radius:6px; background-color:#0F6B63;">
    <a href="{{ cta_url }}" style="display:inline-block; padding:14px 32px; font-family:Helvetica,Arial,sans-serif; font-size:16px; font-weight:bold; color:#FFFFFF; text-decoration:none; background-color:#0F6B63;">{{ cta_label }}</a>
  </td>
  ```

  The whole button stays clickable, and the label is still text if the cell colour is lost.

## 5. Images

- Many clients block images until the reader allows them. Every template must therefore read
  correctly with no images at all. Put the headline, the call to action, the discount code and
  the key facts in text. Images decorate.
- Every `<img>` needs a non-empty `alt` that describes what the picture shows. `alt` is what a
  reader sees while images are blocked, and what a screen reader announces.
- Give each image `width` and `height` attributes and `display:block`. Put `bgcolor` on the
  cell, so a solid coloured band remains visible when the image is blocked.
- Absolute `https://` URLs only. A relative path does not resolve in an email, and `http://`
  is blocked or warned about in many clients.
- Do not put text inside an image. It is invisible when images are blocked and unreadable for
  screen readers.
- Do not use background images as content. Many clients drop them.

## 6. Dark mode

- Clients such as Gmail, Apple Mail and Outlook can invert colours automatically. The
  `color-scheme` and `supported-color-schemes` meta tags ask for light-only rendering. Clients
  may ignore them, so do not rely on them alone.
- Set `color` and `background-color` on the same element, for every cell that holds text.
  If a client changes only one of the two, the explicit pairing stops black-on-black or
  white-on-white.
- Do not use transparent PNGs, which turn black-on-black on a dark background.
- Test in a dark-mode client before shipping (see SKILL.md, pre-ship checklist).

## 7. Accessibility

- Headings: one `<h1>` for the headline, then `<h2>` for sections. Screen readers navigate by
  heading, so the order must be logical.
- Body text needs a contrast ratio of at least 4.5:1 (WCAG AA). The palette below meets that.
- Never convey meaning by colour alone. Links are underlined. The discount code is text, not a
  coloured badge with no label.
- `<html lang="en">`, `alt` on every image, and `role="presentation"` on layout tables.

## 8. Preheader

- A hidden `<div>` with `display:none; font-size:1px; line-height:1px; max-height:0px;
  max-width:0px; opacity:0; overflow:hidden; mso-hide:all;` containing the preheader text.
  Clients show that text next to the subject in the inbox list. Without it, they show the first
  visible body text, which is often "Hi Dana" or "View in browser".
- Side effect, verified: `html_to_text()` does not skip hidden elements, so the preheader text
  becomes the first line of the plaintext part. Write it as a complete sentence that also reads
  well as the opening line of a text-only email.

## 9. Plaintext part (derived automatically)

`build_mime()` uses `html_to_text()` to make the plaintext part from the HTML. Its rules, as
implemented:

- `<a href="X">Y</a>` becomes `Y (X)`. If the label already equals the URL, or the href starts
  with `#`, only the label is kept. Write link labels that read well with the URL in
  parentheses. "Shop the sale (https://...)" works. "Click here (https://...)" reads badly.
- `<br>`, block tags (`p`, `div`, `tr`, `h1`-`h6`, `li` and others) start a new line.
- `<td>` and `<th>` become spaces, so two columns join on one line: "Cell A Cell B". Do not
  rely on side-by-side cells to carry meaning.
- `<head>`, `<style>`, `<script>` and `<title>` contents are dropped. `display:none` content is
  not dropped (see the preheader note above).
- Images contribute no text. An image with no text beside it adds nothing to the plaintext.
- For campaigns sent through `send_campaign`, the server appends `Unsubscribe: <url>` to the
  plaintext after this conversion, when an unsubscribe base URL is configured.

## 10. What the server injects, and where

From `src/email_mcp/mailer.py`:

- `apply_compliance()` inserts the unsubscribe footer immediately before the **last**
  `</body>` in the file (`rfind`). A file with two `</body>` tags is therefore ambiguous. A file
  with none gets the footer appended at the end.
- The footer is a `div` reading "You are receiving this because you are on a contact list for
  Star Support." followed by an "Unsubscribe" link. **It contains no postal address.**
- Two headers are added: `List-Unsubscribe: <url>` and
  `List-Unsubscribe-Post: List-Unsubscribe=One-Click`.
- All of this happens only when `UNSUBSCRIBE_BASE_URL` is set. Without it, no footer and no
  headers are added, and the run carries a `no_unsubscribe_url` warning.
- `preview_campaign` does not show the injected footer, because it is added at send time.

**Why the template must carry the postal address:** the CAN-SPAM Act (15 U.S.C. 7704(a)(5))
requires a valid physical postal address in commercial email. The server's footer does not
include one, so every template needs its own `{{ company_address }}` in a footer block. The
validator fails any template without it.

**Why the template must not carry an unsubscribe link:** the server adds one. A second link
duplicates the footer and gives the reader two unsubscribe paths that may not behave the same.
The validator warns on any unsubscribe text in the markup.

## 11. Placeholders and escaping (from `templates.py`)

- **Strict by default.** `render()` raises `ValueError` if a placeholder name has no key in the
  variables. `send_campaign` then refuses the whole campaign. Nothing is partly sent.
- **Blank values are refused at send time, but not in preview.** Strict rendering only checks
  that a key exists, so a present-but-empty value would render as "Hi ,". `send_campaign` now
  calls `blank_names()` and refuses the whole campaign when any recipient has a blank value for
  a placeholder the template or subject uses. `preview_campaign` does not do this check: a blank
  value shows up as an empty field in the preview, and `ready_to_send` can still be `true`. Read
  every value in the preview output yourself before handing off to the send step.
- **Escaped by default.** `render()` HTML-escapes substituted values (`&`, `<`, `>`, quotes).
  `Tom & Jerry` renders correctly. `<b>bold</b>` appears as literal text. Values are plain text,
  so do not wrap a placeholder in markup, expecting the value to supply it.
- **The subject is not escaped.** `escape=False` is used for subject lines, which are plain
  text.
- **Malformed names are invisible to the server.** The server's pattern only matches
  `[A-Za-z0-9_]+`. `{{ head-line }}` matches nothing, so `render()` returns it unchanged and
  the recipient sees the braces. The server does not report it. The validator fails it
  (`placeholder-names`).

## 12. File size

Gmail clips long messages behind a "View entire message" link, at roughly 102 KB. The
validator warns above 100 KB. Images cost nothing here, since they are URLs. The shipped
templates are 4 to 9 KB.

## 13. Palette

One brand system across all templates. Use only these colours. Any other hex colour triggers
a `palette` WARN. Pure `#FFFFFF` and `#000000` are also accepted.

| Role | Hex | Used for | Contrast |
|---|---|---|---|
| Accent (teal) | `#0F6B63` | Buttons, links, masthead, hero band | White on accent 6.36:1; accent on white 6.36:1 |
| Ink | `#1F2933` | Headings and body text | On white 14.76:1; on callout tint 12.99:1; on page 13.02:1 |
| Muted | `#4A5563` | Footer text | On footer tint 6.20:1; on white 7.58:1 |
| Callout text | `#134E48` | Text on the callout tint | On callout tint 8.36:1; on white 9.50:1 |
| Callout tint | `#E7F3F1` | Discount block, product image block | Background only |
| Footer tint | `#E4E9EE` | Footer background | Background only |
| Page | `#EEF1F4` | Outside the 600px card | Decorative |

The ratios are WCAG 2.x relative-luminance calculations. They were computed for this
document and match the figures in `templates/README.md`.

Do not use purple or violet as an accent. Change the palette in every template together, so
the set stays one brand system.

**Known drift:** `templates/newsletter.html` uses `#D5DCE3` four times, at lines 69, 78, 87 and
96. That colour is not in the palette, so the validator reports it as a WARN.

## 14. What static checks cannot see

The validator reads the markup. It cannot see how a client renders it. Layout, dark mode,
image-blocked appearance, and whether the copy reads well are checked by a person, using the
pre-ship checklist in SKILL.md.

## 15. Full-length local render (for the visual check)

`preview_campaign` cuts its HTML at 4000 characters, so it cannot show the footer or the end of
a long template. For the image-blocked and dark-mode checks, render the whole file locally with
the project's own `render()`. Use one sample row, the operator's values, or a fake row. Do not
paste a real recipient into chat, and delete the output file afterwards:

```bash
cd /Users/daniel.kennett/projects/email-mcp && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv/bin/python -c "
import pathlib, sys
from email_mcp.templates import render
html = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')
values = {'first_name': 'Dana', 'headline': 'Sample headline'}  # every placeholder, filled
sys.stdout.write(render(html, values))
" templates/NAME.html > /tmp/NAME-rendered.html
```

`render()` is strict, so the script fails with the missing names if `values` does not cover every
placeholder. That failure is the point: it is the same check the server makes. The output has no
unsubscribe footer, because the server adds that at send time. Open the file in a browser with
images turned off, then in a dark-mode client if one is available.
