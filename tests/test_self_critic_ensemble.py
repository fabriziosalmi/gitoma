"""Tests for the PHASE 5 reviewer ENSEMBLE (≥2-of-N agreement, 2026-05-02).

Three layers exercised:

1. ``_fingerprint`` — bucket key for cross-reviewer agreement matching.
2. ``merge_ensemble_findings`` — pure folder that takes per-member
   finding lists and applies the agreement floor.
3. ``SelfCriticAgent`` end-to-end with N reviewers mocked — verifies
   the parallel fan-out, dedupe-within-member rule, severity
   selection, and PR-comment ensemble header.

Closes the b2v PR #34 (2026-05-01) post-mortem: solo gemma-4-e2b
flagged "no issues" on a diff with hallucinated nav-links + boilerplate.
≥2-of-N consensus across diverse reviewers kills the single-model
blind spot without losing signal.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from gitoma.review.self_critic import (
    Finding,
    SelfCriticAgent,
    _fingerprint,
    merge_ensemble_findings,
    render_comment_body,
)


# ── _fingerprint ─────────────────────────────────────────────────────────────


def test_fingerprint_buckets_close_lines_together():
    a = Finding(severity="major", file="a.py", line=10, title="x", detail="")
    b = Finding(severity="major", file="a.py", line=12, title="x", detail="")
    assert _fingerprint(a) == _fingerprint(b)


def test_fingerprint_separates_distant_lines():
    a = Finding(severity="major", file="a.py", line=10, title="x", detail="")
    b = Finding(severity="major", file="a.py", line=99, title="x", detail="")
    assert _fingerprint(a) != _fingerprint(b)


def test_fingerprint_excludes_severity():
    """Same defect at major vs minor IS agreement — severity must not
    be in the bucket key."""
    a = Finding(severity="major", file="a.py", line=10, title="x", detail="")
    b = Finding(severity="minor", file="a.py", line=10, title="x", detail="")
    assert _fingerprint(a) == _fingerprint(b)


def test_fingerprint_normalises_title():
    a = Finding(severity="major", file="a.py", line=10, title="Magic   Number", detail="")
    b = Finding(severity="major", file="a.py", line=10, title="magic number", detail="")
    assert _fingerprint(a) == _fingerprint(b)


def test_fingerprint_handles_no_line():
    f = Finding(severity="major", file="a.py", line=None, title="x", detail="")
    fp = _fingerprint(f)
    assert fp[1] == -1


def test_fingerprint_separates_files():
    a = Finding(severity="major", file="a.py", line=10, title="x", detail="")
    b = Finding(severity="major", file="b.py", line=10, title="x", detail="")
    assert _fingerprint(a) != _fingerprint(b)


# ── merge_ensemble_findings ──────────────────────────────────────────────────


def _f(sev: str, file: str | None, line: int | None, title: str, detail: str = "d") -> Finding:
    return Finding(severity=sev, file=file, line=line, title=title, detail=detail)


def test_merge_keeps_2of3_agreement():
    a = [_f("major", "x.py", 10, "magic number")]
    b = [_f("major", "x.py", 11, "magic number")]
    c = [_f("major", "y.py", 5, "different finding")]
    out = merge_ensemble_findings([a, b, c], min_agree=2)
    titles = [f.title for f in out]
    assert "magic number" in titles
    assert "different finding" not in titles
    assert len(out) == 1


def test_merge_drops_singletons():
    a = [_f("major", "x.py", 10, "lonely")]
    b = [_f("major", "y.py", 5, "alone")]
    out = merge_ensemble_findings([a, b], min_agree=2)
    assert out == []


def test_merge_picks_highest_severity_across_votes():
    a = [_f("minor", "x.py", 10, "issue")]
    b = [_f("blocker", "x.py", 10, "issue")]
    out = merge_ensemble_findings([a, b], min_agree=2)
    assert len(out) == 1
    assert out[0].severity == "blocker"


def test_merge_picks_longest_detail():
    a = [_f("major", "x.py", 10, "issue", detail="short")]
    b = [_f("major", "x.py", 10, "issue", detail="much longer explanation")]
    out = merge_ensemble_findings([a, b], min_agree=2)
    assert out[0].detail == "much longer explanation"


def test_merge_dedup_within_single_member():
    """One chatty reviewer reporting the same defect 3 times must not
    satisfy a 2-of-N agreement floor on its own."""
    a = [
        _f("major", "x.py", 10, "issue"),
        _f("major", "x.py", 11, "issue"),  # same fingerprint
        _f("major", "x.py", 12, "issue"),  # same fingerprint
    ]
    b = []
    out = merge_ensemble_findings([a, b], min_agree=2)
    assert out == []


def test_merge_min_agree_3_of_3():
    a = [_f("major", "x.py", 10, "issue")]
    b = [_f("major", "x.py", 10, "issue")]
    c = [_f("major", "x.py", 10, "issue")]
    out = merge_ensemble_findings([a, b, c], min_agree=3)
    assert len(out) == 1


def test_merge_results_sorted_by_severity():
    a = [_f("nit", "x.py", 1, "nit"), _f("blocker", "x.py", 5, "blocker")]
    b = [_f("nit", "x.py", 1, "nit"), _f("blocker", "x.py", 5, "blocker")]
    out = merge_ensemble_findings([a, b], min_agree=2)
    assert [f.severity for f in out] == ["blocker", "nit"]


def test_merge_zero_min_agree_clamped_to_one():
    a = [_f("major", "x.py", 10, "issue")]
    out = merge_ensemble_findings([a], min_agree=0)
    assert len(out) == 1


def test_merge_empty_inputs_returns_empty():
    assert merge_ensemble_findings([], min_agree=2) == []
    assert merge_ensemble_findings([[], [], []], min_agree=2) == []


# ── render_comment_body — ensemble header ────────────────────────────────────


def test_render_body_ensemble_header_carries_min_agree_and_members():
    findings = [_f("major", "x.py", 10, "issue")]
    body = render_comment_body(
        findings,
        ensemble_models=["m-A", "m-B", "m-C"],
        min_agree=2,
    )
    assert "ensemble 2/3" in body
    assert "`m-A`" in body
    assert "`m-B`" in body
    assert "`m-C`" in body
    assert "agreed on by ≥2" in body


def test_render_body_solo_path_unchanged_when_no_ensemble():
    findings = [_f("major", "x.py", 10, "issue")]
    body = render_comment_body(findings)
    assert "ensemble" not in body.lower()


def test_render_body_empty_ensemble_says_no_consensus():
    body = render_comment_body([], ensemble_models=["a", "b"], min_agree=2)
    assert "agreement floor" in body.lower()


# ── SelfCriticAgent end-to-end ensemble ──────────────────────────────────────


def _make_ensemble_agent(mocker, member_responses: list[str]):
    """Build a SelfCriticAgent wired to N mocked reviewer clients.

    Each response in ``member_responses`` is the raw string that the
    matching reviewer will return from ``chat()``. The fixture sets up
    config.lmstudio so ``is_review_ensemble()`` returns True with the
    given member count and min_agree=2.
    """
    n = len(member_responses)
    urls = [f"http://host{i}:1234/v1" for i in range(n)]
    models = [f"model-{chr(65 + i)}" for i in range(n)]

    mock_config = MagicMock()
    mock_config.lmstudio.model = "planner-fallback"
    mock_config.lmstudio.review_base_url = ""
    mock_config.lmstudio.review_model = ""
    mock_config.lmstudio.is_review_ensemble = lambda: True
    mock_config.lmstudio.parsed_review_base_urls = lambda: list(urls)
    mock_config.lmstudio.parsed_review_models = lambda: list(models)
    mock_config.lmstudio.review_ensemble_min_agree = 2

    member_clients: list[MagicMock] = []
    for i, response in enumerate(member_responses):
        c = MagicMock()
        c.chat.return_value = response
        c.model = models[i]
        member_clients.append(c)

    # ``LLMClient`` is called once per member in __init__. Use side_effect
    # so each instantiation pulls the next pre-configured mock client.
    mocker.patch(
        "gitoma.review.self_critic.LLMClient",
        side_effect=member_clients,
    )
    gh_instance = MagicMock()
    mocker.patch(
        "gitoma.review.self_critic.GitHubClient",
        return_value=gh_instance,
    )

    return SelfCriticAgent(mock_config), member_clients, gh_instance


def _mock_pr(gh_instance):
    pr = MagicMock()
    pr.title = "T"
    pr.body = "B"
    pr.get_files.return_value = [
        MagicMock(filename="src/x.py", patch="+def foo():\n+    return 1"),
    ]
    repo = MagicMock()
    repo.get_pull.return_value = pr
    issue = MagicMock()
    repo.get_issue.return_value = issue
    gh_instance.get_repo.return_value = repo
    return pr, issue


def test_ensemble_init_builds_n_clients():
    """N reviewer URLs / models → N LLMClient instances."""
    import unittest.mock as _um

    mock_config = MagicMock()
    mock_config.lmstudio.model = "planner-fallback"
    mock_config.lmstudio.review_base_url = ""
    mock_config.lmstudio.review_model = ""
    mock_config.lmstudio.is_review_ensemble = lambda: True
    mock_config.lmstudio.parsed_review_base_urls = lambda: ["u1", "u2", "u3"]
    mock_config.lmstudio.parsed_review_models = lambda: ["m1", "m2", "m3"]
    mock_config.lmstudio.review_ensemble_min_agree = 2

    with _um.patch("gitoma.review.self_critic.LLMClient") as Mock, \
         _um.patch("gitoma.review.self_critic.GitHubClient"):
        agent = SelfCriticAgent(mock_config)
        assert len(agent.llms) == 3
        assert agent.min_agree == 2
        # One LLMClient call per member.
        assert Mock.call_count == 3


def test_ensemble_min_agree_capped_at_member_count(mocker):
    """A misconfig (MIN_AGREE=5 with N=3) must not deadlock the pipeline.
    Cap to N so the worst case is "all must agree" instead of "nothing
    ever passes the floor"."""
    mock_config = MagicMock()
    mock_config.lmstudio.model = "planner-fallback"
    mock_config.lmstudio.review_base_url = ""
    mock_config.lmstudio.review_model = ""
    mock_config.lmstudio.is_review_ensemble = lambda: True
    mock_config.lmstudio.parsed_review_base_urls = lambda: ["u1", "u2"]
    mock_config.lmstudio.parsed_review_models = lambda: ["m1", "m2"]
    mock_config.lmstudio.review_ensemble_min_agree = 5

    mocker.patch("gitoma.review.self_critic.LLMClient")
    mocker.patch("gitoma.review.self_critic.GitHubClient")
    agent = SelfCriticAgent(mock_config)
    assert agent.min_agree == 2  # capped from 5 → N=2


