from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncio
import pytest

from pr_agent.algo.types import FilePatchInfo
from pr_agent.tools import pr_plus_reviewer as pr_plus_reviewer_module
from pr_agent.tools.pr_plus_reviewer import PlusPRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


class FakeAIHandler:
    def __init__(self, response=""):
        self.main_pr_language = None
        self.response = response
        self.calls = []

    async def chat_completion(self, model, temperature, system, user):
        self.calls.append({
            "model": model,
            "temperature": temperature,
            "system": system,
            "user": user,
        })
        return self.response, "stop"


class FailingAIHandler:
    def __init__(self, exc):
        self.main_pr_language = None
        self.exc = exc
        self.calls = []

    async def chat_completion(self, model, temperature, system, user):
        self.calls.append({
            "model": model,
            "temperature": temperature,
            "system": system,
            "user": user,
        })
        raise self.exc


class FakeAIHandlerFactory:
    def __init__(self, responses):
        self.responses = list(responses)
        self.handlers = []

    def __call__(self):
        response = self.responses.pop(0) if self.responses else ""
        if isinstance(response, Exception):
            handler = FailingAIHandler(response)
        else:
            handler = FakeAIHandler(response)
        self.handlers.append(handler)
        return handler


def _provider():
    provider = MagicMock()
    provider.pr = SimpleNamespace(title="Fix payment retry")
    provider.get_languages.return_value = {"Python": 1}
    provider.get_files.return_value = ["service.py"]
    provider.get_pr_branch.return_value = "feature/retry"
    provider.get_pr_description.return_value = ("Adds retry logic.", [])
    provider.get_num_of_files.return_value = 1
    provider.get_commit_messages.return_value = "fix: retry failed payments"
    provider.is_supported.return_value = True
    return provider


def _risk_review_response(header, verdict="partially_correct"):
    return f"""
risk_review:
  estimated_effort_to_review_[1-5]: |
    3
  relevant_tests: |
    No
  change_logic_assessment:
    verdict: |
      {verdict}
    reasoning: |
      The retry change matches the stated intent, but the diff does not show any retry limit.
    logic_gaps:
      - relevant_file: |
          service.py
        issue_header: |
          {header}
        issue_content: |
          The PR says it adds safe retry behavior, but the shown loop can retry forever.
        start_line: 10
        end_line: 18
  design_architecture_integration_risks:
    - relevant_file: |
        service.py
      risk_header: |
        {header}
      risk_content: |
        The new retry loop can amplify downstream failures.
      start_line: 10
      end_line: 18
  security_concerns: |
    No
  review_checklist:
    - Confirm retry backoff behavior.
"""


def _invalid_risk_review_response():
    return """
risk_review:
  estimated_effort_to_review_[1-5]: |
    3
  design_architecture_integration_risks:
    - relevant_file: |
        service.py
      risk_header: |
        Broken YAML
      risk_content: `unquoted code span breaks yaml`
"""


def _stage1_report_review_response(header, confirmed_summary="Limit retry attempts"):
    return f"""
stage1_report_review:
  risk_review:
    estimated_effort_to_review_[1-5]: |
      3
    relevant_tests: |
      No
    change_logic_assessment:
      verdict: |
        partially_correct
      reasoning: |
        The report item is valid after checking the diff.
      logic_gaps:
        - relevant_file: |
            service.py
          issue_header: |
            {header}
          issue_content: |
            The retry logic has no visible bound in the changed code.
          start_line: 10
          end_line: 18
    design_architecture_integration_risks:
      - relevant_file: |
          service.py
        risk_header: |
          {header}
        risk_content: |
          The unbounded retry can amplify downstream failures.
        start_line: 10
        end_line: 18
    security_concerns: |
      No
    review_checklist:
      - Confirm retry limit and backoff.
  confirmed_findings:
    - relevant_file: |
        service.py
      relevant_lines_start: 10
      relevant_lines_end: 18
      one_sentence_summary: |
        {confirmed_summary}
      label: |
        possible bug
      suggestion_content: |
        Add a maximum retry count and backoff.
      existing_code: |
        while True:
            retry()
      improved_code: |
        for _ in range(3):
            retry()
      score: 9
      score_why: |
        Prevents runaway retries.
  discarded_items:
    - Noisy item without diff support.
"""


def _stage2_report_review_response(summary):
    return f"""
stage2_report_review:
  code_suggestions:
    - relevant_file: |
        service.py
      relevant_lines_start: 10
      relevant_lines_end: 12
      one_sentence_summary: |
        {summary}
      label: |
        possible bug
      suggestion_content: |
        Keep only implementation findings supported by the diff.
      existing_code: |
        while True:
            retry()
      improved_code: |
        for _ in range(3):
            retry()
      score: 9
      score_why: |
        Confirmed by report review.
  discarded_items:
    - Implementation suggestion not supported by the diff.
"""


