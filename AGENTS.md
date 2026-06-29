# termux-tools — AGENTS.md

## Commit Message Rules

Write every commit message in this format:

```
type(scope): imperative description

[body paragraphs — wrap at 72 chars]

[footer tokens — one per line]
```

### Rules

1. **Subject line** — imperative mood, no period, max 72 chars. "add X" not "added X".
2. **Body** — blank line after subject. One idea per paragraph. Wrap at 72 chars. Explain *why*, not *what*.
3. **Footers** — blank line after body. Use `Token: value` or `Token: #value` format.

### Type selection

`feat` — new feature, `fix` — bug fix, `refactor` — restructure without behavior change, `test` — test changes, `docs` — docs only, `chore` — maintenance/deps, `perf` — performance, `revert` — undo commit (add `Refs: <sha>` footer). If a change spans multiple types, make separate commits.

### Scope

Use the affected module: `power_cycle`, `battery`, `load`, `dashboard`, `phases`, `cli`, `docs`, `tests`. Omit scope for project-wide changes.

### Breaking changes

Prefix with `!`: `feat!: description` or `feat(scope)!: description`. Or add `BREAKING CHANGE: description` as a footer.

### Examples

```
fix(battery): handle missing sysfs node gracefully

List available nodes and suggest adding the correct path to
CANDIDATE_NODES instead of silently failing.

Refs: #42
```

```
revert: undo 676104e

Refs: 676104e, a215868
```
