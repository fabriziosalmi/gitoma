"""Tests for the Phase 5 LLM self-critic.

Three layers exercised:

1. Pure parsers (``parse_findings`` + ``render_comment_body``) — run
   without touching an LLM or GitHub. These are where edge cases like
   "model returned garbage", "bad severity string", "integer line
   number as string" live.
2. ``SelfCriticAgent.review_pr`` end-to-end with PyGithub and the LLM
   client mocked — verifies the integration flow: fetch diff, call
   LLM, parse findings, post summary comment, return a
   ``SelfReviewResult``.
3. Regression: the critic must NEVER raise into the caller, because
   Phase 4 has already succeeded (PR exists) — a failing critic just
   returns ``skipped_reason``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from gitoma.review.self_critic import (
    Finding,
    SelfCriticAgent,
    parse_findings,
    render_comment_body,
)


# ── parse_findings ───────────────────────────────────────────────────────────


def test_parse_findings_plain_json_array():
    raw = '[{"severity":"major","file":"x.py","line":10,"title":"T","detail":"D"}]'
    out = parse_findings(raw)
    assert len(out) == 1
    assert out[0].severity == "major"
    assert out[0].file == "x.py"
    assert out[0].line == 10


def test_parse_findings_fenced_markdown():
    raw = "```json\n[{\"severity\":\"minor\",\"title\":\"x\",\"detail\":\"\"}]\n```"
    out = parse_findings(raw)
    assert len(out) == 1
    assert out[0].severity == "minor"


def test_parse_findings_embedded_in_prose():
    """The critic adds extra prose — we should still salvage the array."""
    raw = 'Here is the JSON:\n[{"severity":"blocker","title":"t","detail":""}]\nDone.'
    out = parse_findings(raw)
    assert len(out) == 1
    assert out[0].severity == "blocker"


def test_parse_findings_empty_array_is_fine():
    assert parse_findings("[]") == []
    assert parse_findings("```json\n[]\n```") == []


def test_parse_findings_non_json_returns_empty():
    assert parse_findings("I don't see any issues.") == []
    assert parse_findings("") == []


def test_parse_findings_malformed_json_returns_empty():
    """A critic that returns broken JSON must not crash the pipeline."""
    assert parse_findings('[{"severity":"major", "title"') == []


def test_parse_findings_normalizes_unknown_severity():
    raw = '[{"severity":"nuclear","title":"t","detail":""}]'
    out = parse_findings(raw)
    assert out[0].severity == "minor"  # fall back to minor, not crash


def test_parse_findings_coerces_line_as_string():
    raw = '[{"severity":"nit","title":"x","line":"12","detail":""}]'
    out = parse_findings(raw)
    assert out[0].line == 12


def test_parse_findings_non_list_returns_empty():
    raw = '{"severity":"major","title":"t","detail":""}'
    assert parse_findings(raw) == []


def test_parse_findings_caps_at_30_findings():
    raw = (
        "["
        + ",".join(
            f'{{"severity":"nit","title":"t{i}","detail":""}}'
            for i in range(50)
        )
        + "]"
    )
    out = parse_findings(raw)
    assert len(out) == 30


# ── render_comment_body ──────────────────────────────────────────────────────


def test_render_body_groups_findings_by_severity():
    findings = [
        Finding(severity="major", file="a.py", line=1, title="A", detail="aaa"),
        Finding(severity="blocker", file="b.py", line=2, title="B", detail="bbb"),
        Finding(severity="minor", file=None, line=None, title="C", detail=""),
    ]
    body = render_comment_body(findings)
    # Blocker section appears first regardless of input order.
    blocker_idx = body.find("Blocker")
    major_idx = body.find("Major")
    minor_idx = body.find("Minor")
    assert 0 < blocker_idx < major_idx < minor_idx
    # Locations rendered where available.
    assert "`a.py`:1" in body
    # No-file findings just skip the location suffix.
    assert "- **C**" in body


def test_render_body_empty_findings_renders_lgtm_message():
    body = render_comment_body([])
    assert "No issues found" in body


def test_render_body_includes_signature_line():
    body = render_comment_body(
        [Finding(severity="minor", file=None, line=None, title="t", detail="")]
    )
    assert "gitoma self-review" in body


# ── SelfCriticAgent end-to-end with mocks ────────────────────────────────────


def _make_agent(mocker, llm_response: str):
    mock_config = MagicMock()
    mock_config.lmstudio.model = "mock-gemma"

    llm_instance = MagicMock()
    llm_instance.chat.return_value = llm_response

    gh_instance = MagicMock()

    mocker.patch("gitoma.review.self_critic.LLMClient", return_value=llm_instance)
    mocker.patch("gitoma.review.self_critic.GitHubClient", return_value=gh_instance)

    return SelfCriticAgent(mock_config), llm_instance, gh_instance


def _mock_pr_with_files(gh_instance, files=None, title="T", body="B"):
    files = files or [
        MagicMock(filename="src/x.py", patch="+def foo():\n+    return 1"),
    ]
    pr = MagicMock()
    pr.title = title
    pr.body = body
    pr.get_files.return_value = files
    repo = MagicMock()
    repo.get_pull.return_value = pr
    issue = MagicMock()
    repo.get_issue.return_value = issue
    gh_instance.get_repo.return_value = repo
    return pr, issue


def test_review_pr_posts_summary_comment_when_findings_exist(mocker):
    response = (
        '[{"severity":"major","file":"src/x.py","line":1,'
        '"title":"magic number","detail":"Explain why 1."}]'
    )
    agent, _llm, gh = _make_agent(mocker, response)
    pr, issue = _mock_pr_with_files(gh)

    result = agent.review_pr("o", "r", 42)

    assert len(result.findings) == 1
    assert result.comment_posted is True
    issue.create_comment.assert_called_once()
    posted_body = issue.create_comment.call_args[0][0]
    assert "major" in posted_body.lower()
    assert "magic number" in posted_body.lower()


def test_review_pr_does_not_post_when_llm_returns_empty(mocker):
    """No noisy 'LGTM' comment when the critic had nothing to say."""
    agent, _llm, gh = _make_agent(mocker, "[]")
    pr, issue = _mock_pr_with_files(gh)

    result = agent.review_pr("o", "r", 42)

    assert result.findings == []
    assert result.comment_posted is False
    issue.create_comment.assert_not_called()


def test_review_pr_skipped_reason_on_empty_diff(mocker):
    agent, _llm, gh = _make_agent(mocker, "[]")
    # PR with no file patches → empty diff
    _mock_pr_with_files(gh, files=[MagicMock(filename="empty.py", patch="")])

    result = agent.review_pr("o", "r", 42)
    assert result.skipped_reason == "empty diff"
    assert result.findings == []


def test_review_pr_returns_skipped_when_llm_fails(mocker):
    from gitoma.planner.llm_client import LLMError

    agent, llm, gh = _make_agent(mocker, "")
    llm.chat.side_effect = LLMError("connection refused")
    _mock_pr_with_files(gh)

    result = agent.review_pr("o", "r", 42)
    assert result.findings == []
    assert result.comment_posted is False
    assert "connection refused" in (result.skipped_reason or "")


def test_review_pr_returns_skipped_when_fetching_pr_fails(mocker):
    agent, _llm, gh = _make_agent(mocker, "")
    gh.get_repo.side_effect = RuntimeError("404 not found")

    result = agent.review_pr("o", "r", 99)
    assert result.findings == []
    assert "fetch pr" in (result.skipped_reason or "")


def test_review_pr_still_returns_result_when_comment_post_fails(mocker):
    """If the comment post itself raises (rate-limit, token scope, …),
    we keep the findings the LLM produced and just report comment_posted=False —
    Phase 5 must never raise into Phase 4's caller."""
    response = '[{"severity":"minor","title":"x","detail":""}]'
    agent, _llm, gh = _make_agent(mocker, response)
    pr, issue = _mock_pr_with_files(gh)
    issue.create_comment.side_effect = RuntimeError("403 rate limit")

    result = agent.review_pr("o", "r", 42)

    assert len(result.findings) == 1
    assert result.comment_posted is False


def test_review_pr_truncates_large_diff(mocker):
    """Huge diffs don't blow the LLM context window — we cap to ~40 KB."""
    big_patch = "+x\n" * 50_000  # ~200 KB
    agent, llm, gh = _make_agent(mocker, "[]")
    _mock_pr_with_files(gh, files=[MagicMock(filename="big.py", patch=big_patch)])

    agent.review_pr("o", "r", 42)

    sent_prompt = llm.chat.call_args[0][0][0]["content"]
    assert "diff truncated" in sent_prompt
    assert len(sent_prompt) < 100_000  # sanity — prompt is bounded
