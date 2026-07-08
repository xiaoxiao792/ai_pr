# Combined /plus_review Design

## Goal

Create a new `/plus_review` command that combines the current `/review` architecture/integration risk review with the current `/improve` multi-chunk implementation scan.

The existing `/review` and `/improve` commands must keep their current behavior.

## Current Behavior

- `/review` maps to `PRReviewer`. It performs one regular review pass over a token-fitted diff, asks for high-level review findings, and updates a persistent review comment.
- `/improve` maps to `PRCodeSuggestions`. It splits the PR diff into multiple chunks, asks for concrete code suggestions per chunk, runs a self-reflection pass to score and locate suggestions, and publishes a suggestions comment with history.
- `plus_review` currently maps to `PRReviewer` as an alias of `/review`.

## New Behavior

`/plus_review` should map to a new command class, tentatively `PlusPRReviewer`.

The command runs two stages:

1. Risk review stage
   - Based on the existing `/review` flow.
   - Uses PR metadata, repo context, commit messages, and a token-fitted diff.
   - Focuses on design, architecture, interface contracts, integration, configuration, test gaps, and other reviewer focus areas.

2. Implementation scan stage
   - Based on the existing `/improve` flow.
   - Uses multi-chunk diff scanning.
   - Reuses the code-suggestion self-reflection pass for scoring and line location.
   - Focuses on concrete implementation defects such as undefined variables, incompatible function signatures, placeholder code, missing error handling, suspicious loops, and incorrect control flow.

The final output should be one combined PR comment with sections:

- Review Summary
- Design / Architecture / Integration Risks
- Implementation Findings
- Suggested Review Checklist

## Reuse Strategy

Do not refactor the whole review/improve system in the first implementation.

Reuse existing behavior where practical:

- Keep `PRReviewer` untouched for `/review`.
- Keep `PRCodeSuggestions` untouched for `/improve`.
- Reuse `get_pr_diff` for the risk review stage.
- Reuse `get_pr_multi_diffs` and the code-suggestion reflection behavior for the implementation scan stage.
- Add a dedicated `/plus_review` prompt instead of overloading the existing `/review` prompt.

## Publishing

`/plus_review` should not overwrite `/review` comments.

Use a distinct persistent header, for example:

```markdown
## PR Review Report
```

Prefer history-preserving behavior similar to `/improve`, so repeated `/plus_review` runs keep previous reports folded below the latest result.

## Initial Non-Goals

- Do not change `/review` behavior.
- Do not change `/improve` behavior.
- Do not remove the existing `/plus_review` command name; replace its alias behavior with the new combined command.
- Do not optimize or reduce the number of model calls in the first version.
- Do not try to deduplicate all overlap between risk findings and implementation findings in the first version.

## Tests

Add focused unit tests for:

- `plus_review` routes to the new command class.
- `review` still routes to `PRReviewer`.
- The combined command invokes both the risk review stage and implementation scan stage.
- The combined renderer includes risk findings and implementation findings.
- Repeated publishing uses a `/plus_review`-specific header rather than the `/review` header.