@pytest.mark.asyncio
async def test_prepare_risk_review_runs_three_independent_stage_one_reports(monkeypatch):
    provider = _provider()
    ai_factory = FakeAIHandlerFactory([
        _risk_review_response("Missing retry bound"),
        _risk_review_response("Retry storm"),
        _risk_review_response("Missing retry bound"),
    ])

    monkeypatch.setattr(pr_plus_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_plus_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_plus_reviewer_module, "get_skills_context", lambda: "skill rules")
    monkeypatch.setattr(pr_plus_reviewer_module, "build_repo_context", lambda git_provider: "repo rules")
    monkeypatch.setattr(pr_plus_reviewer_module, "TokenHandler", MagicMock())
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "get_pr_diff",
        lambda git_provider, token_handler, model, add_line_numbers_to_hunks, disable_extra_lines: "diff body",
    )
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "retry_with_fallback_models",
        lambda callback, model_type: callback("gpt-test"),
    )

    snapshot = snapshot_settings(["plus_review.enable_stage1_report_review"])
    try:
        pr_plus_reviewer_module.get_settings().set("plus_review.enable_stage1_report_review", False)
        tool = PlusPRReviewer("https://example/pr/1", ai_handler=ai_factory)

        data = await tool._prepare_risk_review("gpt-test")

        assert "plan1_report1" in data
        assert "plan1_report2" in data
        assert "plan1_report3" in data
        assert len(data["stage1_reports"]) == 3
        assert str(data["risk_review"]["estimated_effort_to_review_[1-5]"]).strip() == "3"
        assert data["risk_review"]["change_logic_assessment"]["verdict"].strip() == "partially_correct"
        assert [
            gap["issue_header"].strip()
            for gap in data["risk_review"]["change_logic_assessment"]["logic_gaps"]
        ] == ["Missing retry bound", "Retry storm"]
        assert [
            risk["risk_header"].strip()
            for risk in data["risk_review"]["design_architecture_integration_risks"]
        ] == ["Missing retry bound", "Retry storm"]
        assert len(ai_factory.handlers) == 3
        stage1_handlers = ai_factory.handlers
        assert all(len(handler.calls) == 1 for handler in stage1_handlers)
        assert stage1_handlers[0] is not stage1_handlers[1]
        assert stage1_handlers[1] is not stage1_handlers[2]
        assert stage1_handlers[0].calls[0]["model"] == "gpt-test"
        assert "Fix payment retry" in stage1_handlers[0].calls[0]["user"]
        assert "feature/retry" in stage1_handlers[0].calls[0]["user"]
        assert "fix: retry failed payments" in stage1_handlers[0].calls[0]["user"]
        assert "diff body" in stage1_handlers[0].calls[0]["user"]
        assert "skill rules" in stage1_handlers[0].calls[0]["system"]
        assert "repo rules" in stage1_handlers[0].calls[0]["system"]
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_prepare_risk_review_retries_unparseable_stage_one_report(monkeypatch):
    provider = _provider()
    ai_factory = FakeAIHandlerFactory([
        _invalid_risk_review_response(),
        _risk_review_response("Recovered retry bound"),
    ])

    monkeypatch.setattr(pr_plus_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_plus_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_plus_reviewer_module, "get_skills_context", lambda: "skill rules")
    monkeypatch.setattr(pr_plus_reviewer_module, "build_repo_context", lambda git_provider: "repo rules")
    monkeypatch.setattr(pr_plus_reviewer_module, "TokenHandler", MagicMock())
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "get_pr_diff",
        lambda git_provider, token_handler, model, add_line_numbers_to_hunks, disable_extra_lines: "diff body",
    )
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "retry_with_fallback_models",
        lambda callback, model_type: callback("gpt-test"),
    )

    snapshot = snapshot_settings([
        "plus_review.enable_stage1_report_review",
        "plus_review.stage1_num_agents",
        "plus_review.agent_retry_count",
        "plus_review.agent_retry_delay_seconds",
    ])
    try:
        pr_plus_reviewer_module.get_settings().set("plus_review.enable_stage1_report_review", False)
        pr_plus_reviewer_module.get_settings().set("plus_review.stage1_num_agents", 1)
        pr_plus_reviewer_module.get_settings().set("plus_review.agent_retry_count", 2)
        pr_plus_reviewer_module.get_settings().set("plus_review.agent_retry_delay_seconds", 0)
        tool = PlusPRReviewer("https://example/pr/1", ai_handler=ai_factory)

        data = await tool._prepare_risk_review("gpt-test")
    finally:
        restore_settings(snapshot)

    assert len(ai_factory.handlers) == 2
    assert len(data["stage1_reports"]) == 1
    assert data["risk_review"]["design_architecture_integration_risks"][0]["risk_header"].strip() == (
        "Recovered retry bound"
    )


@pytest.mark.asyncio
async def test_prepare_risk_review_reviews_stage_one_reports_before_merging(monkeypatch):
    provider = _provider()
    ai_factory = FakeAIHandlerFactory([
        _risk_review_response("Noisy raw finding"),
        _risk_review_response("Missing retry bound"),
        _risk_review_response("Noisy raw finding"),
        _stage1_report_review_response("Reviewed retry bound"),
        _stage1_report_review_response("Reviewed retry bound"),
        _stage1_report_review_response("Reviewed retry bound"),
    ])

    monkeypatch.setattr(pr_plus_reviewer_module, "get_git_provider_with_context", lambda pr_url: provider)
    monkeypatch.setattr(pr_plus_reviewer_module, "get_main_pr_language", lambda languages, files: "Python")
    monkeypatch.setattr(pr_plus_reviewer_module, "get_skills_context", lambda: "skill rules")
    monkeypatch.setattr(pr_plus_reviewer_module, "build_repo_context", lambda git_provider: "repo rules")
    monkeypatch.setattr(pr_plus_reviewer_module, "TokenHandler", MagicMock())
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "get_pr_diff",
        lambda git_provider, token_handler, model, add_line_numbers_to_hunks, disable_extra_lines: "diff body",
    )
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "retry_with_fallback_models",
        lambda callback, model_type: callback("gpt-test"),
    )

    tool = PlusPRReviewer("https://example/pr/1", ai_handler=ai_factory)

    data = await tool._prepare_risk_review("gpt-test")

    assert "plan1_report_review1" in data
    assert "plan1_report_review2" in data
    assert "plan1_report_review3" in data
    assert len(data["stage1_report_reviews"]) == 3
    assert [
        gap["issue_header"].strip()
        for gap in data["risk_review"]["change_logic_assessment"]["logic_gaps"]
    ] == ["Reviewed retry bound"]
    assert data["confirmed_findings"][0]["one_sentence_summary"].strip() == "Limit retry attempts"
    assert len(ai_factory.handlers) == 6
    assert "Noisy raw finding" in ai_factory.handlers[3].calls[0]["user"]
    assert "diff body" in ai_factory.handlers[3].calls[0]["user"]


