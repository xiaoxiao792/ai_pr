import asyncio
import copy
import difflib
import yaml
from datetime import datetime
from functools import partial

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.ai_handlers.base_ai_handler import BaseAiHandler
from pr_agent.algo.ai_handlers.litellm_ai_handler import LiteLLMAIHandler
from pr_agent.algo.pr_processing import get_pr_diff, retry_with_fallback_models
from pr_agent.algo.repo_context import build_repo_context
from pr_agent.algo.skills_loader import get_skills_context
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.algo.utils import ModelType, load_yaml
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.git_provider import get_main_pr_language
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions


_PLUS_REVIEW_AGENT_SEMAPHORES = {}


def _get_plus_review_agent_semaphore():
    max_agent_concurrency = int(get_settings().plus_review.get("max_agent_concurrency", 8))
    limit = max(1, max_agent_concurrency)
    loop = asyncio.get_running_loop()
    key = (id(loop), limit)
    if key not in _PLUS_REVIEW_AGENT_SEMAPHORES:
        _PLUS_REVIEW_AGENT_SEMAPHORES[key] = asyncio.Semaphore(limit)
    return _PLUS_REVIEW_AGENT_SEMAPHORES[key]


class PlusPRReviewer:
    def __init__(self, pr_url: str, args: list = None, ai_handler: partial[BaseAiHandler,] = LiteLLMAIHandler):
        self.git_provider = get_git_provider_with_context(pr_url)
        self.main_language = get_main_pr_language(
            self.git_provider.get_languages(), self.git_provider.get_files()
        )
        self.pr_url = pr_url
        self.args = args
        self.ai_handler_factory = ai_handler
        self.pr_description, self.pr_description_files = (
            self.git_provider.get_pr_description(split_changes_walkthrough=True)
        )
        self.vars = {
            "title": self.git_provider.pr.title,
            "branch": self.git_provider.get_pr_branch(),
            "description": self.pr_description,
            "language": self.main_language,
            "diff": "",
            "num_pr_files": self.git_provider.get_num_of_files(),
            "extra_instructions": get_settings().plus_review.extra_instructions,
            "skills_context": get_skills_context(),
            "repo_context": build_repo_context(self.git_provider),
            "commit_messages_str": self.git_provider.get_commit_messages(),
            "is_ai_metadata": get_settings().get("config.enable_ai_metadata", False),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "duplicate_prompt_examples": get_settings().config.get("duplicate_prompt_examples", False),
        }
        self.token_handler = TokenHandler(
            self.git_provider.pr,
            self.vars,
            get_settings().plus_review_prompt.system,
            get_settings().plus_review_prompt.user,
        )
        self.risk_review = None
        self.implementation_findings = []
        self.stage1_num_agents = int(get_settings().plus_review.get("stage1_num_agents", 3))
        self.stage2_num_agents = int(get_settings().plus_review.get("stage2_num_agents", 3))
        self.stage1_confirmed_findings = []
        self.implementation_report_data = {}

    async def run(self):
        if not self.git_provider.get_files():
            return None

        risk_review, implementation_findings = await asyncio.gather(
            self._prepare_risk_review(),
            self._prepare_implementation_findings(),
        )
        implementation_findings = self._merge_final_implementation_findings(risk_review, implementation_findings)
        risk_review, implementation_findings = await self._deduplicate_and_classify_final_review(
            risk_review or {}, implementation_findings or []
        )
        risk_review, implementation_findings = await self._review_final_evidence(
            risk_review or {}, implementation_findings or []
        )
        report = self._render_report(risk_review or {}, implementation_findings or [])
        report = await self._ensure_chinese_report(report)
        if get_settings().config.publish_output:
            self.git_provider.remove_initial_comment()
            self.git_provider.publish_persistent_comment(
                report,
                initial_header="## PR Review Report",
                update_header=True,
                name="plus_review",
                final_update_message=False,
            )
        else:
            get_settings().data = {"artifact": report}
        return None

    async def _deduplicate_and_classify_final_review(
        self,
        risk_review_data: dict,
        implementation_findings: list,
    ) -> tuple[dict, list]:
        if not get_settings().plus_review.get("enable_final_deduplication", True):
            return risk_review_data, self._deduplicate_findings_by_location(implementation_findings)
        try:
            return await retry_with_fallback_models(
                lambda fallback_model: self._deduplicate_single_final_review(
                    fallback_model,
                    risk_review_data,
                    implementation_findings,
                ),
                model_type=ModelType.REGULAR,
            )
        except Exception:
            return risk_review_data, self._deduplicate_findings_by_location(implementation_findings)

    async def _deduplicate_single_final_review(
        self,
        model: str,
        risk_review_data: dict,
        implementation_findings: list,
    ) -> tuple[dict, list]:
        candidate = {
            "risk_review": (risk_review_data or {}).get("risk_review", risk_review_data or {}),
            "code_suggestions": implementation_findings or [],
        }
        variables = copy.deepcopy(getattr(self, "vars", {}))
        variables["diff"] = getattr(self, "patches_diff", "") or getattr(self, "stage2_patches_diff", "")
        variables["final_review_candidate"] = yaml.safe_dump(
            candidate,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(
            get_settings().plus_review_final_deduplication_prompt.system
        ).render(variables)
        user_prompt = environment.from_string(
            get_settings().plus_review_final_deduplication_prompt.user
        ).render(variables)
        ai_handler = self.ai_handler_factory()
        ai_handler.main_pr_language = self.main_language
        response, finish_reason = await ai_handler.chat_completion(
            model=model,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt,
        )
        data = self._load_required_yaml(
            response.strip(),
            keys_fix_yaml=[
                "final_review:",
                "risk_review:",
                "code_suggestions:",
                "discarded_items:",
                "design_architecture_integration_risks:",
                "logic_gaps:",
                "relevant_file:",
                "relevant_lines_start:",
                "relevant_lines_end:",
                "one_sentence_summary:",
                "label:",
                "suggestion_content:",
                "existing_code:",
                "improved_code:",
                "score:",
                "score_why:",
            ],
            first_key="final_review",
            last_key="discarded_items",
        )
        final_review = data.get("final_review", data or {})
        final_risk_review = final_review.get("risk_review") or candidate["risk_review"]
        final_findings = final_review.get("code_suggestions") or []
        return {"risk_review": final_risk_review}, self._deduplicate_findings_by_location(final_findings)

    async def _review_final_evidence(
        self,
        risk_review_data: dict,
        implementation_findings: list,
    ) -> tuple[dict, list]:
        if not get_settings().plus_review.get("enable_final_evidence_review", True):
            return risk_review_data, self._deduplicate_findings_by_location(implementation_findings)
        if not hasattr(self, "ai_handler_factory"):
            return risk_review_data, self._deduplicate_findings_by_location(implementation_findings)
        candidate = {
            "risk_review": (risk_review_data or {}).get("risk_review", risk_review_data or {}),
            "code_suggestions": implementation_findings or [],
        }
        review_items = self._collect_final_evidence_items(candidate)
        if not review_items:
            return risk_review_data, []

        diff_files = self._diff_files_by_normalized_name()
        reviewed_items = await asyncio.gather(*[
            self._review_final_evidence_item(
                risk_review_data,
                item,
                index,
                diff_files,
            )
            for index, item in enumerate(review_items[:30], start=1)
        ])
        return self._merge_final_evidence_reviews(risk_review_data, reviewed_items)

    async def _review_final_evidence_item(
        self,
        risk_review_data: dict,
        item: dict,
        index: int,
        diff_files: dict,
    ) -> dict:
        try:
            return await retry_with_fallback_models(
                lambda fallback_model: self._review_single_final_evidence(
                    fallback_model,
                    risk_review_data,
                    item,
                    index,
                    diff_files,
                ),
                model_type=ModelType.REGULAR,
            )
        except Exception:
            return self._fallback_final_evidence_review(risk_review_data, item)

    async def _review_single_final_evidence(
        self,
        model: str,
        risk_review_data: dict,
        item: dict,
        index: int,
        diff_files: dict,
    ) -> dict:
        variables = copy.deepcopy(getattr(self, "vars", {}))
        variables["diff"] = getattr(self, "patches_diff", "") or getattr(self, "stage2_patches_diff", "")
        variables["final_review_candidate"] = yaml.safe_dump(
            {
                "candidate_id": index,
                "candidate_category": item.get("candidate_category"),
                "candidate": item.get("candidate_item", {}),
            },
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )
        variables["final_review_evidence_context"] = self._build_single_final_evidence_context(
            item,
            index,
            diff_files,
        )
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(
            get_settings().plus_review_final_evidence_review_prompt.system
        ).render(variables)
        user_prompt = environment.from_string(
            get_settings().plus_review_final_evidence_review_prompt.user
        ).render(variables)
        async with self._agent_semaphore():
            ai_handler = self.ai_handler_factory()
            ai_handler.main_pr_language = self.main_language
            response, finish_reason = await ai_handler.chat_completion(
                model=model,
                temperature=get_settings().config.temperature,
                system=system_prompt,
                user=user_prompt,
            )
        data = self._load_required_yaml(
            response.strip(),
            keys_fix_yaml=[
                "final_review:",
                "risk_review:",
                "code_suggestions:",
                "discarded_items:",
                "design_architecture_integration_risks:",
                "logic_gaps:",
                "relevant_file:",
                "relevant_lines_start:",
                "relevant_lines_end:",
                "one_sentence_summary:",
                "label:",
                "suggestion_content:",
                "existing_code:",
                "improved_code:",
                "score:",
                "score_why:",
                "risk_header:",
                "risk_content:",
                "issue_header:",
                "issue_content:",
                "code_snippet:",
                "start_line:",
                "end_line:",
            ],
            first_key="final_review",
            last_key="discarded_items",
        )
        return data.get("final_review", data or {})

    def _build_single_final_evidence_context(self, item: dict, index: int, diff_files: dict) -> str:
        relevant_file = self._clean_text(item.get("relevant_file"))
        start_line, end_line = self._item_line_range(item)
        diff_file = diff_files.get(self._normalize_path(relevant_file))
        evidence = {
            "candidate_id": index,
            "candidate_category": item.get("candidate_category"),
            "relevant_file": relevant_file,
            "start_line": start_line,
            "end_line": end_line,
            "summary": item.get("summary"),
            "claim": item.get("claim"),
            "existing_code": item.get("existing_code", ""),
            "improved_code": item.get("improved_code", ""),
        }
        if diff_file:
            evidence.update({
                "current_code_context": self._numbered_snippet(
                    getattr(diff_file, "head_file", ""), start_line, end_line
                ),
                "base_code_context": self._numbered_snippet(
                    getattr(diff_file, "base_file", ""), start_line, end_line
                ),
                "file_patch": self._clip_text(getattr(diff_file, "patch", ""), 6000),
            })
        return yaml.safe_dump(
            {"candidate_evidence": evidence},
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )

    def _merge_final_evidence_reviews(self, risk_review_data: dict, reviewed_items: list) -> tuple[dict, list]:
        risk_review = copy.deepcopy((risk_review_data or {}).get("risk_review", risk_review_data or {}))
        assessment = risk_review.get("change_logic_assessment") or {}
        assessment["logic_gaps"] = []
        risk_review["change_logic_assessment"] = assessment
        risk_review["design_architecture_integration_risks"] = []
        code_suggestions = []
        for reviewed_item in reviewed_items or []:
            final_review = reviewed_item.get("final_review", reviewed_item or {})
            reviewed_risk = final_review.get("risk_review") or {}
            reviewed_assessment = reviewed_risk.get("change_logic_assessment") or {}
            assessment["logic_gaps"].extend(reviewed_assessment.get("logic_gaps") or [])
            risk_review["design_architecture_integration_risks"].extend(
                reviewed_risk.get("design_architecture_integration_risks") or []
            )
            code_suggestions.extend(final_review.get("code_suggestions") or [])
        assessment["logic_gaps"] = self._deduplicate_items(assessment["logic_gaps"], header_key="issue_header")
        risk_review["design_architecture_integration_risks"] = self._deduplicate_items(
            risk_review["design_architecture_integration_risks"],
            header_key="risk_header",
        )
        return {"risk_review": risk_review}, self._deduplicate_findings_by_location(code_suggestions)

    def _fallback_final_evidence_review(self, risk_review_data: dict, item: dict) -> dict:
        risk_review = self._empty_review_shell(risk_review_data)
        source_type = item.get("source_type")
        candidate_item = copy.deepcopy(item.get("candidate_item") or {})
        if source_type == "code_suggestion":
            return {"risk_review": risk_review, "code_suggestions": [candidate_item], "discarded_items": []}
        if source_type == "logic_gap":
            risk_review["change_logic_assessment"]["logic_gaps"] = [candidate_item]
        elif source_type == "risk_item":
            risk_review["design_architecture_integration_risks"] = [candidate_item]
        return {"risk_review": risk_review, "code_suggestions": [], "discarded_items": []}

    def _empty_review_shell(self, risk_review_data: dict) -> dict:
        risk_review = copy.deepcopy((risk_review_data or {}).get("risk_review", risk_review_data or {}))
        assessment = risk_review.get("change_logic_assessment") or {}
        assessment["logic_gaps"] = []
        risk_review["change_logic_assessment"] = assessment
        risk_review["design_architecture_integration_risks"] = []
        return risk_review

    def _collect_final_evidence_items(self, candidate: dict) -> list:
        risk_review = (candidate or {}).get("risk_review") or {}
        items = []
        for finding in (candidate or {}).get("code_suggestions") or []:
            if not isinstance(finding, dict):
                continue
            if finding in self._filter_must_fix_findings([finding]):
                candidate_category = "must_fix_candidate"
            else:
                candidate_category = "optional_candidate"
            items.append({
                "source_type": "code_suggestion",
                "candidate_item": finding,
                "candidate_category": candidate_category,
                "relevant_file": finding.get("relevant_file", ""),
                "start_line": finding.get("relevant_lines_start"),
                "end_line": finding.get("relevant_lines_end"),
                "summary": finding.get("one_sentence_summary", ""),
                "claim": finding.get("suggestion_content") or finding.get("score_why", ""),
                "existing_code": finding.get("existing_code", ""),
                "improved_code": finding.get("improved_code", ""),
            })
        assessment = risk_review.get("change_logic_assessment") or {}
        for item in assessment.get("logic_gaps") or []:
            if isinstance(item, dict):
                items.append({
                    "source_type": "logic_gap",
                    "candidate_item": item,
                    "candidate_category": "manual_confirmation_candidate",
                    "relevant_file": item.get("relevant_file", ""),
                    "start_line": item.get("start_line"),
                    "end_line": item.get("end_line"),
                    "summary": item.get("issue_header", ""),
                    "claim": item.get("issue_content", ""),
                    "existing_code": item.get("code_snippet", ""),
                })
        for item in risk_review.get("design_architecture_integration_risks") or []:
            if isinstance(item, dict):
                items.append({
                    "source_type": "risk_item",
                    "candidate_item": item,
                    "candidate_category": "manual_confirmation_candidate",
                    "relevant_file": item.get("relevant_file", ""),
                    "start_line": item.get("start_line"),
                    "end_line": item.get("end_line"),
                    "summary": item.get("risk_header", ""),
                    "claim": item.get("risk_content", ""),
                    "existing_code": item.get("code_snippet", ""),
                })
        return items

    def _diff_files_by_normalized_name(self) -> dict:
        try:
            diff_files = self.git_provider.get_diff_files()
        except Exception:
            diff_files = []
        files_by_name = {}
        for diff_file in diff_files or []:
            for filename in (getattr(diff_file, "filename", None), getattr(diff_file, "old_filename", None)):
                normalized = self._normalize_path(filename)
                if normalized:
                    files_by_name[normalized] = diff_file
        return files_by_name

    def _item_line_range(self, item: dict) -> tuple:
        line_range = self._line_range(item, "start_line", "end_line")
        if line_range:
            return line_range
        line_range = self._line_range(item, "relevant_lines_start", "relevant_lines_end")
        if line_range:
            return line_range
        return None, None

    def _numbered_snippet(self, content: str, start_line, end_line, radius: int = 8) -> str:
        if not content or not start_line:
            return ""
        try:
            start = int(start_line)
            end = int(end_line or start)
        except (TypeError, ValueError):
            return ""
        lines = str(content).splitlines()
        if not lines:
            return ""
        first = max(1, min(start, end) - radius)
        last = min(len(lines), max(start, end) + radius)
        snippet = [f"{line_no}: {lines[line_no - 1]}" for line_no in range(first, last + 1)]
        return self._clip_text("\n".join(snippet), 6000)

    @staticmethod
    def _clip_text(value, max_chars: int) -> str:
        text = str(value or "")
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "\n..."

    @staticmethod
    def _normalize_path(value) -> str:
        return str(value or "").replace("\\", "/").strip().lower()

    async def _prepare_risk_review(self, model: str = None) -> dict:
        model = model or get_settings().config.model
        self.patches_diff = get_pr_diff(
            self.git_provider,
            self.token_handler,
            model,
            add_line_numbers_to_hunks=True,
            disable_extra_lines=False,
        )
        if not self.patches_diff:
            self.risk_review = {"risk_review": {}}
            return self.risk_review

        stage1_reports = await self._run_agent_sequence([
            lambda: retry_with_fallback_models(self._prepare_single_risk_review, model_type=ModelType.REGULAR)
            for _ in range(self.stage1_num_agents)
        ])
        if get_settings().plus_review.get("enable_stage1_report_review", True):
            stage1_report_reviews = await self._run_agent_sequence([
                lambda report=report: retry_with_fallback_models(
                    lambda fallback_model: self._review_single_stage1_report(fallback_model, report),
                    model_type=ModelType.REGULAR,
                )
                for report in stage1_reports
            ])
        else:
            stage1_report_reviews = []
        self.risk_review = self._merge_stage1_reports(stage1_reports, stage1_report_reviews)
        return self.risk_review

    async def _prepare_single_risk_review(self, model: str) -> dict:
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(get_settings().plus_review_prompt.system).render(variables)
        user_prompt = environment.from_string(get_settings().plus_review_prompt.user).render(variables)
        async with self._agent_semaphore():
            ai_handler = self.ai_handler_factory()
            ai_handler.main_pr_language = self.main_language
            response, finish_reason = await ai_handler.chat_completion(
                model=model,
                temperature=get_settings().config.temperature,
                system=system_prompt,
                user=user_prompt,
            )
        return self._load_required_yaml(
            response.strip(),
            keys_fix_yaml=[
                "estimated_effort_to_review_[1-5]:",
                "relevant_tests:",
                "change_logic_assessment:",
                "verdict:",
                "reasoning:",
                "logic_gaps:",
                "design_architecture_integration_risks:",
                "relevant_file:",
                "risk_header:",
                "issue_header:",
                "issue_content:",
                "risk_content:",
                "security_concerns:",
                "review_checklist:",
            ],
            first_key="risk_review",
            last_key="review_checklist",
        )

    async def _review_single_stage1_report(self, model: str, stage1_report: dict) -> dict:
        variables = copy.deepcopy(self.vars)
        variables["diff"] = self.patches_diff
        variables["stage1_report"] = stage1_report
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(
            get_settings().plus_review_stage1_report_review_prompt.system
        ).render(variables)
        user_prompt = environment.from_string(
            get_settings().plus_review_stage1_report_review_prompt.user
        ).render(variables)
        async with self._agent_semaphore():
            ai_handler = self.ai_handler_factory()
            ai_handler.main_pr_language = self.main_language
            response, finish_reason = await ai_handler.chat_completion(
                model=model,
                temperature=get_settings().config.temperature,
                system=system_prompt,
                user=user_prompt,
            )
        return self._load_required_yaml(
            response.strip(),
            keys_fix_yaml=[
                "stage1_report_review:",
                "risk_review:",
                "estimated_effort_to_review_[1-5]:",
                "relevant_tests:",
                "change_logic_assessment:",
                "verdict:",
                "reasoning:",
                "logic_gaps:",
                "design_architecture_integration_risks:",
                "confirmed_findings:",
                "discarded_items:",
                "relevant_file:",
                "risk_header:",
                "issue_header:",
                "issue_content:",
                "risk_content:",
                "security_concerns:",
                "review_checklist:",
                "relevant_lines_start:",
                "relevant_lines_end:",
                "one_sentence_summary:",
                "label:",
                "suggestion_content:",
                "existing_code:",
                "improved_code:",
                "score:",
                "score_why:",
            ],
            first_key="stage1_report_review",
            last_key="discarded_items",
        )

    def _merge_stage1_reports(self, stage1_reports: list, stage1_report_reviews: list = None) -> dict:
        merged = {"risk_review": {}}
        reports_by_name = {}
        parsed_reviews = []
        for index, report in enumerate(stage1_reports, start=1):
            report_name = f"plan1_report{index}"
            reports_by_name[report_name] = report
            risk_review = (report or {}).get("risk_review", report or {})
            if risk_review:
                parsed_reviews.append(risk_review)

        review_reports_by_name = {}
        parsed_report_reviews = []
        confirmed_findings = []
        discarded_items = []
        for index, report_review in enumerate(stage1_report_reviews or [], start=1):
            report_review_name = f"plan1_report_review{index}"
            review_reports_by_name[report_review_name] = report_review
            reviewed_report = (report_review or {}).get("stage1_report_review", report_review or {})
            if reviewed_report:
                parsed_report_reviews.append(reviewed_report)
                risk_review = reviewed_report.get("risk_review") or {}
                if risk_review:
                    parsed_reviews.append(risk_review)
                confirmed_findings.extend(reviewed_report.get("confirmed_findings") or [])
                discarded_items.extend(reviewed_report.get("discarded_items") or [])

        if parsed_report_reviews:
            parsed_reviews = [
                reviewed_report.get("risk_review") or {}
                for reviewed_report in parsed_report_reviews
                if reviewed_report.get("risk_review")
            ]

        if not parsed_reviews:
            merged.update(reports_by_name)
            merged.update(review_reports_by_name)
            merged["stage1_reports"] = stage1_reports
            merged["stage1_report_reviews"] = stage1_report_reviews or []
            merged["confirmed_findings"] = []
            merged["discarded_items"] = []
            return merged

        merged_review = {
            "estimated_effort_to_review_[1-5]": self._merge_effort(parsed_reviews),
            "relevant_tests": self._merge_text_field(parsed_reviews, "relevant_tests"),
            "change_logic_assessment": self._merge_change_logic_assessments(parsed_reviews),
            "design_architecture_integration_risks": self._deduplicate_items(
                self._flatten_field(parsed_reviews, "design_architecture_integration_risks"),
                header_key="risk_header",
            ),
            "security_concerns": self._merge_text_field(parsed_reviews, "security_concerns"),
            "review_checklist": self._deduplicate_texts(self._flatten_field(parsed_reviews, "review_checklist")),
        }
        merged["risk_review"] = merged_review
        merged["stage1_reports"] = stage1_reports
        merged["stage1_report_reviews"] = stage1_report_reviews or []
        merged["confirmed_findings"] = self._deduplicate_findings(confirmed_findings)
        merged["discarded_items"] = self._deduplicate_texts(discarded_items)
        merged.update(reports_by_name)
        merged.update(review_reports_by_name)
        self.stage1_confirmed_findings = merged["confirmed_findings"]
        return merged

    async def _prepare_implementation_findings(self, model: str = None) -> list:
        model = model or get_settings().config.model
        self.stage2_patches_diff = get_pr_diff(
            self.git_provider,
            self.token_handler,
            model,
            add_line_numbers_to_hunks=True,
            disable_extra_lines=False,
        )
        stage2_reports = await self._run_agent_sequence([
            lambda: retry_with_fallback_models(
                self._prepare_single_implementation_report,
                model_type=ModelType.REGULAR,
            )
            for _ in range(getattr(self, "stage2_num_agents", 3))
        ])
        if get_settings().plus_review.get("enable_stage2_report_review", True):
            stage2_report_reviews = await self._run_agent_sequence([
                lambda report=report: retry_with_fallback_models(
                    lambda fallback_model: self._review_single_stage2_report(fallback_model, report),
                    model_type=ModelType.REGULAR,
                )
                for report in stage2_reports
            ])
        else:
            stage2_report_reviews = []
        self.implementation_report_data = self._merge_stage2_reports(stage2_reports, stage2_report_reviews)
        self.implementation_findings = self.implementation_report_data.get("code_suggestions", [])
        return self.implementation_findings

    async def _prepare_single_implementation_report(self, model: str) -> dict:
        async with self._agent_semaphore():
            code_suggestions = PRCodeSuggestions(
                self.pr_url,
                args=self.args,
                ai_handler=self.ai_handler_factory,
            )
            return await code_suggestions.prepare_prediction_main(model)

    async def _review_single_stage2_report(self, model: str, stage2_report: dict) -> dict:
        variables = copy.deepcopy(self.vars)
        variables["diff"] = getattr(self, "stage2_patches_diff", "") or variables.get("diff", "")
        variables["stage2_report"] = stage2_report
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(
            get_settings().plus_review_stage2_report_review_prompt.system
        ).render(variables)
        user_prompt = environment.from_string(
            get_settings().plus_review_stage2_report_review_prompt.user
        ).render(variables)
        async with self._agent_semaphore():
            ai_handler = self.ai_handler_factory()
            ai_handler.main_pr_language = self.main_language
            response, finish_reason = await ai_handler.chat_completion(
                model=model,
                temperature=get_settings().config.temperature,
                system=system_prompt,
                user=user_prompt,
            )
        return self._load_required_yaml(
            response.strip(),
            keys_fix_yaml=[
                "stage2_report_review:",
                "code_suggestions:",
                "discarded_items:",
                "relevant_file:",
                "relevant_lines_start:",
                "relevant_lines_end:",
                "one_sentence_summary:",
                "label:",
                "suggestion_content:",
                "existing_code:",
                "improved_code:",
                "score:",
                "score_why:",
            ],
            first_key="stage2_report_review",
            last_key="discarded_items",
        )

    def _merge_stage2_reports(self, stage2_reports: list, stage2_report_reviews: list = None) -> dict:
        merged = {
            "stage2_reports": stage2_reports,
            "code_suggestions": [],
            "stage2_report_reviews": stage2_report_reviews or [],
            "discarded_items": [],
        }
        for index, report in enumerate(stage2_reports, start=1):
            merged[f"plan2_report{index}"] = report
            merged["code_suggestions"].extend((report or {}).get("code_suggestions", []))
        reviewed_suggestions = []
        for index, report_review in enumerate(stage2_report_reviews or [], start=1):
            merged[f"plan2_report_review{index}"] = report_review
            reviewed_report = (report_review or {}).get("stage2_report_review", report_review or {})
            reviewed_suggestions.extend(reviewed_report.get("code_suggestions") or [])
            merged["discarded_items"].extend(reviewed_report.get("discarded_items") or [])
        if stage2_report_reviews:
            merged["code_suggestions"] = reviewed_suggestions
        merged["code_suggestions"] = self._deduplicate_findings(merged["code_suggestions"])
        merged["discarded_items"] = self._deduplicate_texts(merged["discarded_items"])
        return merged

    def _merge_final_implementation_findings(self, risk_review_data: dict, implementation_findings: list) -> list:
        stage1_confirmed_findings = (risk_review_data or {}).get("confirmed_findings", [])
        return self._deduplicate_findings(stage1_confirmed_findings + (implementation_findings or []))

    async def _run_agent_sequence(self, agent_factories: list) -> list:
        results = []
        for agent_factory in agent_factories:
            result = await self._run_agent_with_retries(agent_factory)
            if result:
                results.append(result)
        return results

    async def _run_agent_with_retries(self, agent_factory):
        retry_count = max(1, int(get_settings().plus_review.get("agent_retry_count", 3)))
        retry_delay_seconds = max(0, float(get_settings().plus_review.get("agent_retry_delay_seconds", 5)))
        for attempt in range(1, retry_count + 1):
            try:
                return await agent_factory()
            except Exception:
                if attempt == retry_count:
                    raise
                if retry_delay_seconds:
                    await asyncio.sleep(retry_delay_seconds)
        return None

    def _load_required_yaml(self, response: str, keys_fix_yaml: list, first_key: str, last_key: str) -> dict:
        data = load_yaml(response, keys_fix_yaml=keys_fix_yaml, first_key=first_key, last_key=last_key)
        if not data:
            raise ValueError("plus_review agent returned YAML that could not be parsed")
        return data

    def _agent_semaphore(self):
        return _get_plus_review_agent_semaphore()

    async def _ensure_chinese_report(self, report: str) -> str:
        if not get_settings().plus_review.get("enable_final_chinese_polish", True):
            return report
        if not hasattr(self, "ai_handler_factory"):
            return report
        try:
            polished_report = await retry_with_fallback_models(
                lambda fallback_model: self._polish_report_to_chinese(fallback_model, report),
                model_type=ModelType.REGULAR,
            )
            return polished_report or report
        except Exception:
            return report

    async def _polish_report_to_chinese(self, model: str, report: str) -> str:
        variables = {"report": report}
        environment = Environment(undefined=StrictUndefined)
        system_prompt = environment.from_string(
            get_settings().plus_review_final_report_prompt.system
        ).render(variables)
        user_prompt = environment.from_string(
            get_settings().plus_review_final_report_prompt.user
        ).render(variables)
        ai_handler = self.ai_handler_factory()
        ai_handler.main_pr_language = self.main_language
        response, finish_reason = await ai_handler.chat_completion(
            model=model,
            temperature=get_settings().config.temperature,
            system=system_prompt,
            user=user_prompt,
        )
        response = self._strip_markdown_fence(response.strip())
        return response.rstrip() + "\n" if response else report

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```markdown") and stripped.endswith("```"):
            return stripped[len("```markdown"): -3].strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            first_newline = stripped.find("\n")
            if first_newline != -1:
                return stripped[first_newline + 1: -3].strip()
        return text

    def _render_report(self, risk_review_data: dict, implementation_findings: list) -> str:
        risk_review = risk_review_data.get("risk_review", risk_review_data or {})
        must_fix_findings = self._filter_must_fix_findings(implementation_findings)
        optional_findings = [finding for finding in implementation_findings if finding not in must_fix_findings]
        verification_items = self._collect_verification_items(risk_review)
        effort = self._clean_text(risk_review.get("estimated_effort_to_review_[1-5]", "N/A"))
        relevant_tests = self._translate_common_value(risk_review.get("relevant_tests", "N/A"))
        security_concerns = self._translate_common_value(risk_review.get("security_concerns", "\u65e0"))
        parts = ["## Plus Review", "", "### \u4fee\u6539\u63cf\u8ff0", ""]
        parts.extend(self._render_change_description(risk_review))
        parts.extend([
            "",
            "### \u603b\u7ed3",
            "",
            (
                f"- \u95ee\u9898\uff1a\u5fc5\u987b\u4fee\u590d {len(must_fix_findings)} \u4e2a\uff0c"
                f"\u4eba\u5de5\u786e\u8ba4 {len(verification_items)} \u4e2a\uff0c"
                f"\u53ef\u9009\u4f18\u5316 {len(optional_findings)} \u4e2a\u3002"
            ),
            (
                f"- \u8bc4\u5ba1\uff1a\u5de5\u4f5c\u91cf {effort}/5\uff0c"
                f"\u6d4b\u8bd5 {relevant_tests}\uff0c\u5b89\u5168 {security_concerns}\u3002"
            ),
        ])

        parts.extend(["", "### \u5fc5\u987b\u4fee\u590d", ""])
        parts.extend(self._render_finding_table(
            must_fix_findings,
            empty_text=(
                "- \u672a\u53d1\u73b0\u8bc1\u636e\u660e\u786e\u3001\u5fc5\u987b\u7acb\u5373"
                "\u4fee\u590d\u7684\u95ee\u9898\u3002"
            ),
            row_renderer=self._render_must_fix_row,
        ))

        parts.extend(["", "### \u9700\u8981\u4eba\u5de5\u786e\u8ba4", ""])
        parts.extend(self._render_verification_table(verification_items))

        parts.extend(["", "### \u53ef\u9009\u4f18\u5316", ""])
        parts.extend(self._render_finding_table(
            optional_findings,
            empty_text="- \u6682\u65e0\u53ef\u9009\u4f18\u5316\u5efa\u8bae\u3002",
            row_renderer=self._render_optional_finding_row,
        ))

        parts.extend(["", "### \u626b\u63cf\u8fc7\u7a0b", ""])
        parts.extend([
            f"- 阶段一：{getattr(self, 'stage1_num_agents', 3)} 个 agent，"
            "结合 PR 标题、描述、提交信息和 diff 理解需求与改动范围，"
            "审查需求清晰度、场景覆盖、边界情况、兼容性、设计合理性、"
            "实现逻辑、可读性与旧代码一致性、测试验收及依赖/资源/性能/安全等风险。",
            f"- 阶段二：{getattr(self, 'stage2_num_agents', 3)} 个 agent，"
            "聚焦具体代码实现缺陷，并生成可落地的修改建议。",
            "- \u5df2\u5408\u5e76\u91cd\u590d\u9879\uff1b"
            "\u4f4e\u53ef\u4fe1\u5185\u5bb9\u4e0d\u4f1a\u4f5c\u4e3a"
            "\u5fc5\u987b\u4fee\u590d\u9879\u5c55\u793a\u3002",
        ])

        return "\n".join(parts).rstrip() + "\n"

    def _render_change_description(self, risk_review: dict) -> list:
        assessment = risk_review.get("change_logic_assessment") or {}
        verdict = self._translate_common_value(assessment.get("verdict", "\u4e0d\u660e\u786e"))
        reasoning = self._summarize_text(
            assessment.get("reasoning", "\u672a\u751f\u6210\u660e\u786e\u7684\u6539\u52a8\u63cf\u8ff0\u3002"),
            max_chars=180,
        )
        return [
            f"- \u8303\u56f4\uff1a{reasoning}",
            f"- \u5224\u65ad\uff1a{verdict}",
        ]

    def _filter_must_fix_findings(self, findings: list) -> list:
        must_fix = []
        for finding in findings or []:
            try:
                score = int(finding.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            label = self._clean_text(finding.get("label", "")).lower()
            if score >= 8 or "bug" in label or "security" in label:
                must_fix.append(finding)
        return must_fix

    def _collect_verification_items(self, risk_review: dict) -> list:
        assessment = risk_review.get("change_logic_assessment") or {}
        logic_gaps = assessment.get("logic_gaps") or []
        risks = risk_review.get("design_architecture_integration_risks") or []
        return self._deduplicate_items(logic_gaps + risks, header_key="issue_header")

    def _render_finding_table(self, findings: list, empty_text: str, row_renderer) -> list:
        if not findings:
            return [empty_text]
        lines = self._table_header()
        for finding in findings:
            lines.extend(row_renderer(finding))
        lines.append("</tbody></table>")
        return lines

    def _render_verification_table(self, items: list) -> list:
        if not items:
            return ["- \u672a\u53d1\u73b0\u9700\u8981\u5355\u72ec\u4eba\u5de5\u786e\u8ba4\u7684\u98ce\u9669\u3002"]
        lines = self._table_header()
        for item in items:
            lines.extend(self._render_verification_row(item))
        lines.append("</tbody></table>")
        return lines

    @staticmethod
    def _table_header() -> list:
        return [
            "<table>",
            "<thead><tr><td><strong>\u5206\u7c7b</strong></td>"
            "<td><strong>\u95ee\u9898/\u5efa\u8bae</strong></td><td><strong>\u5f71\u54cd</strong></td></tr></thead>",
            "<tbody>",
        ]

    def _render_must_fix_row(self, finding: dict) -> list:
        details = self._finding_details(finding, include_patch=True, include_code_snippet=False)
        return self._render_table_row(
            self._translate_label(finding.get("label", "\u5fc5\u987b\u4fee\u590d")),
            details,
            self._clean_text(finding.get("score", "N/A")),
        )

    def _render_optional_finding_row(self, finding: dict) -> list:
        details = self._finding_details(finding, include_patch=False, include_code_snippet=True)
        return self._render_table_row(
            self._translate_label(finding.get("label", "\u53ef\u9009\u4f18\u5316")),
            details,
            self._clean_text(finding.get("score", "N/A")),
        )

    def _finding_details(self, finding: dict, include_patch: bool, include_code_snippet: bool) -> str:
        relevant_file = self._clean_text(finding.get("relevant_file", ""))
        start_line = finding.get("relevant_lines_start")
        end_line = finding.get("relevant_lines_end", start_line)
        location = self._line_reference(relevant_file, start_line, end_line)
        summary = self._clean_text(finding.get("one_sentence_summary", "\u4ee3\u7801\u95ee\u9898"))
        suggestion = self._clean_text(finding.get("suggestion_content", ""))
        score_why = self._clean_text(finding.get("score_why", ""))
        existing_code = self._clean_text(finding.get("existing_code", ""))
        improved_code = self._clean_text(finding.get("improved_code", ""))
        patch = self._diff(existing_code, improved_code)
        details = [
            f"<details><summary>{summary}</summary>",
            "",
            f"- \u4f4d\u7f6e\uff1a{location}",
        ]
        if suggestion:
            details.append(f"- \u5efa\u8bae\uff1a{suggestion}")
        if score_why:
            details.append(f"- \u539f\u56e0\uff1a{score_why}")
        if include_patch and patch:
            details.extend(["", "\u5efa\u8bae\u4fee\u6539\uff1a", "", "```diff", patch, "```"])
        if include_code_snippet:
            self._append_code_snippet(details, existing_code, "\u5f53\u524d\u4ee3\u7801")
            self._append_code_snippet(details, improved_code, "\u5efa\u8bae\u4ee3\u7801")
        details.extend(["", "</details>"])
        return chr(10).join(details)

    def _render_verification_row(self, item: dict) -> list:
        relevant_file = self._clean_text(item.get("relevant_file", ""))
        start_line = item.get("start_line")
        end_line = item.get("end_line", start_line)
        header = self._clean_text(
            item.get("issue_header") or item.get("risk_header") or "\u5f85\u786e\u8ba4\u4e8b\u9879"
        )
        content = self._clean_text(item.get("issue_content") or item.get("risk_content") or "")
        code_snippet = self._clean_text(item.get("code_snippet", ""))
        location = self._line_reference(relevant_file, start_line, end_line)
        details = [
            f"<details><summary>{header}</summary>",
            "",
            f"- \u4f4d\u7f6e\uff1a{location}",
        ]
        if content:
            details.append(f"- \u8bc1\u636e\uff1a{content}")
        self._append_code_snippet(details, code_snippet, "\u76f8\u5173\u4ee3\u7801")
        details.append(
            "- \u5efa\u8bae\uff1a\u8bf7\u7ed3\u5408\u4e1a\u52a1\u9884\u671f"
            "\u6216\u6d4b\u8bd5\u7ed3\u679c\u786e\u8ba4\u3002"
        )
        details.extend(["", "</details>"])
        return self._render_table_row("\u4eba\u5de5\u786e\u8ba4", chr(10).join(details), "-")

    def _append_code_snippet(self, details: list, code: str, title: str) -> None:
        if not code:
            return
        details.extend(["", f"{title}\uff1a", "", "```", code, "```"])

    @staticmethod
    def _render_table_row(category: str, details: str, impact: str) -> list:
        return [
            "<tr>",
            f"<td>{category}</td>",
            f"<td>{details}</td>",
            f"<td align=center>{impact}</td>",
            "</tr>",
        ]

    def _translate_label(self, label) -> str:
        clean_label = self._clean_text(label)
        key = clean_label.lower().strip()
        label_map = {
            "possible bug": "\u7591\u4f3c Bug",
            "bug": "Bug",
            "possible issue": "\u7591\u4f3c\u95ee\u9898",
            "issue": "\u95ee\u9898",
            "security": "\u5b89\u5168\u98ce\u9669",
            "performance": "\u6027\u80fd\u98ce\u9669",
            "maintainability": "\u53ef\u7ef4\u62a4\u6027",
            "readability": "\u53ef\u8bfb\u6027",
            "general": "\u4e00\u822c",
        }
        return label_map.get(key, clean_label or "\u4e00\u822c")

    def _translate_common_value(self, value) -> str:
        clean_value = self._clean_text(value)
        value_map = {
            "yes": "\u662f",
            "no": "\u5426",
            "n/a": "\u4e0d\u9002\u7528",
            "none": "\u65e0",
            "partially_correct": "\u90e8\u5206\u6b63\u786e",
            "correct": "\u6b63\u786e",
            "incorrect": "\u4e0d\u6b63\u786e",
            "unclear": "\u4e0d\u660e\u786e",
        }
        return value_map.get(clean_value.lower(), clean_value)

    def _summarize_text(self, value, max_chars: int = 180) -> str:
        text = " ".join(self._clean_text(value).split())
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip(" ,;:\uff0c\uff1b\uff1a") + "..."

    def _append_change_logic_assessment(self, parts: list, risk_review: dict) -> list:
        assessment = risk_review.get("change_logic_assessment") or {}
        if not assessment:
            return parts

        verdict = self._clean_text(assessment.get("verdict", "unclear"))
        reasoning = self._clean_text(assessment.get("reasoning", ""))
        parts.extend(["", "### Change Logic Assessment", ""])
        parts.extend([
            f"- Verdict: {verdict}",
            f"- Reasoning: {reasoning}" if reasoning else "- Reasoning: N/A",
        ])

        logic_gaps = assessment.get("logic_gaps") or []
        if logic_gaps:
            parts.extend(["", "Logic gaps:"])
            for gap in logic_gaps:
                relevant_file = self._clean_text(gap.get("relevant_file", ""))
                start_line = gap.get("start_line")
                end_line = gap.get("end_line", start_line)
                location = self._line_reference(relevant_file, start_line, end_line)
                parts.extend([
                    f"- **{self._clean_text(gap.get('issue_header', 'Logic gap'))}** - {location}",
                    f"  {self._clean_text(gap.get('issue_content', ''))}",
                ])

        return parts

    def _merge_change_logic_assessments(self, parsed_reviews: list) -> dict:
        assessments = [
            review.get("change_logic_assessment")
            for review in parsed_reviews
            if review.get("change_logic_assessment")
        ]
        if not assessments:
            return {}

        verdicts = [self._clean_text(assessment.get("verdict")) for assessment in assessments]
        reasonings = [self._clean_text(assessment.get("reasoning")) for assessment in assessments]
        return {
            "verdict": self._merge_verdicts(verdicts),
            "reasoning": self._join_unique_texts(reasonings),
            "logic_gaps": self._deduplicate_items(
                self._flatten_field(assessments, "logic_gaps"),
                header_key="issue_header",
            ),
        }

    def _merge_effort(self, parsed_reviews: list):
        efforts = []
        for review in parsed_reviews:
            try:
                efforts.append(int(str(review.get("estimated_effort_to_review_[1-5]", "")).strip()))
            except ValueError:
                continue
        if not efforts:
            return "N/A"
        return round(sum(efforts) / len(efforts))

    def _merge_text_field(self, items: list, field: str) -> str:
        return self._join_unique_texts([item.get(field) for item in items])

    def _merge_verdicts(self, verdicts: list) -> str:
        clean_verdicts = [verdict for verdict in verdicts if verdict]
        if not clean_verdicts:
            return "unclear"
        if any(verdict.lower() not in ("correct", "looks_correct", "yes", "no issues") for verdict in clean_verdicts):
            return clean_verdicts[0]
        return clean_verdicts[0]

    def _flatten_field(self, items: list, field: str) -> list:
        flattened = []
        for item in items:
            value = item.get(field) if isinstance(item, dict) else None
            if isinstance(value, list):
                flattened.extend(value)
            elif value:
                flattened.append(value)
        return flattened

    def _deduplicate_findings_by_location(self, findings: list) -> list:
        deduplicated = []
        for finding in self._deduplicate_findings(findings):
            if not isinstance(finding, dict):
                continue
            duplicate_index = self._find_duplicate_finding_index(deduplicated, finding)
            if duplicate_index is None:
                deduplicated.append(finding)
                continue
            deduplicated[duplicate_index] = self._choose_better_finding(deduplicated[duplicate_index], finding)
        return deduplicated

    def _find_duplicate_finding_index(self, findings: list, candidate: dict):
        for index, finding in enumerate(findings):
            if self._findings_have_same_location(finding, candidate):
                return index
            if self._findings_overlap_with_same_fix(finding, candidate):
                return index
        return None

    def _findings_have_same_location(self, first: dict, second: dict) -> bool:
        return self._finding_location_key(first) == self._finding_location_key(second)

    def _findings_overlap_with_same_fix(self, first: dict, second: dict) -> bool:
        first_file = self._clean_text(first.get("relevant_file")).lower()
        second_file = self._clean_text(second.get("relevant_file")).lower()
        if first_file != second_file:
            return False
        first_range = self._line_range(first, "relevant_lines_start", "relevant_lines_end")
        second_range = self._line_range(second, "relevant_lines_start", "relevant_lines_end")
        if not first_range or not second_range:
            return False
        if first_range[1] < second_range[0] or second_range[1] < first_range[0]:
            return False
        return self._finding_fix_key(first) == self._finding_fix_key(second)

    def _finding_location_key(self, finding: dict) -> tuple:
        return (
            self._clean_text(finding.get("relevant_file")).lower(),
            self._clean_text(finding.get("relevant_lines_start")),
            self._clean_text(finding.get("relevant_lines_end", finding.get("relevant_lines_start"))),
        )

    def _finding_fix_key(self, finding: dict) -> tuple:
        return (
            self._normalize_code_for_dedup(finding.get("existing_code")),
            self._normalize_code_for_dedup(finding.get("improved_code")),
        )

    @staticmethod
    def _normalize_code_for_dedup(value) -> str:
        return " ".join(str(value or "").split()).lower()

    def _line_range(self, item: dict, start_key: str, end_key: str):
        try:
            start = int(item.get(start_key))
            end = int(item.get(end_key, start))
            return min(start, end), max(start, end)
        except (TypeError, ValueError):
            return None

    def _choose_better_finding(self, first: dict, second: dict) -> dict:
        first_score = self._finding_score(first)
        second_score = self._finding_score(second)
        if second_score > first_score:
            return second
        if first_score > second_score:
            return first
        first_span = self._finding_span_size(first)
        second_span = self._finding_span_size(second)
        return second if second_span < first_span else first

    @staticmethod
    def _finding_score(finding: dict) -> int:
        try:
            return int(finding.get("score", 0))
        except (TypeError, ValueError):
            return 0

    def _finding_span_size(self, finding: dict) -> int:
        line_range = self._line_range(finding, "relevant_lines_start", "relevant_lines_end")
        if not line_range:
            return 10**9
        return line_range[1] - line_range[0]

    def _deduplicate_items(self, items: list, header_key: str) -> list:
        deduplicated = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            key = (
                self._clean_text(item.get("relevant_file")).lower(),
                self._clean_text(item.get(header_key)).lower(),
                self._clean_text(item.get("start_line")),
                self._clean_text(item.get("end_line")),
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(item)
        return deduplicated

    def _deduplicate_texts(self, items: list) -> list:
        deduplicated = []
        seen = set()
        for item in items:
            clean_item = self._clean_text(item)
            key = clean_item.lower()
            if not clean_item or key in seen:
                continue
            seen.add(key)
            deduplicated.append(clean_item)
        return deduplicated

    def _deduplicate_findings(self, findings: list) -> list:
        deduplicated = []
        seen = set()
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            key = (
                self._clean_text(finding.get("relevant_file")).lower(),
                self._clean_text(finding.get("relevant_lines_start")),
                self._clean_text(finding.get("relevant_lines_end")),
                self._clean_text(finding.get("one_sentence_summary")).lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(finding)
        return deduplicated

    def _join_unique_texts(self, items: list) -> str:
        return " ".join(self._deduplicate_texts(items))

    def _render_implementation_finding(self, finding: dict) -> list:
        relevant_file = self._clean_text(finding.get("relevant_file", ""))
        start_line = finding.get("relevant_lines_start")
        end_line = finding.get("relevant_lines_end", start_line)
        location = self._line_reference(relevant_file, start_line, end_line)
        existing_code = self._clean_text(finding.get("existing_code", ""))
        improved_code = self._clean_text(finding.get("improved_code", ""))
        patch = self._diff(existing_code, improved_code)
        summary = self._clean_text(finding.get("one_sentence_summary", "Implementation finding"))
        label = self._clean_text(finding.get("label", "general")).capitalize()
        score = finding.get("score", "N/A")
        score_why = self._clean_text(finding.get("score_why", ""))
        lines = [
            "<tr>",
            f"<td>{label}</td>",
            "<td>",
            f"<details><summary>{summary}</summary>",
            "",
            f"**{self._clean_text(finding.get('suggestion_content', ''))}**",
            "",
            f"{location}",
        ]
        if patch:
            lines.extend(["", "```diff", patch, "```"])
        if score_why:
            lines.extend(["", f"Why: {score_why}"])
        lines.extend([
            "",
            "</details>",
            "</td>",
            f"<td align=center>{score}</td>",
            "</tr>",
        ])
        return lines

    def _line_reference(self, relevant_file, start_line, end_line):
        raw_location = f"{relevant_file}:{start_line}-{end_line}"
        link = self._line_link(relevant_file, start_line, end_line)
        return f"[{raw_location}]({link})" if link else raw_location

    def _line_link(self, relevant_file, start_line, end_line):
        try:
            if relevant_file and start_line is not None:
                return self.git_provider.get_line_link(relevant_file, int(start_line), int(end_line))
        except Exception:
            return ""
        return ""

    @staticmethod
    def _clean_text(value) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _diff(existing_code: str, improved_code: str) -> str:
        if not existing_code and not improved_code:
            return ""
        diff = difflib.unified_diff(
            existing_code.splitlines(),
            improved_code.splitlines(),
            fromfile="existing",
            tofile="improved",
            lineterm="",
        )
        return "\n".join(diff)
