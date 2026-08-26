# Domain Docs

This is a single-context repository.

## Before exploring

The repository does not currently contain `CONTEXT.md` or `docs/adr/`. Create
them only when domain terminology or an architectural decision needs to be
recorded; do not create empty placeholders.

Read these when they exist:

- `CONTEXT.md` at the repository root.
- Relevant ADRs under `docs/adr/`.

If either is absent, proceed silently. The domain-modeling workflow creates
domain documentation lazily when terminology or architectural decisions are
resolved.

## Layout

- `CONTEXT.md` defines the project glossary and domain model.
- `docs/adr/NNNN-short-title.md` records architectural decisions.

Use terminology defined in `CONTEXT.md` in issues, tests, implementation, and
documentation. If proposed work conflicts with an ADR, identify the conflict
explicitly instead of silently overriding the decision.
