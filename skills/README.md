# Skills

Three skills that drive the `email-marketing` MCP server. They live here so they are
version-controlled alongside the server they describe, and are symlinked into
`~/.claude/skills/` so Claude Code picks them up:

```sh
for s in marketing-template-author marketing-campaign-send marketing-list-prep; do
    ln -sfn "/Users/daniel.kennett/projects/email-mcp/skills/$s" "$HOME/.claude/skills/$s"
done
```

| Skill | Use it when | Ships |
|---|---|---|
| `marketing-template-author` | Creating or editing an HTML template | `scripts/validate_template.py`, `references/email-html-constraints.md` |
| `marketing-list-prep` | Turning an export into a usable contact list | `scripts/normalize_list.py` |
| `marketing-campaign-send` | Actually sending | `references/deliverability.md` |

The two scripts are standalone: stdlib only, no project imports, and they run on the
system `python3` (3.9) without the venv. The server itself needs 3.12.

```sh
python3 skills/marketing-template-author/scripts/validate_template.py          # all templates
python3 skills/marketing-list-prep/scripts/normalize_list.py export.csv -o list.csv --report-only
```

A typical pass is list-prep, then template-author, then campaign-send. The send skill is
the one with teeth: it refuses a real send while `UNSUBSCRIBE_BASE_URL` is unset, and it
treats the recipient count as something a human must confirm.

Names are prefixed `marketing-` so they do not collide with the separate `email-followups`
skill, which triages personal Gmail and has nothing to do with this server.
