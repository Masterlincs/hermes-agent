"""Native smart-router module for Hermes Agent.

Classifies user prompts into tiers using regex + optional LLM.
Resolves tier configs into model/provider/max_iterations/toolsets.
No external HTTP calls — runs inline.
"""

import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Per-session router cache so classification only runs once per session.
_ROUTER_CACHE: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_MAX_CACHED_SESSIONS = 256


def clear_router_cache() -> None:
    """Clear the router session cache.  Useful in tests."""
    _ROUTER_CACHE.clear()


def _get_cached_result(session_key: str) -> Optional[Dict[str, Any]]:
    if session_key and session_key in _ROUTER_CACHE:
        _ROUTER_CACHE.move_to_end(session_key)
        return _ROUTER_CACHE[session_key]
    return None


def _set_cached_result(session_key: str, result: Dict[str, Any]) -> None:
    if not session_key:
        return
    _ROUTER_CACHE[session_key] = result
    while len(_ROUTER_CACHE) > _MAX_CACHED_SESSIONS:
        _ROUTER_CACHE.popitem(last=False)

DEFAULT_TIER_CONFIG = {
    "fast": {
        "model": "",
        "provider": "",
        "max_iterations": 8,
        "toolsets": [],
        "enabled_tools": None,   # null = all tools in the toolset
        "disabled_tools": [],
        "enabled_skills": None,  # null = all available skills
        "disabled_skills": [],
    },
    "code": {
        "model": "",
        "provider": "",
        "max_iterations": 40,
        "toolsets": ["terminal", "file", "code_execution"],
        "enabled_tools": None,
        "disabled_tools": [],
        "enabled_skills": None,
        "disabled_skills": [],
    },
    "research": {
        "model": "",
        "provider": "",
        "max_iterations": 60,
        "toolsets": ["web", "browser", "delegation"],
        "enabled_tools": None,
        "disabled_tools": [],
        "enabled_skills": None,
        "disabled_skills": [],
    },
    "planning": {
        "model": "",
        "provider": "",
        "max_iterations": 25,
        "toolsets": [],
        "enabled_tools": None,
        "disabled_tools": [],
        "enabled_skills": None,
        "disabled_skills": [],
    },
    "smart": {
        "model": "",
        "provider": "",
        "max_iterations": 90,
        "toolsets": None,  # null = all available tools
        "enabled_tools": None,
        "disabled_tools": [],
        "enabled_skills": None,
        "disabled_skills": [],
    },
}

DEFAULT_REGEX_PATTERNS = {
    "fast": [
        r"^!(fast)\b",
        r"^(hi\b|hello|^hey\s)",
    ],
    "code": [
        r"(fix.?bug|write.?function|debug|code.?review)",
    ],
    "research": [
        r"(search|find.?paper|scholarship|current events|look up)",
    ],
    "planning": [
        r"(plan|design|roadmap|architecture|system.?design|break.?down)",
    ],
}

DEFAULT_CLASSIFIER_PROMPT = (
    "Classify this user request into exactly one tier: fast, code, research, planning, or smart.\n"
    "Respond with ONLY the tier name.\n"
    "Request: {prompt}\n"
    "Tier:"
)


