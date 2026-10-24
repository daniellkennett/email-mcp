# Email templates

These files are reusable HTML shells for the email-marketing MCP server. Claude Desktop drafts the copy and fills the variables; the server substitutes them and sends the result through the SMTP relay. Templates are not finished one-off emails. Keep copy out of them except for generic boilerplate, and put everything that changes per campaign in a variable.

## Shipped templates

| File | Purpose |
|---|---|
| `summer_promo.html` | Seasonal offer: hero image, headline, discount-code callout, one CTA |
| `newsletter.html` | Multi-section update: intro, three content blocks, closing CTA |
| `product_announcement.html` | Single-focus launch: product image, tagline, three features, one CTA |
| `plain_update.html` | Minimal text-first update, no images, one inline link |

## Authoring a new template

1. Create `templates/<name>.html`.
2. Put the subject hint on a single line near the top:

   ```
   <!-- subject: Your subject line here -->
   ```

   `{{ }}` variables are allowed inside it.
3. Directly after it, add a comment block listing every variable the file uses, with a one-line description and an example value. Write the names without braces in that block so it is never mistaken for a placeholder.
4. Write a complete document: doctype, `<html>`, `<head>` with `<meta charset="utf-8">` and the viewport meta, `<body>`, and a real `</body>`.
5. Use `{{ name }}` placeholders for everything that changes.

### Placeholder syntax

- Form: `{{ name }}`. The name must match `[A-Za-z0-9_]+`. Inner whitespace is optional (`{{name}}` also works).
- Any double-brace sequence in the file is treated as a placeholder, including in comments and inline CSS. Do not write literal double braces for any other purpose.
- An unresolved placeholder is a hard error at send time. Nothing is silently blanked, so every placeholder in a template must be one the sender is expected to fill. Keep the set small.
- Values are plain text. Do not pass HTML in a value unless the server documents that it inserts values raw. Whether values are HTML-escaped is the server's behavior, not something these templates assume.
- URL variables (`cta_url`, `*_url`, `link_url`) must be absolute `https://` URLs.

### Subject hint

- Exactly one `<!-- subject: ... -->` per file, on one line, near the top.
- Variables inside it must be listed in the variable comment block like any other variable.

### Footer and unsubscribe contract

- Every template ends its body with a footer block that holds the sender name (Star Support) and `{{ company_address }}`.
- The server appends the unsubscribe footer immediately before `</body>` when an unsubscribe URL is configured. Templates must not contain their own unsubscribe link, because that would duplicate the server's footer.
- Every template needs a real `</body>`, which is where the server inserts that footer.
- `{{ company_address }}` is required. CAN-SPAM requires a valid physical postal address in commercial email.
- If no unsubscribe URL is configured, the server adds no footer. Configure one before sending marketing mail.

### Email HTML constraints, and why

- **Table layout, 600px max, centered.** Email clients support CSS unevenly. Nested tables render consistently in Outlook, which uses the Word rendering engine and ignores most modern layout CSS.
- **No flexbox, grid, external CSS, web fonts, background images used as the only content, or JavaScript.** Most clients ignore them, and scripts are stripped. Images are not load-bearing; see below.
- **Inline `style` attributes for everything that matters.** Gmail strips much of `<head>`, including most `<style>` rules. The one `<style>` block is a `max-width: 600px` media query for narrow screens. Nothing depends on it.
- **Preheader.** A hidden `div` with `display:none; overflow:hidden; max-height:0` shows as inbox preview text but not in the body.
- **Buttons are padded `<a>` elements inside a table cell with `background-color`.** Do not use `<button>`, which many clients do not render.
- **Web-safe font stack** (`Helvetica, Arial, sans-serif`). Web fonts do not load reliably.
- **Dark mode.** Text and background colors are set together on every cell so no client can produce black-on-black. Do not use transparent PNGs over a white assumption. The `color-scheme` meta tags tell clients the design is light-only.
- **Images.** Every image uses an absolute `https://` URL placeholder, meaningful `alt` text, and a background color on its cell. Many clients block images by default. Every template must still read correctly with images off.
- **Accessibility.** `alt` on every image, a logical heading order (`h1` then `h2`), text contrast of at least 4.5:1, no meaning conveyed by color alone (links are also underlined), and `role="presentation"` on layout tables.
- **Size.** Keep HTML under about 100KB. Gmail clips longer messages behind a "View entire message" link.

### Palette

All four templates share one palette. Contrast ratios are against the background they sit on.

| Role | Hex | Used for | Contrast |
|---|---|---|---|
| Accent (teal) | `#0F6B63` | Buttons, links, masthead, hero block | 6.36:1 white text on accent |
| Ink | `#1F2933` | Headings and body text | 14.8:1 on white |
| Muted | `#4A5563` | Footer text | 6.2:1 on `#E4E9EE` |
| Callout tint | `#E7F3F1` | Discount block, product image block | `#134E48` text ~8.3:1 |
| Footer tint | `#E4E9EE` | Footer background | see above |
| Page | `#EEF1F4` | Outside the 600px card | decorative only |

