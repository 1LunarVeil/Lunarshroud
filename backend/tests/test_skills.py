"""Skill `@mention` contract and tool-filter resolution.

The detection rules are the interesting part: every negative case below is a
plausible way to *write about* a skill without asking for it. Getting one wrong
means the model burns a turn loading instructions the user did not request, and the
symptom would look like "the model randomly behaved differently".
"""
from __future__ import annotations

import pytest
from core import skills

NAMES = ["reviewer", "skill", "skill-creator", "pdf-tools"]


def test_a_plain_mention_is_detected():
    assert skills.mentioned_skills("please use @reviewer for this", NAMES) == ["reviewer"]


def test_matching_is_case_insensitive_but_the_stored_name_wins():
    assert skills.mentioned_skills("@Reviewer", NAMES) == ["reviewer"]


def test_no_prefix_matching():
    """`@skill-creator` must not also load `skill`."""
    assert skills.mentioned_skills("@skill-creator", NAMES) == ["skill-creator"]
    assert skills.mentioned_skills("@skill", NAMES) == ["skill"]


def test_a_mention_inside_an_inline_code_span_is_not_a_request():
    assert skills.mentioned_skills("type `@reviewer` to use it", NAMES) == []


def test_a_mention_inside_a_fenced_block_is_not_a_request():
    message = "example:\n```\n@reviewer\n```\nthat is all"
    assert skills.mentioned_skills(message, NAMES) == []


def test_an_unterminated_fence_swallows_the_rest():
    """A user mid-code has not asked for anything after the fence."""
    assert skills.mentioned_skills("here:\n```\n@reviewer", NAMES) == []


def test_a_blockquote_mention_is_not_a_request():
    assert skills.mentioned_skills("> @reviewer said so", NAMES) == []


def test_a_url_or_path_mention_is_not_a_request():
    for message in (
        "see https://example.com/@reviewer",
        r"open C:\work\@reviewer",
        "mail me at me@reviewer",
    ):
        assert skills.mentioned_skills(message, NAMES) == [], message


def test_an_unknown_name_is_an_ordinary_word():
    assert skills.mentioned_skills("@somebody hello", NAMES) == []


def test_mentions_are_returned_in_first_appearance_order_without_duplicates():
    assert skills.mentioned_skills("@pdf-tools then @reviewer then @pdf-tools", NAMES) == [
        "pdf-tools",
        "reviewer",
    ]


def test_only_the_latest_user_message_counts_for_auto_selection():
    """A mention in history is not a request for this turn."""
    skill = {"name": "reviewer", "instructions": "review", "allowed_tools": []}
    messages = [
        {"role": "user", "content": "@reviewer please"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "now something else"},
    ]
    assert skills.resolve_active_skill(None, messages, [skill]) is None


def test_an_explicit_choice_beats_a_mention():
    """A pinned skill must not be displaced by a passing mention."""
    pinned = {"name": "reviewer", "instructions": "r", "allowed_tools": []}
    other = {"name": "pdf-tools", "instructions": "p", "allowed_tools": []}
    messages = [{"role": "user", "content": "@pdf-tools do this"}]
    assert skills.resolve_active_skill("reviewer", messages, [pinned, other]) is pinned


def test_an_empty_allow_list_means_no_tools_not_all_tools():
    """`[]` and `None` are different answers and must stay different."""
    assert skills.allowed_tools({"name": "x", "allowed_tools": []}) == []
    assert skills.allowed_tools({"name": "x", "allowed_tools": None}) is None
    assert skills.allowed_tools(None) is None


def test_allowed_tools_accepts_the_stored_json_string():
    assert skills.allowed_tools({"name": "x", "allowed_tools": '["read_file"]'}) == ["read_file"]
    # A malformed value is an empty allow-list, never "no restriction".
    assert skills.allowed_tools({"name": "x", "allowed_tools": "{broken"}) == []


@pytest.mark.parametrize(
    "name",
    ["", "  ", "MySkill", "has space", "-leading", "x" * 65, "emoji🙂"],
)
def test_invalid_skill_names_are_refused(name):
    with pytest.raises(ValueError, match="skill name"):
        skills.normalize_skill_name(name)


def test_valid_skill_names_are_accepted():
    for name in ("reviewer", "pdf-tools", "a", "v2.1_beta"):
        assert skills.normalize_skill_name(name) == name


def test_the_injected_block_names_the_skill():
    context = skills.skill_context({"name": "reviewer", "instructions": "be strict"})
    assert "reviewer" in context
    assert "be strict" in context


def test_a_skill_with_no_instructions_injects_nothing():
    assert skills.skill_context({"name": "reviewer", "instructions": ""}) == ""