@pytest.mark.asyncio
async def test_prepare_implementation_findings_reuses_code_suggestions_pipeline(monkeypatch):
    class FakeCodeSuggestions:
        instances = []

        def __init__(self, pr_url, args=None, ai_handler=None):
            self.pr_url = pr_url
            self.args = args
            self.ai_handler = ai_handler
            FakeCodeSuggestions.instances.append(self)

        async def prepare_prediction_main(self, model):
            self.model = model
            return {
                "code_suggestions": [{
                    "relevant_file": "service.py",
                    "relevant_lines_start": 10,
                    "relevant_lines_end": 12,
                    "one_sentence_summary": f"Bound retry attempts {len(FakeCodeSuggestions.instances)}",
                    "label": "possible bug",
                    "suggestion_content": "Limit the retry loop.",
                    "existing_code": "while True:\n    retry()",
                    "improved_code": "for _ in range(3):\n    retry()",
                    "score": 9,
                    "score_why": "Prevents unbounded retries.",
                }]
            }

    monkeypatch.setattr(pr_plus_reviewer_module, "PRCodeSuggestions", FakeCodeSuggestions)
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "retry_with_fallback_models",
        lambda callback, model_type: callback("gpt-test"),
    )
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "get_pr_diff",
        lambda git_provider, token_handler, model, add_line_numbers_to_hunks, disable_extra_lines: "diff body",
    )
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = MagicMock()
    tool.token_handler = MagicMock()
    tool.pr_url = "https://example/pr/1"
    tool.args = ["--flag"]
    tool.ai_handler_factory = "fake-ai-factory"
    tool.stage2_num_agents = 3

    snapshot = snapshot_settings(["plus_review.enable_stage2_report_review"])
    try:
        pr_plus_reviewer_module.get_settings().set("plus_review.enable_stage2_report_review", False)
        findings = await tool._prepare_implementation_findings("gpt-test")
    finally:
        restore_settings(snapshot)

    assert "plan2_report1" in tool.implementation_report_data
    assert "plan2_report3" in tool.implementation_report_data
    assert len(tool.implementation_report_data["stage2_reports"]) == 3
    assert len(findings) == 3
    assert findings[0]["one_sentence_summary"] == "Bound retry attempts 1"
    assert findings[-1]["one_sentence_summary"] == "Bound retry attempts 3"
    assert len(FakeCodeSuggestions.instances) == 3
    assert FakeCodeSuggestions.instances[0].pr_url == "https://example/pr/1"
    assert FakeCodeSuggestions.instances[0].args == ["--flag"]
    assert FakeCodeSuggestions.instances[0].ai_handler == "fake-ai-factory"
    assert FakeCodeSuggestions.instances[0].model == "gpt-test"


def test_merge_final_implementation_findings_combines_stage_one_and_stage_two_findings():
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    risk_review_data = {
        "confirmed_findings": [{
            "relevant_file": "service.py",
            "relevant_lines_start": 10,
            "relevant_lines_end": 18,
            "one_sentence_summary": "Limit retry attempts",
            "label": "possible bug",
            "suggestion_content": "Add a maximum retry count and backoff.",
            "existing_code": "while True:\n    retry()",
            "improved_code": "for _ in range(3):\n    retry()",
            "score": 9,
            "score_why": "Prevents runaway retries.",
        }]
    }
    implementation_findings = [{
        "relevant_file": "service.py",
        "relevant_lines_start": 30,
        "relevant_lines_end": 32,
        "one_sentence_summary": "Add jitter to retry",
        "label": "performance",
        "suggestion_content": "Add jitter to reduce synchronized retries.",
        "existing_code": "sleep(1)",
        "improved_code": "sleep(1 + random())",
        "score": 7,
        "score_why": "Reduces retry spikes.",
    }]

    findings = tool._merge_final_implementation_findings(risk_review_data, implementation_findings)

    assert [finding["one_sentence_summary"] for finding in findings] == [
        "Limit retry attempts",
        "Add jitter to retry",
    ]


def test_deduplicate_findings_by_exact_location_keeps_highest_score():
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    findings = [
        {
            "relevant_file": "src/fcl.c",
            "relevant_lines_start": 2762,
            "relevant_lines_end": 2762,
            "one_sentence_summary": "Verify page variable",
            "label": "possible issue",
            "suggestion_content": "Confirm page source.",
            "existing_code": "GET_PROG_TYPE(page)",
            "improved_code": "GET_PROG_TYPE(temp_addr.page)",
            "score": 6,
            "score_why": "Needs confirmation.",
        },
        {
            "relevant_file": "src/fcl.c",
            "relevant_lines_start": 2762,
            "relevant_lines_end": 2762,
            "one_sentence_summary": "Fix undefined page",
            "label": "compile error",
            "suggestion_content": "Use temp_addr.page.",
            "existing_code": "GET_PROG_TYPE(page)",
            "improved_code": "GET_PROG_TYPE(temp_addr.page)",
            "score": 10,
            "score_why": "Compile failure.",
        },
    ]

    deduplicated = tool._deduplicate_findings_by_location(findings)

    assert len(deduplicated) == 1
    assert deduplicated[0]["score"] == 10
    assert deduplicated[0]["one_sentence_summary"] == "Fix undefined page"


def test_deduplicate_findings_by_overlapping_location_and_same_fix():
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    findings = [
        {
            "relevant_file": "src/fcl.c",
            "relevant_lines_start": 2761,
            "relevant_lines_end": 2763,
            "one_sentence_summary": "Fix page in block",
            "label": "possible bug",
            "suggestion_content": "Use temp_addr.page.",
            "existing_code": "GET_PROG_TYPE(page)",
            "improved_code": "GET_PROG_TYPE(temp_addr.page)",
            "score": 8,
            "score_why": "Compile failure.",
        },
        {
            "relevant_file": "src/fcl.c",
            "relevant_lines_start": 2762,
            "relevant_lines_end": 2762,
            "one_sentence_summary": "Fix exact page line",
            "label": "compile error",
            "suggestion_content": "Use temp_addr.page.",
            "existing_code": "GET_PROG_TYPE(page)",
            "improved_code": "GET_PROG_TYPE(temp_addr.page)",
            "score": 8,
            "score_why": "More precise location.",
        },
    ]

    deduplicated = tool._deduplicate_findings_by_location(findings)

    assert len(deduplicated) == 1
    assert deduplicated[0]["relevant_lines_start"] == 2762
    assert deduplicated[0]["relevant_lines_end"] == 2762