Do not use purple or violet as an accent. Change the palette in all four files together so they stay one brand system.

## Variables per template

Every variable below is required. The subject hint variables are included.

### `summer_promo.html`

| Variable | Description | Example |
|---|---|---|
| `first_name` | Greeting and subject | Dana |
| `preheader` | Hidden preview text | Take 20% off everything through August 31. |
| `headline` | Main heading | Long days, bigger savings |
| `body_copy` | One plain-text paragraph | Our summer sale covers the whole shop until the end of August. |
| `hero_image_url` | Absolute `https://` URL of the hero image | https://images.example.com/summer-hero.jpg |
| `hero_image_alt` | Alt text for the hero | Striped beach towel on warm sand |
| `discount_code` | Promo code | SUMMER20 |
| `discount_expiry` | Expiry date, human-readable | August 31, 2026 |
| `cta_label` | Button text | Shop the sale |
| `cta_url` | Button link, absolute `https://` | https://shop.example.com/summer |
| `company_address` | Footer postal address | 100 Example Way, Springfield, IL 62701 |

Subject hint: `Summer savings are here, {{ first_name }}`

### `newsletter.html`

| Variable | Description | Example |
|---|---|---|
| `issue_title` | Subject and main heading | Star Support news: August edition |
| `preheader` | Hidden preview text | Three updates from the team this month. |
| `intro_text` | Intro paragraph | Here is what we shipped and what is coming next. |
| `section_1_heading` | Block 1 heading | New reporting dashboards |
| `section_1_blurb` | Block 1 summary | Custom views are now available on every plan. |
| `section_1_url` | Block 1 link, absolute `https://` | https://www.example.com/news/dashboards |
| `section_2_heading` | Block 2 heading | Office hours returning |
| `section_2_blurb` | Block 2 summary | Weekly support office hours start again on Tuesdays. |
| `section_2_url` | Block 2 link, absolute `https://` | https://www.example.com/news/office-hours |
| `section_3_heading` | Block 3 heading | Partner spotlight |
| `section_3_blurb` | Block 3 summary | A look at how one customer cut onboarding time in half. |
| `section_3_url` | Block 3 link, absolute `https://` | https://www.example.com/news/partner-spotlight |
| `cta_label` | Closing button text | Read the full update |
| `cta_url` | Closing button link, absolute `https://` | https://www.example.com/news |
| `company_address` | Footer postal address | 100 Example Way, Springfield, IL 62701 |

Subject hint: `{{ issue_title }}`

### `product_announcement.html`

| Variable | Description | Example |
|---|---|---|
| `product_name` | Subject and main heading | Lantern Dashboards |
| `tagline` | One-line description | See every metric in one calm view. |
| `preheader` | Hidden preview text | Now available to all Star Support customers. |
| `product_image_url` | Absolute `https://` URL of the product image | https://images.example.com/lantern-product.png |
| `product_image_alt` | Alt text for the product image | Dashboard showing three charts on a light background |
| `feature_1` | Feature line 1 | Drag-and-drop report layouts |
| `feature_2` | Feature line 2 | Scheduled email summaries |
| `feature_3` | Feature line 3 | Shared views with your whole team |
| `cta_label` | Button text | Try it free |
| `cta_url` | Button link, absolute `https://` | https://www.example.com/lantern |
| `company_address` | Footer postal address | 100 Example Way, Springfield, IL 62701 |

Subject hint: `Introducing {{ product_name }}`

### `plain_update.html`

| Variable | Description | Example |
|---|---|---|
| `update_title` | Subject line only (not repeated in the body) | Your account update for August |
| `preheader` | Hidden preview text | A short note about your account. |
| `first_name` | Greeting | Dana |
| `body_text` | One or two short plain-text paragraphs | We wanted to let you know that your billing address was updated on August 3. No action is needed. |
| `link_label` | Text of the single inline link | your account settings |
| `link_url` | Link target, absolute `https://` | https://account.example.com/settings |
| `company_address` | Footer postal address | 100 Example Way, Springfield, IL 62701 |

Subject hint: `{{ update_title }}`

## Pre-send checklist

- [ ] Render the template with real data (for example, one row from `contacts/`) and open the output. Do not judge a template by its raw placeholders.
- [ ] Verify every placeholder is filled. The rendered output must contain no `{{` and no `}}`.
- [ ] Check with images blocked. The copy, headline, CTA, and discount code must still read.
- [ ] Test in a dark-mode client (for example, the Gmail app and Apple Mail with dark appearance) and confirm no text disappears.
- [ ] Check the subject line with its variables filled in.
- [ ] Confirm every link and image URL is absolute `https://`.
- [ ] Confirm the footer shows the real postal address and that the server's unsubscribe footer appears before `</body>` in the rendered output.
- [ ] Keep the rendered HTML under about 100KB.
- [ ] Send a test to a seed address you control before sending to a list.
