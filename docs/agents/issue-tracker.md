# Issue tracker: GitHub

Issues and PRDs for this repository live in GitHub Issues at
`TH1NKING/predictive-hpa`. Use the `gh` CLI from the repository root.

## Conventions

- Create: `gh issue create --title "..." --body "..."`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open`
- Comment: `gh issue comment <number> --body "..."`
- Label: `gh issue edit <number> --add-label "..."`
- Close: `gh issue close <number> --comment "..."`

Infer the repository from `git remote`; use explicit `--repo TH1NKING/predictive-hpa`
when running outside the clone.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## Skill terminology

When a skill says "publish to the issue tracker," create a GitHub issue.
When it says "fetch the relevant ticket," run
`gh issue view <number> --comments`.