@pytest.mark.asyncio
async def test_final_deduplication_merges_cross_category_duplicates(monkeypatch):
    risk_review_data = {
        "risk_review": {
            "estimated_effort_to_review_[1-5]": "4",
            "relevant_tests": "No",
            "change_logic_assessment": {
                "verdict": "partially_correct",
                "reasoning": "Need address fixes.",
                "logic_gaps": [
                    {
                        "relevant_file": "src/fcl.c",
                        "issue_header": "plane variable needs verification",
                        "issue_content": "NOP_PROG_SP uses plane.",
                        "code_snippet": "NOP_PROG_SP(plane, addr.loc)",
                        "start_line": 704,
                        "end_line": 705,
                    }
                ],
            },
            "design_architecture_integration_risks": [],
            "security_concerns": "No",
            "review_checklist": [],
        }
    }
    implementation_findings = [
        {
            "relevant_file": "src/fcl.c",
            "relevant_lines_start": 705,
            "relevant_lines_end": 705,
            "one_sentence_summary": "fcl_cmd_prog_notslc_sp_eng uses undefined plane",
            "label": "possible bug",
            "suggestion_content": "Replace plane with addr.pln.",
            "existing_code": "NOP_PROG_SP(plane, addr.loc)",
            "improved_code": "NOP_PROG_SP(addr.pln, addr.loc)",
            "score": 9,
            "score_why": "Compile failure.",
        },
        {
            "relevant_file": "src/fcl.c",
            "relevant_lines_start": 704,
            "relevant_lines_end": 705,
            "one_sentence_summary": "Fix undefined plane in fcl_cmd_prog_notslc_sp_eng",
            "label": "compile error",
            "suggestion_content": "Use addr.pln instead of plane.",
            "existing_code": "NOP_PROG_SP(plane, addr.loc)",
            "improved_code": "NOP_PROG_SP(addr.pln, addr.loc)",
            "score": 10,
            "score_why": "Same root cause, higher confidence.",
        },
    ]
    response = """
final_review:
  risk_review:
    estimated_effort_to_review_[1-5]: |
      4
    relevant_tests: |
      \u5426
    change_logic_assessment:
      verdict: |
        partially_correct
      reasoning: |
        \u5730\u5740\u5f15\u64ce\u6539\u9020\u4ecd\u6709\u5fc5\u987b\u4fee\u590d\u7684\u7f16\u8bd1\u95ee\u9898\u3002
      logic_gaps: []
    design_architecture_integration_risks: []
    security_concerns: |
      \u5426
    review_checklist: []
  code_suggestions:
    - relevant_file: |
        src/fcl.c
      relevant_lines_start: 704
      relevant_lines_end: 705
      one_sentence_summary: |
        fcl_cmd_prog_notslc_sp_eng \u4f7f\u7528\u672a\u5b9a\u4e49\u7684 plane
      label: |
        compile error
      suggestion_content: |
        \u5c06 plane \u66ff\u6362\u4e3a addr.pln\u3002
      existing_code: |
        NOP_PROG_SP(plane, addr.loc)
      improved_code: |
        NOP_PROG_SP(addr.pln, addr.loc)
      score: 10
      score_why: |
        \u540c\u4e00\u6839\u56e0\u5df2\u5408\u5e76\uff0c\u4fdd\u7559\u6700\u9ad8\u4e25\u91cd\u7ea7\u3002
  discarded_items:
    - duplicate plane finding
"""
    ai_factory = FakeAIHandlerFactory([response])
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "retry_with_fallback_models",
        lambda callback, model_type: callback("gpt-test"),
    )
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.ai_handler_factory = ai_factory
    tool.main_language = "C"
    tool.vars = {"title": "Address engine", "diff": ""}
    tool.patches_diff = "diff body"

    final_risk_review, final_findings = await tool._deduplicate_and_classify_final_review(
        risk_review_data,
        implementation_findings,
    )

    assert final_risk_review["risk_review"]["change_logic_assessment"]["logic_gaps"] == []
    assert len(final_findings) == 1
    assert final_findings[0]["score"] == 10
    assert "fcl_cmd_prog_notslc_sp_eng" in final_findings[0]["one_sentence_summary"]
    assert "NOP_PROG_SP(plane, addr.loc)" in ai_factory.handlers[0].calls[0]["user"]


@pytest.mark.asyncio
async def test_final_evidence_review_rechecks_candidates_with_code_context(monkeypatch):
    risk_review_data = {
        "risk_review": {
            "estimated_effort_to_review_[1-5]": "4",
            "relevant_tests": "No",
            "change_logic_assessment": {
                "verdict": "partially_correct",
                "reasoning": "Retry behavior changed.",
                "logic_gaps": [{
                    "relevant_file": "service.py",
                    "issue_header": "Retry limit needs confirmation",
                    "issue_content": "The PR intent says safe retry, verify the retry bound.",
                    "code_snippet": "while True:\n    retry()",
                    "start_line": 10,
                    "end_line": 11,
                }],
            },
            "design_architecture_integration_risks": [],
            "security_concerns": "No",
            "review_checklist": [],
        }
    }
    implementation_findings = [{
        "relevant_file": "service.py",
        "relevant_lines_start": 10,
        "relevant_lines_end": 11,
        "one_sentence_summary": "Retry loop can run forever",
        "label": "possible bug",
        "suggestion_content": "Add a maximum retry count.",
        "existing_code": "while True:\n    retry()",
        "improved_code": "for _ in range(3):\n    retry()",
        "score": 9,
        "score_why": "The changed code has no exit condition.",
    }]
    response = """
final_review:
  risk_review:
    estimated_effort_to_review_[1-5]: |
      4
    relevant_tests: |
      否
    change_logic_assessment:
      verdict: |
        partially_correct
      reasoning: |
        复核后确认无限重试是代码证据明确的问题，不再作为人工确认项保留。
      logic_gaps: []
    design_architecture_integration_risks: []
    security_concerns: |
      否
    review_checklist: []
  code_suggestions:
    - relevant_file: |
        service.py
      relevant_lines_start: 10
      relevant_lines_end: 11
      one_sentence_summary: |
        重试循环缺少退出条件
      label: |
        bug
      suggestion_content: |
        增加最大重试次数，避免失败场景无限循环。
      existing_code: |
        while True:
            retry()
      improved_code: |
        for _ in range(3):
            retry()
      score: 9
      score_why: |
        结合 diff 和附近代码确认没有退出条件。
  discarded_items:
    - 已将人工确认项合并为明确 bug。
"""
    ai_factory = FakeAIHandlerFactory([response, response])

    async def fake_retry(callback, model_type):
        return await callback("gpt-test")

    provider = MagicMock()
    provider.get_diff_files.return_value = [FilePatchInfo(
        "def run():\n    count = 0\n    while should_retry():\n        retry()\n",
        "def run():\n    count = 0\n    while True:\n        retry()\n",
        "@@ -1,4 +1,4 @@\n def run():\n     count = 0\n"
        "-    while should_retry():\n+    while True:\n         retry()\n",
        "service.py",
    )]
    monkeypatch.setattr(pr_plus_reviewer_module, "retry_with_fallback_models", fake_retry)
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = provider
    tool.ai_handler_factory = ai_factory
    tool.main_language = "Python"
    tool.vars = {
        "title": "Make retry safer",
        "branch": "feature/retry",
        "description": "Adds safe retry behavior.",
        "commit_messages_str": "fix: retry failed payments",
    }
    tool.patches_diff = "diff body with retry loop"

    final_risk_review, final_findings = await tool._review_final_evidence(
        risk_review_data,
        implementation_findings,
    )

    assert len(ai_factory.handlers) == 2
    first_prompt = ai_factory.handlers[0].calls[0]["user"]
    second_prompt = ai_factory.handlers[1].calls[0]["user"]
    assert "Single candidate after merge/dedup/classification" in first_prompt
    assert "Evidence context for this candidate location" in first_prompt
    assert "Single candidate after merge/dedup/classification" in second_prompt
    assert "Evidence context for this candidate location" in second_prompt
    assert "Retry loop can run forever" in first_prompt
    assert "Retry limit needs confirmation" not in first_prompt
    assert "Retry limit needs confirmation" in second_prompt
    assert "Retry loop can run forever" not in second_prompt
    assert "while True" in first_prompt
    assert "while should_retry" in first_prompt
    assert "diff body with retry loop" in first_prompt
    assert final_risk_review["risk_review"]["change_logic_assessment"]["logic_gaps"] == []
    assert len(final_findings) == 1
    assert final_findings[0]["label"].strip() == "bug"
    assert "退出条件" in final_findings[0]["one_sentence_summary"]