def test_ensemble_review_pr_keeps_only_consensus(mocker):
    """3 reviewers → only findings ≥2 agree on get posted."""
    consensus = (
        '[{"severity":"major","file":"src/x.py","line":1,'
        '"title":"magic number","detail":"d"}]'
    )
    only_one = (
        '[{"severity":"nit","file":"src/x.py","line":99,'
        '"title":"lonely opinion","detail":"d"}]'
    )
    agent, members, gh = _make_ensemble_agent(
        mocker, [consensus, consensus, only_one]
    )
    pr, issue = _mock_pr(gh)

    result = agent.review_pr("o", "r", 42)

    assert len(result.findings) == 1
    assert result.findings[0].title == "magic number"
    assert result.comment_posted is True
    # All 3 members were called.
    assert all(c.chat.called for c in members)
    # Result carries ensemble metadata.
    assert result.ensemble_min_agree == 2
    assert len(result.ensemble_models) == 3
    assert len(result.per_member_findings) == 3


def test_ensemble_review_pr_empty_when_no_consensus(mocker):
    """3 reviewers with mutually disjoint findings → 0 posted."""
    a = '[{"severity":"major","file":"a.py","line":1,"title":"A","detail":""}]'
    b = '[{"severity":"major","file":"b.py","line":1,"title":"B","detail":""}]'
    c = '[{"severity":"major","file":"c.py","line":1,"title":"C","detail":""}]'
    agent, _members, gh = _make_ensemble_agent(mocker, [a, b, c])
    pr, issue = _mock_pr(gh)

    result = agent.review_pr("o", "r", 42)
    assert result.findings == []
    assert result.comment_posted is False
    issue.create_comment.assert_not_called()


