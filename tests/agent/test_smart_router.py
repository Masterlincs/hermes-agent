"""Unit tests for agent/smart_router.py."""

import pytest
from agent.smart_router import (
    classify_prompt,
    _check_override,
    _regex_classify,
    _build_result,
    DEFAULT_TIER_CONFIG,
)


class TestCheckOverride:
    def test_fast_override(self):
        assert _check_override("!fast hello") == "fast"

    def test_code_override(self):
        assert _check_override("!code write a function") == "code"

    def test_no_override(self):
        assert _check_override("hello world") is None

    def test_invalid_tier(self):
        assert _check_override("!invalid something") is None


class TestRegexClassify:
    def test_fast_greeting(self):
        assert _regex_classify("hi there", None) == "fast"
        assert _regex_classify("hello world", None) == "fast"

    def test_code_pattern(self):
        assert _regex_classify("fix bug in login", None) == "code"
        assert _regex_classify("write function to sort", None) == "code"

    def test_research_pattern(self):
        assert _regex_classify("search for papers on AI", None) == "research"

    def test_planning_pattern(self):
        assert _regex_classify("design a new system", None) == "planning"

    def test_no_match(self):
        assert _regex_classify("just chatting", None) is None

    def test_custom_patterns(self):
        patterns = {"custom": [r"^custom\b"]}
        assert _regex_classify("custom task", patterns) == "custom"


class TestBuildResult:
    def test_default_tier(self):
        result = _build_result("fast", 0.85, "regex", {})
        assert result["tier"] == "fast"
        assert result["confidence"] == 0.85
        assert result["method"] == "regex"
        assert result["max_iterations"] == 8
        assert result["toolsets"] == []

    def test_smart_tier_null_toolsets(self):
        result = _build_result("smart", 0.0, "fallback", {})
        assert result["toolsets"] is None
        assert result["max_iterations"] == 90

    def test_custom_tier_config(self):
        sr_cfg = {
            "tiers": {
                "fast": {
                    "model": "gpt-4o-mini",
                    "provider": "openai",
                    "max_iterations": 5,
                    "toolsets": ["file"],
                }
            }
        }
        result = _build_result("fast", 1.0, "override", sr_cfg)
        assert result["model"] == "gpt-4o-mini"
        assert result["provider"] == "openai"
        assert result["max_iterations"] == 5
        assert result["toolsets"] == ["file"]


class TestClassifyPrompt:
    def test_disabled_returns_fallback(self):
        config = {"smart_router": {"enabled": False}}
        result = classify_prompt("hello", config)
        assert result["tier"] == "smart"
        assert result["method"] == "fallback"

    def test_override_prefix(self):
        config = {"smart_router": {"enabled": True}}
        result = classify_prompt("!fast hello", config)
        assert result["tier"] == "fast"
        assert result["method"] == "override"
        assert result["confidence"] == 1.0

    def test_regex_classification(self):
        config = {"smart_router": {"enabled": True, "regex": {"enabled": True}}}
        result = classify_prompt("debug this code", config)
        assert result["tier"] == "code"
        assert result["method"] == "regex"

    def test_regex_disabled_falls_back(self):
        config = {
            "smart_router": {
                "enabled": True,
                "regex": {"enabled": False},
                "classifier": {"enabled": False},
                "fallback_tier": "smart",
            }
        }
        result = classify_prompt("random prompt", config)
        assert result["tier"] == "smart"
        assert result["method"] == "fallback"

    def test_empty_model_provider_means_default(self):
        config = {"smart_router": {"enabled": True}}
        result = classify_prompt("!code test", config)
        assert result["model"] == ""
        assert result["provider"] == ""