@pytest.mark.asyncio
async def test_prepare_implementation_findings_does_not_depend_on_stage_one_findings(monkeypatch):
    class FakeCodeSuggestions:
        async def prepare_prediction_main(self, model):
            return {
                "code_suggestions": [{
                    "relevant_file": "service.py",
                    "relevant_lines_start": 30,
                    "relevant_lines_end": 32,
                    "one_sentence_summary": "Add jitter to retry",
                    "label": "performance",
                    "suggestion_content": "Add jitter to reduce synchronized retries.",
                    "existing_code": "sleep(1)",
                    "improved_code": "sleep(1 + random())",
                    "score": 7,
                    "score_why": "Reduces retry spikes.",
                }]
            }

        def __init__(self, pr_url, args=None, ai_handler=None):
            pass

    monkeypatch.setattr(pr_plus_reviewer_module, "PRCodeSuggestions", FakeCodeSuggestions)
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "get_pr_diff",
        lambda git_provider, token_handler, model, add_line_numbers_to_hunks, disable_extra_lines: "diff body",
    )
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = MagicMock()
    tool.token_handler = MagicMock()
    tool.pr_url = "https://example/pr/1"
    tool.args = []
    tool.ai_handler_factory = "fake-ai-factory"
    tool.stage1_confirmed_findings = [{
        "relevant_file": "service.py",
        "relevant_lines_start": 10,
        "relevant_lines_end": 18,
        "one_sentence_summary": "Limit retry attempts",
        "label": "possible bug",
        "suggestion_content": "Add a maximum retry count and backoff.",
        "existing_code": "while True:\n    retry()",
        "improved_code": "for _ in range(3):\n    retry()",
        "score": 9,
        "score_why": "Prevents runaway retries.",
    }]

    snapshot = snapshot_settings(["plus_review.enable_stage2_report_review"])
    try:
        pr_plus_reviewer_module.get_settings().set("plus_review.enable_stage2_report_review", False)
        findings = await tool._prepare_implementation_findings("gpt-test")
    finally:
        restore_settings(snapshot)

    assert [finding["one_sentence_summary"] for finding in findings] == [
        "Add jitter to retry",
    ]


@pytest.mark.asyncio
async def test_prepare_implementation_findings_reviews_stage_two_reports_before_merging(monkeypatch):
    class FakeCodeSuggestions:
        instances = []

        def __init__(self, pr_url, args=None, ai_handler=None):
            FakeCodeSuggestions.instances.append(self)

        async def prepare_prediction_main(self, model):
            return {
                "code_suggestions": [{
                    "relevant_file": "service.py",
                    "relevant_lines_start": 10,
                    "relevant_lines_end": 12,
                    "one_sentence_summary": f"Raw implementation finding {len(FakeCodeSuggestions.instances)}",
                    "label": "possible bug",
                    "suggestion_content": "Raw suggestion.",
                    "existing_code": "while True:\n    retry()",
                    "improved_code": "for _ in range(3):\n    retry()",
                    "score": 9,
                    "score_why": "Raw scan score.",
                }]
            }

    ai_factory = FakeAIHandlerFactory([
        _stage2_report_review_response("Reviewed implementation finding"),
        _stage2_report_review_response("Reviewed implementation finding"),
        _stage2_report_review_response("Reviewed implementation finding"),
    ])
    monkeypatch.setattr(pr_plus_reviewer_module, "PRCodeSuggestions", FakeCodeSuggestions)
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "get_pr_diff",
        lambda git_provider, token_handler, model, add_line_numbers_to_hunks, disable_extra_lines: "diff body",
    )
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = MagicMock()
    tool.token_handler = MagicMock()
    tool.pr_url = "https://example/pr/1"
    tool.args = []
    tool.ai_handler_factory = ai_factory
    tool.main_language = "Python"
    tool.stage2_num_agents = 3
    tool.stage1_confirmed_findings = []
    tool.vars = {"title": "Fix payment retry", "diff": "", "date": "2026-07-07"}
    tool.patches_diff = "diff body"

    findings = await tool._prepare_implementation_findings("gpt-test")

    assert "plan2_report_review1" in tool.implementation_report_data
    assert "plan2_report_review3" in tool.implementation_report_data
    assert len(tool.implementation_report_data["stage2_report_reviews"]) == 3
    assert [finding["one_sentence_summary"].strip() for finding in findings] == [
        "Reviewed implementation finding",
    ]
    assert len(ai_factory.handlers) == 3
    assert "Raw implementation finding 1" in ai_factory.handlers[0].calls[0]["user"]
    assert "diff body" in ai_factory.handlers[0].calls[0]["user"]