def test_ensemble_review_pr_skipped_when_all_members_fail(mocker):
    """If every member raises LLMError, return a fatal skip — not a
    silent empty pass."""
    from gitoma.planner.llm_client import LLMError

    agent, members, gh = _make_ensemble_agent(mocker, ["", "", ""])
    for c in members:
        c.chat.side_effect = LLMError("connection refused")
    _pr, _issue = _mock_pr(gh)

    result = agent.review_pr("o", "r", 42)
    assert result.findings == []
    assert "connection refused" in (result.skipped_reason or "")


def test_ensemble_review_pr_partial_failure_still_merges(mocker):
    """1 member errors but 2 succeed with consensus → still post."""
    from gitoma.planner.llm_client import LLMError

    consensus = (
        '[{"severity":"major","file":"src/x.py","line":1,'
        '"title":"shared","detail":"d"}]'
    )
    agent, members, gh = _make_ensemble_agent(
        mocker, [consensus, consensus, ""]
    )
    members[2].chat.side_effect = LLMError("503 from host3")
    _pr, _issue = _mock_pr(gh)

    result = agent.review_pr("o", "r", 42)
    assert len(result.findings) == 1
    assert result.findings[0].title == "shared"


def test_ensemble_review_pr_posted_body_carries_ensemble_header(mocker):
    consensus = (
        '[{"severity":"major","file":"src/x.py","line":1,'
        '"title":"magic","detail":"d"}]'
    )
    agent, _members, gh = _make_ensemble_agent(
        mocker, [consensus, consensus]
    )
    _pr, issue = _mock_pr(gh)

    agent.review_pr("o", "r", 42)
    body = issue.create_comment.call_args[0][0]
    assert "ensemble 2/2" in body
    assert "`model-A`" in body
    assert "`model-B`" in body