def classify_prompt(
    prompt: str,
    config: Optional[Dict[str, Any]] = None,
    session_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify a user prompt into a tier and return resolved config.

    If *session_key* is provided, the result is cached for the lifetime of the
    session so classification only happens on the first message.

    Returns:
        {
            "tier": str,
            "confidence": float,
            "method": "regex" | "llm" | "fallback" | "override",
            "model": str,
            "provider": str,
            "max_iterations": int,
            "toolsets": list[str] | None,
        }
    """
    cached = _get_cached_result(session_key)
    if cached is not None:
        return cached

    if config is None:
        config = _load_config()

    sr_cfg = config.get("smart_router", {})
    if not sr_cfg.get("enabled", False):
        result = _build_result("smart", 0.0, "fallback", sr_cfg)
        _set_cached_result(session_key, result)
        return result

    # 1. Check for !tier override prefix
    override_tier = _check_override(prompt)
    if override_tier:
        result = _build_result(override_tier, 1.0, "override", sr_cfg)
        _maybe_log(result, prompt, config)
        _set_cached_result(session_key, result)
        return result

    # 2. Regex classifier
    regex_cfg = sr_cfg.get("regex", {})
    if regex_cfg.get("enabled", True):
        regex_tier = _regex_classify(prompt, regex_cfg.get("patterns"))
        if regex_tier:
            result = _build_result(regex_tier, 0.85, "regex", sr_cfg)
            _maybe_log(result, prompt, config)
            _set_cached_result(session_key, result)
            return result

    # 3. LLM classifier
    classifier_cfg = sr_cfg.get("classifier", {})
    if classifier_cfg.get("enabled", False):
        llm_tier = _llm_classify(prompt, classifier_cfg, config)
        if llm_tier and llm_tier in DEFAULT_TIER_CONFIG:
            result = _build_result(llm_tier, 0.75, "llm", sr_cfg)
            _maybe_log(result, prompt, config)
            _set_cached_result(session_key, result)
            return result

    # 4. Fallback
    fallback_tier = sr_cfg.get("fallback_tier", "smart")
    result = _build_result(fallback_tier, 0.0, "fallback", sr_cfg)
    _maybe_log(result, prompt, config)
    _set_cached_result(session_key, result)
    return result


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _check_override(prompt: str) -> Optional[str]:
    match = re.match(r"^!(\w+)\b", prompt.strip())
    if match:
        tier = match.group(1).lower()
        if tier in DEFAULT_TIER_CONFIG:
            return tier
    return None


def _regex_classify(prompt: str, patterns: Optional[Dict[str, List[str]]]) -> Optional[str]:
    if not patterns:
        patterns = DEFAULT_REGEX_PATTERNS
    for tier, pattern_list in patterns.items():
        for pattern in pattern_list:
            try:
                if re.search(pattern, prompt, re.IGNORECASE):
                    return tier
            except re.error:
                logger.warning("Invalid regex pattern in smart_router: %r", pattern)
                continue
    return None


def _llm_classify(prompt: str, classifier_cfg: Dict[str, Any], config: Dict[str, Any]) -> Optional[str]:
    chain = classifier_cfg.get("chain", [])
    if not chain:
        chain = [{"model": "", "provider": "", "timeout": 10}]

    template = classifier_cfg.get("prompt_template", DEFAULT_CLASSIFIER_PROMPT)
    messages = [
        {"role": "system", "content": "You are a request classifier."},
        {"role": "user", "content": template.format(prompt=prompt)},
    ]

    for entry in chain:
        model = entry.get("model", "")
        provider = entry.get("provider", "")
        timeout = entry.get("timeout", 10)
        try:
            from agent.auxiliary_client import call_llm
            response = call_llm(
                messages=messages,
                model=model or None,
                provider=provider or None,
                max_tokens=20,
                temperature=0.0,
                timeout=timeout,
            )
            content = (response.choices[0].message.content or "").strip().lower()
            # Extract just the tier word
            for tier in DEFAULT_TIER_CONFIG:
                if tier in content:
                    return tier
        except Exception as e:
            logger.debug("LLM classifier attempt failed (%s/%s): %s", provider, model, e)
            continue

    return None


def _build_result(tier: str, confidence: float, method: str, sr_cfg: Dict[str, Any]) -> Dict[str, Any]:
    tiers = sr_cfg.get("tiers", {})
    tier_cfg = tiers.get(tier, DEFAULT_TIER_CONFIG.get(tier, {}))
    default_tier = DEFAULT_TIER_CONFIG.get(tier, {})

    return {
        "tier": tier,
        "confidence": confidence,
        "method": method,
        "model": tier_cfg.get("model", ""),
        "provider": tier_cfg.get("provider", ""),
        "max_iterations": tier_cfg.get("max_iterations", default_tier.get("max_iterations", 90)),
        "toolsets": tier_cfg.get("toolsets", default_tier.get("toolsets", [])),
        "enabled_tools": tier_cfg.get("enabled_tools", default_tier.get("enabled_tools")),
        "disabled_tools": tier_cfg.get("disabled_tools", default_tier.get("disabled_tools", [])),
        "enabled_skills": tier_cfg.get("enabled_skills", default_tier.get("enabled_skills")),
        "disabled_skills": tier_cfg.get("disabled_skills", default_tier.get("disabled_skills", [])),
    }


def _maybe_log(result: Dict[str, Any], prompt: str, config: Dict[str, Any]) -> None:
    sr_cfg = config.get("smart_router", {})
    if not sr_cfg.get("log_decisions", True):
        return

    try:
        home = os.path.expanduser("~/.hermes")
        log_dir = Path(home) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "smart_router.jsonl"

        entry = {
            "ts": time.time(),
            "prompt_preview": prompt[:200],
            "tier": result["tier"],
            "confidence": result["confidence"],
            "method": result["method"],
            "model": result["model"],
            "provider": result["provider"],
            "max_iterations": result["max_iterations"],
            "toolsets": result["toolsets"],
            "enabled_tools": result.get("enabled_tools"),
            "disabled_tools": result.get("disabled_tools", []),
            "enabled_skills": result.get("enabled_skills"),
            "disabled_skills": result.get("disabled_skills", []),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug("Smart router decision logging failed: %s", e)