@pytest.mark.asyncio
async def test_prepare_implementation_findings_propagates_failed_stage_two_agents(monkeypatch):
    class FakeCodeSuggestions:
        instances = []

        def __init__(self, pr_url, args=None, ai_handler=None):
            FakeCodeSuggestions.instances.append(self)

        async def prepare_prediction_main(self, model):
            if len(FakeCodeSuggestions.instances) >= 2:
                raise RuntimeError("gateway timeout")
            return {
                "code_suggestions": [{
                    "relevant_file": "service.py",
                    "relevant_lines_start": 10,
                    "relevant_lines_end": 12,
                    "one_sentence_summary": f"Bound retry attempts {len(FakeCodeSuggestions.instances)}",
                    "label": "possible bug",
                    "suggestion_content": "Limit the retry loop.",
                    "existing_code": "while True:\n    retry()",
                    "improved_code": "for _ in range(3):\n    retry()",
                    "score": 9,
                    "score_why": "Prevents unbounded retries.",
                }]
            }

    monkeypatch.setattr(pr_plus_reviewer_module, "PRCodeSuggestions", FakeCodeSuggestions)
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "retry_with_fallback_models",
        lambda callback, model_type: callback("gpt-test"),
    )
    monkeypatch.setattr(
        pr_plus_reviewer_module,
        "get_pr_diff",
        lambda git_provider, token_handler, model, add_line_numbers_to_hunks, disable_extra_lines: "diff body",
    )
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = MagicMock()
    tool.token_handler = MagicMock()
    tool.pr_url = "https://example/pr/1"
    tool.args = []
    tool.ai_handler_factory = "fake-ai-factory"
    tool.stage2_num_agents = 3

    snapshot = snapshot_settings(["plus_review.enable_stage2_report_review"])
    try:
        pr_plus_reviewer_module.get_settings().set("plus_review.enable_stage2_report_review", False)
        with pytest.raises(RuntimeError, match="gateway timeout"):
            await tool._prepare_implementation_findings("gpt-test")
    finally:
        restore_settings(snapshot)


def _risk_review_data():
    return {
        "risk_review": {
            "estimated_effort_to_review_[1-5]": "3",
            "relevant_tests": "No",
            "change_logic_assessment": {
                "verdict": "partially_correct",
                "reasoning": "The retry change matches the stated intent, but it does not bound retries.",
                "logic_gaps": [{
                    "relevant_file": "service.py",
                    "issue_header": "Missing retry bound",
                    "issue_content": "The PR says retry should be safe, but the loop can continue forever.",
                    "code_snippet": "while True:\n    retry()",
                    "start_line": 10,
                    "end_line": 18,
                }],
            },
            "design_architecture_integration_risks": [{
                "relevant_file": "service.py",
                "risk_header": "Retry storm",
                "risk_content": "The new retry loop can amplify downstream failures.",
                "code_snippet": "while True:\n    retry()",
                "start_line": 10,
                "end_line": 18,
            }],
            "security_concerns": "No",
            "review_checklist": ["Confirm retry backoff behavior."],
        }
    }


def _implementation_findings():
    return [
        {
            "relevant_file": "service.py",
            "relevant_lines_start": 10,
            "relevant_lines_end": 12,
            "one_sentence_summary": "Bound retry attempts",
            "label": "possible bug",
            "suggestion_content": "Limit the retry loop.",
            "existing_code": "while True:\n    retry()",
            "improved_code": "for _ in range(3):\n    retry()",
            "score": 9,
            "score_why": "Prevents unbounded retries.",
        },
        {
            "relevant_file": "service.py",
            "relevant_lines_start": 20,
            "relevant_lines_end": 24,
            "one_sentence_summary": "Extract retry delay",
            "label": "maintainability",
            "suggestion_content": "Move the retry delay into a named constant.",
            "existing_code": "sleep(3)",
            "improved_code": "sleep(RETRY_DELAY_SECONDS)",
            "score": 5,
            "score_why": "Makes retry configuration clearer.",
        },
    ]


@pytest.mark.asyncio
async def test_agent_sequence_runs_one_agent_at_a_time():
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    active_agents = 0
    max_active_agents = 0
    run_order = []

    async def fake_agent(index):
        nonlocal active_agents, max_active_agents
        active_agents += 1
        max_active_agents = max(max_active_agents, active_agents)
        run_order.append(index)
        await asyncio.sleep(0)
        active_agents -= 1
        return {"index": index}

    results = await tool._run_agent_sequence([
        lambda: fake_agent(1),
        lambda: fake_agent(2),
        lambda: fake_agent(3),
    ])

    assert max_active_agents == 1
    assert run_order == [1, 2, 3]
    assert results == [{"index": 1}, {"index": 2}, {"index": 3}]


@pytest.mark.asyncio
async def test_agent_sequence_retries_only_the_failed_agent(monkeypatch):
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    call_order = []
    second_agent_attempts = 0

    async def first_agent():
        call_order.append("first")
        return {"index": 1}

    async def second_agent():
        nonlocal second_agent_attempts
        second_agent_attempts += 1
        call_order.append(f"second-{second_agent_attempts}")
        if second_agent_attempts == 1:
            raise RuntimeError("gateway timeout")
        return {"index": 2}

    async def third_agent():
        call_order.append("third")
        return {"index": 3}

    snapshot = snapshot_settings([
        "plus_review.agent_retry_count",
        "plus_review.agent_retry_delay_seconds",
    ])
    try:
        pr_plus_reviewer_module.get_settings().set("plus_review.agent_retry_count", 2)
        pr_plus_reviewer_module.get_settings().set("plus_review.agent_retry_delay_seconds", 0)

        results = await tool._run_agent_sequence([
            first_agent,
            second_agent,
            third_agent,
        ])
    finally:
        restore_settings(snapshot)

    assert call_order == ["first", "second-1", "second-2", "third"]
    assert results == [{"index": 1}, {"index": 2}, {"index": 3}]


