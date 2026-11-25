"""Skills: a named bundle of instructions plus a tool subset.

A skill answers one question the launcher could not answer before: *"for this kind
of work, which tools should be available, and what standing instructions apply?"*
Without it every conversation gets the full tool list and an empty system prompt, so
the model has to be told by hand every time.

### Why `@skill` detection lives here, as a pure function

The `@name` convention is a **parsing contract**, and the interesting part is what
does *not* count as a mention. Getting that wrong in the UI and again in the backend
would give two different answers to "was this skill requested?" — so the rule is one
function, tested directly, with the exact negatives spelled out:

- only the **latest user message** counts (a mention in history is not a request),
- **no prefix matching**: `@skill-creator` must not match a skill named `skill`,
- a mention inside a code fence, an inline code span, or a blockquote is text about
  a skill, not a request to use it,
- a mention glued into a URL, an email address or a path is not a mention,
- matching is case-insensitive on the name, but the name must be whitespace-delimited.

This follows the reference implementation's stated intent contract (Unsloth Studio's
`SKILL_MENTIONS.md`), written from scratch rather than copied: the point is to avoid
a model burning a turn loading a skill because the user *quoted* its name.

The loaded text is instructions **plus the tool filter**; a denied skill is never
read, and a failed read must tell the model the skill was not loaded rather than
pretending it was.
"""
from __future__ import annotations

import json
import re
from typing import Any

# A skill's instruction text is injected into every request that uses it, so it is
# bounded like memory: this is context, not a document store.
MAX_SKILL_NAME = 64
MAX_SKILL_DESCRIPTION = 500
MAX_SKILL_INSTRUCTIONS = 8000
MAX_SKILLS = 100

_SKILL_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_.-]{0,63}\Z")
# Case-insensitive by design: a user typing `@Reviewer` means the skill, and the
# stored (canonical, lowercase) name is what gets returned. The lookarounds keep
# handles, paths and emails out — see the negatives in `test_skills.py`.
_MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9_@/\\:.-])@([A-Za-z0-9][A-Za-z0-9_.-]{0,63})(?![A-Za-z0-9_.-])"
)


def normalize_skill_name(value: Any) -> str:
    """Validate a skill name.

    Lowercase, no spaces: the name is also the `@mention` token, and allowing
    mixed case would make the mention contract ambiguous on a case-insensitive
    comparison. Refused rather than silently lowercased — a user who typed
    `MySkill` should be told the name is invalid, not discover later that only
    `myskill` triggers it.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("skill name is required")
    if not _SKILL_NAME_RE.match(text):
        raise ValueError(
            "skill name must be lowercase letters, digits, '.', '_' or '-', "
            "starting with a letter or digit (at most 64 characters)"
        )
    return text


def _strip_code_and_quotes(text: str) -> str:
    """Remove the regions where `@name` is prose about a skill, not a request.

    Removes, in order: fenced code blocks, inline code spans, and blockquote lines.
    The order matters — stripping inline spans first would eat the backtick that
    opens a fence.
    """
    # Fenced blocks: ```...``` (with any info string) and ~~~...~~~
    without_fences = re.sub(r"(?ms)^[ \t]*(?:```|~~~).*?(?:```|~~~)[ \t]*$", " ", text)
    # Any unterminated fence swallows the rest: the user is mid-code, so nothing
    # after it is a request.
    without_fences = re.sub(r"(?ms)^[ \t]*(?:```|~~~).*$", " ", without_fences)
    # Inline code spans (single or multiple backticks).
    without_inline = re.sub(r"(`+)(?:(?!\1).)*\1", " ", without_fences, flags=re.S)
    # Blockquote lines.
    without_quotes = re.sub(r"(?m)^[ \t]*>.*$", " ", without_inline)
    return without_quotes


def mentioned_skills(message: str | None, known_names: list[str]) -> list[str]:
    """Skill names mentioned in **one user message**, in first-appearance order.

    `known_names` is required: an unknown `@foo` is an ordinary word (a handle, a
    package name, a path segment), not a request to load something that does not
    exist. Returning unknown names would make every `@username` look like a skill.
    """
    if not message or not known_names:
        return []
    known = {name.lower(): name for name in known_names}
    cleaned = _strip_code_and_quotes(message)
    found: list[str] = []
    for match in _MENTION_RE.finditer(cleaned):
        canonical = known.get(match.group(1).lower())
        if canonical and canonical not in found:
            found.append(canonical)
    return found


def resolve_active_skill(
    explicit_skill: Any,
    messages: list[dict[str, Any]] | None,
    skills: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Which skill applies to this turn: the explicit one, else a mention.

    The explicit choice wins. A conversation that pinned a skill should not switch
    because the user typed the name of another one in passing — the pinned choice is
    the clearer statement of intent.
    """
    by_name = {str(skill.get("name") or ""): skill for skill in skills}
    if isinstance(explicit_skill, str) and explicit_skill.strip():
        return by_name.get(explicit_skill.strip())
    latest_user = ""
    for message in reversed(messages or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                latest_user = content
            break
    if not latest_user:
        return None
    names = mentioned_skills(latest_user, list(by_name))
    return by_name[names[0]] if names else None


def skill_context(skill: dict[str, Any]) -> str:
    """Render a skill's instructions as the block injected into the system prompt."""
    instructions = str(skill.get("instructions") or "").strip()
    if not instructions:
        return ""
    name = skill.get("name") or "skill"
    return f"Active skill: {name}\n{instructions}"


def allowed_tools(skill: dict[str, Any] | None) -> list[str] | None:
    """The tool allow-list for a skill, or ``None`` for "no restriction".

    An empty list is a real answer — a skill that wants **no** tools — so this
    returns `[]` (not `None`) when that is what was stored. Conflating the two would
    silently hand a reading-only skill the command tools.

    A **missing** key is also `None` ("no restriction"): the row may come from a
    version that did not record a filter, and inventing an empty list there would
    break every existing skill by removing its tools.
    """
    if skill is None:
        return None
    raw = skill.get("allowed_tools")
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, str)]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, list):
            return [str(item) for item in parsed if isinstance(item, str)]
        return None
    return []
