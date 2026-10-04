"""结果解析（含确定性裁决）与事件过滤测试。"""

import json

from core.filter import EventFilter
from core.result_parser import ResultParser
from models.review import IssueSeverity, Verdict
from tests.conftest import make_event


def _payload(**overrides) -> str:
    data = {
        "summary": "整体良好",
        "issues": [],
        "verdict": "APPROVE",
        "prompt_injection_suspected": False,
        "guideline_refs": ["CONTRIBUTING.md"],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


class TestResultParser:
    def test_plain_json(self, event):
        result = ResultParser().parse(event, _payload(), "normal")
        assert result.verdict == Verdict.APPROVE
        assert result.summary == "整体良好"
        assert result.guideline_refs == ["CONTRIBUTING.md"]
        assert not result.degraded

    def test_fenced_json(self, event):
        raw = f"好的，结果如下：\n```json\n{_payload()}\n```"
        result = ResultParser().parse(event, raw, "normal")
        assert result.verdict == Verdict.APPROVE

    def test_prose_with_brace_block(self, event):
        raw = f"前言……{_payload()}后记"
        result = ResultParser().parse(event, raw, "normal")
        assert result.verdict == Verdict.APPROVE

    def test_garbage_degrades(self, event):
        raw = "模型跑飞了，没有 JSON"
        result = ResultParser().parse(event, raw, "normal")
        assert result.degraded
        assert result.verdict == Verdict.COMMENT
        assert result.summary == raw

    def test_injection_flag_passthrough(self, event):
        result = ResultParser().parse(
            event, _payload(prompt_injection_suspected=True), "normal"
        )
        assert result.prompt_injection_suspected

    def test_rc_without_issues_downgraded(self, event):
        result = ResultParser().parse(event, _payload(verdict="REQUEST_CHANGES"), "strict")
        assert result.verdict == Verdict.COMMENT

    def test_blocker_always_rc(self, event):
        issues = [{"severity": "blocker", "title": "t", "detail": "d"}]
        for level in ("loose", "normal", "strict"):
            result = ResultParser().parse(event, _payload(issues=issues), level)
            assert result.verdict == Verdict.REQUEST_CHANGES, level

    def test_major_by_level(self, event):
        issues = [{"severity": "major", "title": "t", "detail": "d"}]
        parser = ResultParser()
        assert parser.parse(event, _payload(issues=issues), "loose").verdict == Verdict.COMMENT
        assert (
            parser.parse(event, _payload(issues=issues), "normal").verdict
            == Verdict.REQUEST_CHANGES
        )
        assert (
            parser.parse(event, _payload(issues=issues), "strict").verdict
            == Verdict.REQUEST_CHANGES
        )

    def test_minor_by_level(self, event):
        issues = [{"severity": "minor", "title": "t", "detail": "d"}]
        parser = ResultParser()
        assert parser.parse(event, _payload(issues=issues), "normal").verdict == Verdict.COMMENT
        assert parser.parse(event, _payload(issues=issues), "strict").verdict == Verdict.COMMENT
        assert parser.parse(event, _payload(issues=issues), "loose").verdict == Verdict.COMMENT

    def test_llm_approve_with_issues_overridden(self, event):
        """注入 LLM 输出自批 APPROVE 时，有问题清单仍不得通过。"""

        issues = [{"severity": "nit", "title": "t", "detail": "d"}]
        result = ResultParser().parse(event, _payload(issues=issues), "loose")
        assert result.verdict == Verdict.COMMENT

    def test_bad_severity_falls_back_to_minor(self, event):
        issues = [{"severity": "catastrophic", "title": "t", "detail": "d"}]
        result = ResultParser().parse(event, _payload(issues=issues), "normal")
        assert result.issues[0].severity == IssueSeverity.MINOR

    def test_bad_line_discarded(self, event):
        issues = [
            {"severity": "minor", "title": "t", "detail": "d", "file": "a.py", "line": -3}
        ]
        result = ResultParser().parse(event, _payload(issues=issues), "normal")
        assert result.issues[0].line is None
        assert result.issues[0].file == "a.py"

    def test_empty_issue_items_skipped(self, event):
        issues = [{}, {"title": " ", "detail": ""}, "not-a-dict"]
        result = ResultParser().parse(event, _payload(issues=issues), "normal")
        assert result.issues == []


class TestEventFilter:
    def test_repo_out_of_scope(self, config):
        decision = EventFilter(config).check(make_event(repo="other/repo"))
        assert not decision.allowed

    def test_bot_author_blocked(self, config):
        decision = EventFilter(config).check(make_event(author="dependabot[bot]"))
        assert not decision.allowed
        decision = EventFilter(config).check(make_event(author="random[bot]"))
        assert not decision.allowed

    def test_whitelist_skip(self, config_dict):
        from models import PluginConfig

        config_dict["whitelist"]["users"] = ["alice"]
        config_dict["whitelist"]["whitelist_action"] = "skip"
        config = PluginConfig.from_dict(config_dict)
        decision = EventFilter(config).check(make_event(author="Alice"))
        assert not decision.allowed

    def test_whitelist_downgrade(self, config_dict):
        from models import PluginConfig

        config_dict["whitelist"]["users"] = ["alice"]
        config_dict["whitelist"]["whitelist_action"] = "downgrade"
        config = PluginConfig.from_dict(config_dict)
        decision = EventFilter(config).check(make_event(author="alice"))
        assert decision.allowed and decision.downgrade_only

    def test_normal_author_allowed(self, config):
        decision = EventFilter(config).check(make_event(author="bob"))
        assert decision.allowed and not decision.downgrade_only

    def test_is_whitelisted(self, config_dict):
        from models import PluginConfig

        config_dict["whitelist"]["users"] = ["Alice"]
        config = PluginConfig.from_dict(config_dict)
        f = EventFilter(config)
        assert f.is_whitelisted("ALICE")
        assert f.is_whitelisted("dependabot[bot]")
        assert f.is_whitelisted("ci[bot]")
        assert not f.is_whitelisted("mallory")
