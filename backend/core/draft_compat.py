"""Local-only draft-model compatibility checks (B3).

llama.cpp requires a speculative draft model to share the target model's
vocabulary. A mismatch can crash the process instead of producing a readable
error, so the launcher checks locally *before* launch.

The check is deliberately offline and bounded:
- reads only GGUF metadata, never tensors or model weights;
- hashes the total vocabulary size plus the first 256 token strings;
- caches by (path, mtime, size, options);
- a malformed/unknown signature is reported as ``unknown``, never as ``match``.

Equal prefixes are evidence, not proof: two different tokenizers could share
the first 256 tokens. The UI wording therefore says "prefix matches" and the
check only blocks on a definite mismatch.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from core.gguf_probe import VOCAB_PREFIX_TOKENS, read_vocab_prefix_cached

STATUS_MATCH = "match"
STATUS_MISMATCH = "mismatch"
STATUS_UNKNOWN = "unknown"
_BUILD_RE = re.compile(r"(?:build\s*|b)(\d{2,})", re.IGNORECASE)


def _build_number(build: int | str | None) -> int | None:
    if isinstance(build, bool):
        return None
    if isinstance(build, int):
        return build
    if not build:
        return None
    match = _BUILD_RE.search(str(build))
    if match:
        return int(match.group(1))
    try:
        return int(str(build).strip())
    except ValueError:
        return None


def _signature(path: str | Path) -> dict[str, Any]:
    return read_vocab_prefix_cached(path, max_tokens=VOCAB_PREFIX_TOKENS)


def _public(signature: dict[str, Any]) -> dict[str, Any]:
    """Return only comparison facts; never include token text."""
    return {
        "tokenizer_model": signature.get("tokenizer_model"),
        "token_count": signature.get("token_count"),
        "prefix_count": signature.get("prefix_count"),
        "prefix_hash": signature.get("prefix_hash"),
        "complete_prefix": bool(signature.get("complete_prefix")),
        "error": signature.get("error"),
    }


def compare_draft_vocab(
    main_model: str | Path,
    draft_model: str | Path,
) -> dict[str, Any]:
    """Compare main/draft vocabulary prefixes without network access.

    ``mismatch`` is safe to block on (different tokenizer type, vocabulary size,
    or first-N token strings). ``unknown`` means one file could not be read;
    callers should show an informational check and let the launch proceed.
    """
    main = _signature(main_model)
    draft = _signature(draft_model)
    result: dict[str, Any] = {
        "status": STATUS_UNKNOWN,
        "reason": "",
        "main": _public(main),
        "draft": _public(draft),
    }
    if not os.path.isfile(str(main_model)):
        result["reason"] = f"main model does not exist: {main_model}"
        return result
    if not os.path.isfile(str(draft_model)):
        result["reason"] = f"draft model does not exist: {draft_model}"
        return result
    if main.get("error"):
        result["reason"] = f"main model vocabulary is unreadable: {main['error']}"
        return result
    if draft.get("error"):
        result["reason"] = f"draft model vocabulary is unreadable: {draft['error']}"
        return result
    main_type = main.get("tokenizer_model")
    draft_type = draft.get("tokenizer_model")
    if main_type and draft_type and main_type != draft_type:
        result["status"] = STATUS_MISMATCH
        result["reason"] = f"tokenizer types differ ({main_type} vs {draft_type})"
        return result
    if main.get("token_count") != draft.get("token_count"):
        result["status"] = STATUS_MISMATCH
        result["reason"] = (
            f"vocabulary sizes differ ({main.get('token_count')} vs "
            f"{draft.get('token_count')})"
        )
        return result
    if main.get("prefix_hash") != draft.get("prefix_hash"):
        result["status"] = STATUS_MISMATCH
        result["reason"] = (
            f"the first {min(main.get('prefix_count') or 0, draft.get('prefix_count') or 0)} "
            "token strings differ"
        )
        return result
    result["status"] = STATUS_MATCH
    result["reason"] = (
        f"vocabulary size and first {main.get('prefix_count')} token strings match"
    )
    return result


def manifest_runtime_checks(
    manifest: dict[str, Any],
    build: int | str | None,
) -> list[dict[str, str]]:
    """Return preflight checks for manifest-declared runtime requirements.

    Only the minimum llama.cpp build is enforced here. Backend-family
    requirements are displayed as manifest metadata; mapping them to a runtime
    profile needs the selected ``runtime_id`` and is not a file-level fact.
    """
    checks: list[dict[str, str]] = []
    requires = manifest.get("requires") if isinstance(manifest, dict) else None
    if not isinstance(requires, dict):
        return checks
    minimum = requires.get("min_llama_cpp_build")
    if not isinstance(minimum, int):
        return checks
    build_number = _build_number(build)
    if build_number is not None and build_number < minimum:
        checks.append(
            {
                "code": "runtime_too_old",
                "severity": "error",
                "detail": (
                    f"this model manifest requires llama.cpp build {minimum} or newer; "
                    f"the selected runtime is build {build_number}"
                ),
            }
        )
    return checks