@pytest.mark.asyncio
async def test_agent_sequence_raises_after_failed_agent_retries(monkeypatch):
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    failing_attempts = 0

    async def failing_agent():
        nonlocal failing_attempts
        failing_attempts += 1
        raise RuntimeError("gateway timeout")

    snapshot = snapshot_settings([
        "plus_review.agent_retry_count",
        "plus_review.agent_retry_delay_seconds",
    ])
    try:
        pr_plus_reviewer_module.get_settings().set("plus_review.agent_retry_count", 3)
        pr_plus_reviewer_module.get_settings().set("plus_review.agent_retry_delay_seconds", 0)

        with pytest.raises(RuntimeError, match="gateway timeout"):
            await tool._run_agent_sequence([failing_agent])
    finally:
        restore_settings(snapshot)

    assert failing_attempts == 3


@pytest.mark.asyncio
async def test_plus_review_agents_share_global_concurrency_limit():
    tool1 = PlusPRReviewer.__new__(PlusPRReviewer)
    tool2 = PlusPRReviewer.__new__(PlusPRReviewer)
    active_agents = 0
    max_active_agents = 0

    class SlowAIHandler:
        main_pr_language = None

        async def chat_completion(self, model, temperature, system, user):
            nonlocal active_agents, max_active_agents
            active_agents += 1
            max_active_agents = max(max_active_agents, active_agents)
            await asyncio.sleep(0)
            active_agents -= 1
            return _risk_review_response("Missing retry bound"), "stop"

    for tool in (tool1, tool2):
        tool.vars = {
            "title": "Fix payment retry",
            "branch": "feature/retry",
            "description": "Adds retry logic.",
            "language": "Python",
            "diff": "",
            "num_pr_files": 1,
            "extra_instructions": "",
            "skills_context": "",
            "repo_context": "",
            "commit_messages_str": "fix: retry failed payments",
            "is_ai_metadata": False,
            "date": "2026-07-07",
            "duplicate_prompt_examples": False,
        }
        tool.patches_diff = "diff body"
        tool.ai_handler_factory = SlowAIHandler
        tool.main_language = "Python"

    snapshot = snapshot_settings(["plus_review.max_agent_concurrency"])
    try:
        pr_plus_reviewer_module.get_settings().set("plus_review.max_agent_concurrency", 1)
        if hasattr(pr_plus_reviewer_module, "_PLUS_REVIEW_AGENT_SEMAPHORES"):
            pr_plus_reviewer_module._PLUS_REVIEW_AGENT_SEMAPHORES.clear()

        await asyncio.gather(
            tool1._prepare_single_risk_review("gpt-test"),
            tool2._prepare_single_risk_review("gpt-test"),
        )
    finally:
        if hasattr(pr_plus_reviewer_module, "_PLUS_REVIEW_AGENT_SEMAPHORES"):
            pr_plus_reviewer_module._PLUS_REVIEW_AGENT_SEMAPHORES.clear()
        restore_settings(snapshot)

    assert max_active_agents == 1


@pytest.mark.asyncio
async def test_prepare_single_implementation_report_waits_before_creating_subagent(monkeypatch):
    class FakeCodeSuggestions:
        instances = []

        def __init__(self, pr_url, args=None, ai_handler=None):
            FakeCodeSuggestions.instances.append(self)

        async def prepare_prediction_main(self, model):
            return {"code_suggestions": []}

    monkeypatch.setattr(pr_plus_reviewer_module, "PRCodeSuggestions", FakeCodeSuggestions)
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.pr_url = "https://example/pr/1"
    tool.args = []
    tool.ai_handler_factory = "fake-ai-factory"

    snapshot = snapshot_settings(["plus_review.max_agent_concurrency"])
    try:
        pr_plus_reviewer_module.get_settings().set("plus_review.max_agent_concurrency", 1)
        if hasattr(pr_plus_reviewer_module, "_PLUS_REVIEW_AGENT_SEMAPHORES"):
            pr_plus_reviewer_module._PLUS_REVIEW_AGENT_SEMAPHORES.clear()
        semaphore = tool._agent_semaphore()
        await semaphore.acquire()

        task = asyncio.create_task(tool._prepare_single_implementation_report("gpt-test"))
        await asyncio.sleep(0)

        assert FakeCodeSuggestions.instances == []

        semaphore.release()
        await task
    finally:
        if not task.done():
            task.cancel()
        if semaphore.locked():
            semaphore.release()
        if hasattr(pr_plus_reviewer_module, "_PLUS_REVIEW_AGENT_SEMAPHORES"):
            pr_plus_reviewer_module._PLUS_REVIEW_AGENT_SEMAPHORES.clear()
        restore_settings(snapshot)


def test_render_report_includes_risks_checklist_and_implementation_findings():
    git_provider = MagicMock()
    git_provider.get_line_link.return_value = "https://example.test/service.py#L10-L12"
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = git_provider

    report = tool._render_report(_risk_review_data(), _implementation_findings())

    assert "## Plus Review" in report
    assert "### \u4fee\u6539\u63cf\u8ff0" in report
    assert "### \u603b\u7ed3" in report
    assert "### \u5fc5\u987b\u4fee\u590d" in report
    assert "### \u9700\u8981\u4eba\u5de5\u786e\u8ba4" in report
    assert "### \u53ef\u9009\u4f18\u5316" in report
    assert "### \u626b\u63cf\u8fc7\u7a0b" in report
    assert "\u5efa\u8bae\u4eba\u5de5\u68c0\u67e5" not in report
    assert "\u5fc5\u987b\u4fee\u590d 1 \u4e2a" in report
    assert "\u4eba\u5de5\u786e\u8ba4 2 \u4e2a" in report
    assert "\u53ef\u9009\u4f18\u5316 1 \u4e2a" in report
    assert "需求清晰度" in report
    assert "场景覆盖" in report
    assert "兼容性" in report
    assert "设计合理性" in report
    assert "测试验收" in report
    assert "<table>" in report
    assert "<strong>\u5206\u7c7b</strong>" in report
    assert "<strong>\u95ee\u9898/\u5efa\u8bae</strong>" in report
    assert "<strong>\u5f71\u54cd</strong>" in report
    assert "<td>\u7591\u4f3c Bug</td>" in report
    assert "<td>\u4eba\u5de5\u786e\u8ba4</td>" in report
    assert "<td>\u53ef\u7ef4\u62a4\u6027</td>" in report
    assert "<details><summary>Bound retry attempts</summary>" in report
    assert "\u5efa\u8bae\u4fee\u6539" in report
    assert "```diff" in report
    assert "\u76f8\u5173\u4ee3\u7801" in report
    assert "\u5f53\u524d\u4ee3\u7801" in report
    assert "\u5efa\u8bae\u4ee3\u7801" in report
    assert "https://example.test/service.py#L10-L12" in report
    assert "\u90e8\u5206\u6b63\u786e" in report
    assert "Missing retry bound" in report
    assert "Retry storm" in report
    assert "Prevents unbounded retries." in report


