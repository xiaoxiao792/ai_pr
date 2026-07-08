# Combined Plus Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add a new `/plus_review` command that combines high-level review risk analysis with multi-chunk implementation bug scanning.

**Architecture:** Keep `/review` and `/improve` unchanged. Add a new `PlusPRReviewer` tool class that runs a risk-review stage and an implementation-scan stage, then renders one combined persistent comment with its own header. Reuse existing diff, model, YAML, and suggestion-scoring helpers where practical.

**Tech Stack:** Python 3.12+, pytest, Dynaconf TOML prompt/settings files, Jinja2 prompts, existing PR-Agent GitProvider and LiteLLM abstractions.

---

### Task 1: Route `plus_review` To A New Tool

**Files:**
- Modify: `pr_agent/agent/pr_agent.py`
- Create: `pr_agent/tools/pr_plus_reviewer.py`
- Test: `tests/unittest/test_pr_agent_routing.py`

- [x] **Step 1: Write the failing routing test**

Add a test that patches `pr_agent.agent.pr_agent.PlusPRReviewer`, invokes `PRAgent().handle_request(url, ["plus_review"])`, and asserts the new class is instantiated. Also assert `["review"]` still instantiates `PRReviewer`.

- [x] **Step 2: Run the routing test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='.'; .\.conda\python.exe -m pytest --assert=plain tests\unittest\test_pr_agent_routing.py -q
```

Expected: failure because `PlusPRReviewer` does not exist or `plus_review` still maps to `PRReviewer`.

- [x] **Step 3: Add the new tool shell and route**

Create `pr_agent/tools/pr_plus_reviewer.py`:

```python
from functools import partial

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler


class PlusPRReviewer:
    def __init__(self, pr_url: str, args: list = None, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        self.pr_url = pr_url
        self.args = args
        self.ai_handler = ai_handler

    async def run(self):
        return None
```

Modify `pr_agent/agent/pr_agent.py`:

```python
from pr_agent.tools.pr_plus_reviewer import PlusPRReviewer

command2class = {
    ...
    "review": PRReviewer,
    "plus_review": PlusPRReviewer,
    ...
}
```

- [x] **Step 4: Run the routing test to verify it passes**

Run the same pytest command. Expected: route tests pass.

- [x] **Step 5: Commit**

```powershell
git add pr_agent/agent/pr_agent.py pr_agent/tools/pr_plus_reviewer.py tests/unittest/test_pr_agent_routing.py
git commit -m "feat: route plus_review to combined reviewer"
```

### Task 2: Add Risk Review Stage

**Files:**
- Modify: `pr_agent/tools/pr_plus_reviewer.py`
- Create: `pr_agent/settings/plus_review_prompts.toml`
- Modify: `pr_agent/config_loader.py`
- Test: `tests/unittest/test_pr_plus_reviewer.py`

- [x] **Step 1: Write failing tests for risk-stage prompt execution**

Create `tests/unittest/test_pr_plus_reviewer.py` with a fake GitProvider and fake AI handler. Assert `_prepare_risk_review()` calls `get_pr_diff`, renders `plus_review_prompt`, parses YAML, and stores a `risk_review` dictionary.

- [x] **Step 2: Run the new test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='.'; .\.conda\python.exe -m pytest --assert=plain tests\unittest\test_pr_plus_reviewer.py -q
```

Expected: failure because risk-stage methods and prompt are missing.

- [x] **Step 3: Add prompt file and load it**

Add `pr_agent/settings/plus_review_prompts.toml` with a `[plus_review_prompt]` section. The prompt must ask for:

```yaml
risk_review:
  estimated_effort_to_review_[1-5]: |
    ...
  relevant_tests: |
    ...
  design_architecture_integration_risks:
    - relevant_file: |
        ...
      risk_header: |
        ...
      risk_content: |
        ...
      start_line: ...
      end_line: ...
  security_concerns: |
    ...
  review_checklist:
    - ...
```

Modify `config_loader.py` settings files list to include:

```python
"settings/plus_review_prompts.toml",
```

- [x] **Step 4: Implement the risk stage**

In `PlusPRReviewer`, initialize provider, language, vars, token handler, and AI handler similarly to `PRReviewer`. Add `_prepare_risk_review(model)` that calls `get_pr_diff(..., add_line_numbers_to_hunks=True)`, renders `plus_review_prompt.system/user`, calls `chat_completion`, and parses YAML with `load_yaml`.

- [x] **Step 5: Run risk-stage tests**

Run the new test file. Expected: pass.

- [x] **Step 6: Commit**

```powershell
git add pr_agent/tools/pr_plus_reviewer.py pr_agent/settings/plus_review_prompts.toml pr_agent/config_loader.py tests/unittest/test_pr_plus_reviewer.py
git commit -m "feat: add plus_review risk review stage"
```

### Task 3: Add Implementation Scan Stage

**Files:**
- Modify: `pr_agent/tools/pr_plus_reviewer.py`
- Test: `tests/unittest/test_pr_plus_reviewer.py`

- [x] **Step 1: Write failing tests for implementation scan**

Add a test that stubs `get_pr_multi_diffs`, a fake AI handler response for `code_suggestions`, and a fake reflection response. Assert the implementation stage returns scored findings with file, line range, suggestion content, and improved code.

- [x] **Step 2: Run the test to verify it fails**

Run:

```powershell
$env:PYTHONPATH='.'; .\.conda\python.exe -m pytest --assert=plain tests\unittest\test_pr_plus_reviewer.py -q
```

Expected: failure because implementation scan is not implemented.

- [x] **Step 3: Reuse `PRCodeSuggestions` internals conservatively**

Implement `_prepare_implementation_findings(model)` by creating a `PRCodeSuggestions` instance for the same PR URL and invoking its `prepare_prediction_main(model)`. This intentionally preserves `/improve` quality and behavior in the first version.

Map the returned `code_suggestions` into plus-review findings without deduping:

```python
implementation_findings = data.get("code_suggestions", [])
```

- [x] **Step 4: Run implementation tests**

Run the test file. Expected: pass.

- [x] **Step 5: Commit**

```powershell
git add pr_agent/tools/pr_plus_reviewer.py tests/unittest/test_pr_plus_reviewer.py
git commit -m "feat: add plus_review implementation scan"
```

### Task 4: Render And Publish Combined Report

**Files:**
- Modify: `pr_agent/tools/pr_plus_reviewer.py`
- Test: `tests/unittest/test_pr_plus_reviewer.py`

- [x] **Step 1: Write failing renderer/publisher tests**

Add tests asserting `_render_report()` includes:

- `## PR Review Report`
- `Review Summary`
- `Design / Architecture / Integration Risks`
- `Implementation Findings`
- `Suggested Review Checklist`

Add a test that `run()` publishes with a `/plus_review`-specific persistent header and does not use the `/review` header.

- [x] **Step 2: Run tests to verify failure**

Run:

```powershell
$env:PYTHONPATH='.'; .\.conda\python.exe -m pytest --assert=plain tests\unittest\test_pr_plus_reviewer.py -q
```

- [x] **Step 3: Implement renderer**

Render risk findings and implementation findings as Markdown. For implementation findings, reuse the same basic shape as `/improve`: summary, file/line link, suggestion content, diff block from `existing_code` to `improved_code`, score, and why.

- [x] **Step 4: Implement publisher**

In `run()`, call both stages through `retry_with_fallback_models`, render the report, then publish with:

```python
PRCodeSuggestions.publish_persistent_comment_with_history(
    self.git_provider,
    report,
    initial_header="## PR Review Report",
    update_header=True,
    name="plus_review",
    final_update_message=False,
    max_previous_comments=get_settings().pr_code_suggestions.max_history_len,
)
```

- [x] **Step 5: Run renderer/publisher tests**

Expected: pass.

- [x] **Step 6: Commit**

```powershell
git add pr_agent/tools/pr_plus_reviewer.py tests/unittest/test_pr_plus_reviewer.py
git commit -m "feat: publish combined plus_review report"
```

### Task 5: Integration Verification

**Files:**
- Modify as needed based on failing tests.

- [x] **Step 1: Run targeted tests**

```powershell
$env:PYTHONPATH='.'; .\.conda\python.exe -m pytest --assert=plain tests\unittest\test_pr_agent_routing.py tests\unittest\test_pr_plus_reviewer.py -q
```

- [x] **Step 2: Run related review/improve tests**

```powershell
$env:PYTHONPATH='.'; .\.conda\python.exe -m pytest --assert=plain tests\unittest\test_pr_reviewer_core.py tests\unittest\test_pr_code_suggestions_core.py tests\unittest\test_pr_code_suggestions_rendering.py -q
```

- [x] **Step 3: Inspect diff**

Confirm the diff only touches:

- `pr_agent/agent/pr_agent.py`
- `pr_agent/config_loader.py`
- `pr_agent/tools/pr_plus_reviewer.py`
- `pr_agent/settings/plus_review_prompts.toml`
- targeted unit tests

- [x] **Step 4: Final commit if needed**

```powershell
git status --short
git add <remaining-files>
git commit -m "test: verify combined plus_review command"
```
