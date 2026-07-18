"""Role-based multi-provider LLM router with per-role fallback chains.

Each *role* (triage, drafting, content) maps to an ordered list of
(provider, model) candidates in config/models.yaml. `complete()` tries them in
order and returns the first success, so one provider being down — a missing key,
a timeout, a 5xx — never breaks a run. It only raises if every backup fails.

Providers are called over their REST APIs via httpx — no extra SDK deps.
OpenAI / DeepSeek / Grok share the OpenAI chat-completions shape; Anthropic and
Gemini have their own. Add a provider by adding a `kind` + a `_call_<kind>`.

Usage:
    from llm_router import complete
    result = complete("triage", system="...", user="...", max_tokens=1500)
    text = result.text          # the model's reply
    result.model                # which model actually answered
    result.attempts             # [(provider, model, error), ...] that were skipped
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Reuse the same env loading the rest of the app relies on.
try:
    from dotenv import load_dotenv
    _env_root = _HERE.parent
    _explicit_env = os.environ.get("MARKETING_OPS_ENV_FILE")
    for _candidate in (
        Path(_explicit_env) if _explicit_env else None,
        _env_root / ".env",
        _env_root.parent / ".ENV",
        _env_root.parent / ".env",
    ):
        if _candidate and _candidate.exists():
            load_dotenv(_candidate, override=False)
except ImportError:
    pass

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is a hard dep elsewhere
    yaml = None  # type: ignore[assignment]

CONFIG_PATH = _HERE.parent / "config" / "models.yaml"


class LLMRouterError(RuntimeError):
    """Raised only when every candidate in a role's chain has failed."""


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    attempts: list[tuple[str, str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def resolve_chain(cfg: dict[str, Any], role: str) -> list[dict[str, str]]:
    """Return the ordered [{provider, model}, ...] candidates for a role."""
    roles = cfg.get("roles") or {}
    chain = roles.get(role)
    if not chain:
        raise LLMRouterError(
            f"No model chain configured for role '{role}'. "
            f"Add it under roles: in {CONFIG_PATH.name}."
        )
    return [c for c in chain if c.get("provider") and c.get("model")]


# ---------------------------------------------------------------------------
# Provider calls — each returns assistant text or raises
# ---------------------------------------------------------------------------

def _call_anthropic(base_url: str, key: str, model: str, system: str, user: str,
                    max_tokens: int, timeout: float) -> str:
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
        resp = client.post(base_url, headers=headers, json=body)
    resp.raise_for_status()
    data = resp.json()
    return "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    ).strip()


def _call_openai(base_url: str, key: str, model: str, system: str, user: str,
                 max_tokens: int, timeout: float) -> str:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
        resp = client.post(base_url, headers=headers, json=body)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"No choices in response: {str(data)[:200]}")
    return (choices[0].get("message", {}).get("content") or "").strip()


def _call_gemini(base_url: str, key: str, model: str, system: str, user: str,
                 max_tokens: int, timeout: float) -> str:
    url = f"{base_url}/{model}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
        resp = client.post(url, params={"key": key}, json=body)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"No candidates in response: {str(data)[:200]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


_PROVIDER_CALLS: dict[str, Callable[..., str]] = {
    "anthropic": _call_anthropic,
    "openai": _call_openai,
    "gemini": _call_gemini,
}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def complete(
    role: str,
    *,
    system: str,
    user: str,
    max_tokens: Optional[int] = None,
    cfg: Optional[dict[str, Any]] = None,
) -> LLMResult:
    """Try each candidate for `role` in order; return the first that succeeds.

    Falls through on a missing key or any provider error. Raises LLMRouterError
    only if every candidate fails — with the per-candidate reasons attached.
    """
    cfg = cfg if cfg is not None else load_config()
    providers = cfg.get("providers") or {}
    defaults = cfg.get("defaults") or {}
    tokens = int(max_tokens or defaults.get("max_tokens", 2000))
    timeout = float(defaults.get("timeout_seconds", 60))

    chain = resolve_chain(cfg, role)
    attempts: list[tuple[str, str, str]] = []

    for cand in chain:
        provider = cand["provider"]
        model = cand["model"]
        pcfg = providers.get(provider)
        if not pcfg:
            attempts.append((provider, model, "provider not defined in config"))
            continue
        kind = pcfg.get("kind", provider)
        call = _PROVIDER_CALLS.get(kind)
        if call is None:
            attempts.append((provider, model, f"no handler for kind '{kind}'"))
            continue
        key = os.environ.get(pcfg.get("key_env", ""))
        if not key:
            attempts.append((provider, model, f"missing {pcfg.get('key_env')}"))
            continue
        try:
            text = call(pcfg["base_url"], key, model, system, user, tokens, timeout)
        except Exception as exc:  # noqa: BLE001 - any failure should fall through
            attempts.append((provider, model, f"{type(exc).__name__}: {exc}"))
            continue
        if not text:
            attempts.append((provider, model, "empty response"))
            continue
        return LLMResult(text=text, provider=provider, model=model, attempts=attempts)

    reasons = "; ".join(f"{p}/{m}: {why}" for p, m, why in attempts) or "no candidates"
    raise LLMRouterError(f"All candidates failed for role '{role}' — {reasons}")


def complete_json(role: str, **kwargs: Any) -> tuple[Any, LLMResult]:
    """complete() + tolerant JSON parse (strips ```json fences). Returns (data, result)."""
    import re
    result = complete(role, **kwargs)
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.text.strip())
    return json.loads(raw), result


# ---------------------------------------------------------------------------
# CLI — inspect the configured routing and key availability
# ---------------------------------------------------------------------------

def _cli() -> int:
    import argparse
    p = argparse.ArgumentParser(description="LLM router inspector")
    p.add_argument("--role", help="Show the chain for one role (default: all)")
    args = p.parse_args()
    cfg = load_config()
    providers = cfg.get("providers") or {}
    roles = cfg.get("roles") or {}
    targets = [args.role] if args.role else list(roles)
    for role in targets:
        print(f"\n{role}:")
        for cand in roles.get(role, []):
            pcfg = providers.get(cand.get("provider"), {})
            key_env = pcfg.get("key_env", "")
            have = "ready " if os.environ.get(key_env) else "NO KEY"
            print(f"  [{have}] {cand.get('provider')} / {cand.get('model')}  ({key_env})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