@pytest.mark.asyncio
async def test_run_polishes_report_to_chinese_before_publishing(monkeypatch):
    provider = _provider()
    provider.get_files.return_value = ["service.py"]
    published = {}

    async def fake_retry(callback, model_type):
        return await callback("gpt-test")

    def fake_publish(pr_comment, initial_header, update_header, name, final_update_message):
        published["pr_comment"] = pr_comment

    monkeypatch.setattr(pr_plus_reviewer_module, "retry_with_fallback_models", fake_retry)
    provider.publish_persistent_comment.side_effect = fake_publish
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = provider
    tool.pr_url = "https://example/pr/1"
    tool.main_language = "Python"
    tool.ai_handler_factory = FakeAIHandlerFactory([
        "## Plus Review\n\n### \u603b\u7ed3\n\n- \u5168\u90e8\u4e3a\u4e2d\u6587\n"
    ])
    tool._prepare_risk_review = AsyncMock(return_value={"risk_review": {}})
    tool._prepare_implementation_findings = AsyncMock(return_value=[])
    tool._render_report = MagicMock(return_value="## Plus Review\n\n### Summary\n\n- No issue\n")

    snapshot = snapshot_settings(["config.publish_output", "plus_review.enable_final_chinese_polish"])
    try:
        pr_plus_reviewer_module.get_settings().set("config.publish_output", True)
        pr_plus_reviewer_module.get_settings().set("plus_review.enable_final_chinese_polish", True)

        await tool.run()

        assert "### \u603b\u7ed3" in published["pr_comment"]
        assert "Summary" not in published["pr_comment"]
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_run_publishes_combined_report_without_plus_review_history(monkeypatch):
    provider = _provider()
    provider.get_files.return_value = ["service.py"]
    published = {}

    async def fake_retry(callback, model_type):
        return await callback("gpt-test")

    def fake_publish(pr_comment, initial_header, update_header, name, final_update_message):
        published.update({
            "pr_comment": pr_comment,
            "initial_header": initial_header,
            "update_header": update_header,
            "name": name,
            "final_update_message": final_update_message,
        })

    monkeypatch.setattr(pr_plus_reviewer_module, "retry_with_fallback_models", fake_retry)
    provider.publish_persistent_comment.side_effect = fake_publish
    monkeypatch.setattr(
        pr_plus_reviewer_module.PRCodeSuggestions,
        "publish_persistent_comment_with_history",
        MagicMock(side_effect=AssertionError("plus_review should not preserve previous suggestions")),
    )
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = provider
    tool.pr_url = "https://example/pr/1"
    tool.risk_review = _risk_review_data()
    tool.implementation_findings = _implementation_findings()
    tool._prepare_risk_review = AsyncMock(return_value=tool.risk_review)
    tool._prepare_implementation_findings = AsyncMock(return_value=tool.implementation_findings)
    tool._render_report = MagicMock(return_value="## PR Review Report\n\nBody")

    snapshot = snapshot_settings(["config.publish_output"])
    try:
        pr_plus_reviewer_module.get_settings().set("config.publish_output", True)

        await tool.run()

        assert published["initial_header"] == "## PR Review Report"
        assert published["name"] == "plus_review"
        assert published["update_header"] is True
        assert published["final_update_message"] is False
        assert published["pr_comment"] == "## PR Review Report\n\nBody"
        provider.remove_initial_comment.assert_called_once_with()
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_run_prepares_stage_one_and_stage_two_in_parallel(monkeypatch):
    provider = _provider()
    provider.get_files.return_value = ["service.py"]
    stage1_started = asyncio.Event()
    stage2_started = asyncio.Event()
    started_order = []

    async def fake_retry(callback, model_type):
        return await callback("gpt-test")

    async def fake_prepare_risk_review():
        started_order.append("stage1")
        stage1_started.set()
        await stage2_started.wait()
        return _risk_review_data()

    async def fake_prepare_implementation_findings():
        started_order.append("stage2")
        stage2_started.set()
        await stage1_started.wait()
        return _implementation_findings()

    monkeypatch.setattr(pr_plus_reviewer_module, "retry_with_fallback_models", fake_retry)
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = provider
    tool._prepare_risk_review = fake_prepare_risk_review
    tool._prepare_implementation_findings = fake_prepare_implementation_findings
    tool._render_report = MagicMock(return_value="## PR Review Report\n\nBody")

    snapshot = snapshot_settings(["config.publish_output"])
    try:
        pr_plus_reviewer_module.get_settings().set("config.publish_output", False)

        await asyncio.wait_for(tool.run(), timeout=1)

        assert set(started_order) == {"stage1", "stage2"}
        assert pr_plus_reviewer_module.get_settings().data["artifact"] == "## PR Review Report\n\nBody"
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_run_does_not_retry_whole_stage(monkeypatch):
    provider = _provider()
    provider.get_files.return_value = ["service.py"]

    async def fail_if_stage_retry_is_used(callback, model_type):
        raise AssertionError("run should not retry an entire plus_review stage")

    monkeypatch.setattr(pr_plus_reviewer_module, "retry_with_fallback_models", fail_if_stage_retry_is_used)
    tool = PlusPRReviewer.__new__(PlusPRReviewer)
    tool.git_provider = provider
    tool._prepare_risk_review = AsyncMock(return_value=_risk_review_data())
    tool._prepare_implementation_findings = AsyncMock(return_value=_implementation_findings())
    tool._render_report = MagicMock(return_value="## PR Review Report\n\nBody")

    snapshot = snapshot_settings(["config.publish_output"])
    try:
        pr_plus_reviewer_module.get_settings().set("config.publish_output", False)

        await tool.run()

        tool._prepare_risk_review.assert_awaited_once_with()
        tool._prepare_implementation_findings.assert_awaited_once_with()
    finally:
        restore_settings(snapshot)
