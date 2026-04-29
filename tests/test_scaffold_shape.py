"""Tests for the PHASE 1.7 scaffold-shape inference + delta module.

Pure-function tests — no occam-trees server needed."""

from __future__ import annotations

from gitoma.context import RepoBrief
from gitoma.planner.scaffold_shape import (
    DEFAULT_MAX_CHARS,
    StackInferenceResult,
    compute_delta,
    infer_level,
    infer_stack,
    render_shape_context,
)


# ── Sample stacks (subset of the real occam-trees catalog) ────────


SAMPLE_STACKS = [
    {"id": "mern", "rank": 1, "name": "MERN",
     "components": ["MongoDB", "Express.js", "React", "Node.js"]},
    {"id": "django-react", "rank": 8, "name": "Django + React",
     "components": ["Python", "Django", "React", "PostgreSQL"]},
    {"id": "farm", "rank": 14, "name": "FARM",
     "components": ["Python", "FastAPI", "React", "MongoDB"]},
    {"id": "fastapi-vue", "rank": 32, "name": "FastAPI + Vue",
     "components": ["Python", "FastAPI", "Vue.js", "PostgreSQL"]},
    {"id": "pytorch-fastapi", "rank": 90, "name": "PyTorch + FastAPI",
     "components": ["Python", "PyTorch", "FastAPI", "Docker"]},
    {"id": "langchain-fastapi", "rank": 96, "name": "LangChain + FastAPI",
     "components": ["Python", "LangChain", "FastAPI", "Pinecone"]},
]


# ── infer_stack ───────────────────────────────────────────────────


def test_infer_stack_no_brief_signals_returns_none() -> None:
    brief = RepoBrief(stack=[])
    assert infer_stack(brief, SAMPLE_STACKS) is None


def test_infer_stack_no_catalog_returns_none() -> None:
    brief = RepoBrief(stack=["Python", "FastAPI"])
    assert infer_stack(brief, []) is None


def test_infer_stack_picks_best_match_by_count() -> None:
    """Brief = ['Python','FastAPI','React','MongoDB'] → FARM (4 matches),
    not django-react (only Python+React = 2)."""
    brief = RepoBrief(stack=["Python", "FastAPI", "React", "MongoDB"])
    res = infer_stack(brief, SAMPLE_STACKS)
    assert res is not None
    assert res.stack_id == "farm"
    assert res.match_count == 4
    assert set(res.matched_components) == {"Python", "FastAPI", "React", "MongoDB"}


def test_infer_stack_below_threshold_returns_none() -> None:
    """Only 1 match ('Python') across all stacks → below default threshold of 2."""
    brief = RepoBrief(stack=["Python"])
    assert infer_stack(brief, SAMPLE_STACKS) is None


def test_infer_stack_threshold_override() -> None:
    """Caller can lower the bar to 1 match."""
    brief = RepoBrief(stack=["Python"])
    res = infer_stack(brief, SAMPLE_STACKS, min_matches=1)
    assert res is not None
    # With 1-match threshold, all 4 Python stacks tie at match_count=1.
    # Tie-break = lowest rank = django-react (rank=8).
    assert res.stack_id == "django-react"


def test_infer_stack_tiebreak_by_rank() -> None:
    """Two stacks with equal match_count → lower rank wins."""
    # Brief matches Python+FastAPI in MULTIPLE stacks (farm rank=14,
    # fastapi-vue rank=32, pytorch-fastapi rank=90, langchain rank=96).
    # All score 2 except farm which would be 2 too. → farm wins on rank.
    brief = RepoBrief(stack=["Python", "FastAPI"])
    res = infer_stack(brief, SAMPLE_STACKS)
    assert res is not None
    assert res.stack_id == "farm"
    assert res.match_count == 2


def test_infer_stack_case_insensitive_and_punctuation_tolerant() -> None:
    """'node.js' and 'NodeJS' should both match 'Node.js'."""
    brief = RepoBrief(stack=["mongodb", "express.js", "REACT", "nodejs"])
    res = infer_stack(brief, SAMPLE_STACKS)
    assert res is not None
    assert res.stack_id == "mern"
    assert res.match_count == 4


def test_infer_stack_returns_top3_candidates() -> None:
    brief = RepoBrief(stack=["Python", "FastAPI"])
    res = infer_stack(brief, SAMPLE_STACKS)
    assert res is not None
    assert len(res.candidates) <= 3
    # All candidates should have at least 1 match
    for cand_id, count in res.candidates:
        assert isinstance(cand_id, str)
        assert count >= 1


def test_infer_stack_skips_malformed_entries() -> None:
    """Stack entries missing 'components' or with wrong types must
    not crash inference."""
    brief = RepoBrief(stack=["Python", "FastAPI"])
    bad_catalog = [
        {"id": "bad1"},  # no components
        {"id": "bad2", "components": "not-a-list"},  # wrong type
        {"id": "bad3", "components": [None, 42, "FastAPI"]},  # mixed
        SAMPLE_STACKS[2],  # farm — should still win
    ]
    res = infer_stack(brief, bad_catalog)
    assert res is not None
    assert res.stack_id == "farm"


# ── infer_level ───────────────────────────────────────────────────


def test_infer_level_empty_tree_is_l1() -> None:
    assert infer_level([]) == 1


def test_infer_level_tiny_repo_is_l1() -> None:
    """3 source files → L1."""
    tree = ["src/main.py", "README.md", "pyproject.toml"]
    assert infer_level(tree) == 1


def test_infer_level_excludes_docs_images_locks() -> None:
    """Only .py/.ts/etc count, not .md/.png/.lock."""
    tree = ["a.md", "b.png", "package-lock.json", "logo.svg"]
    assert infer_level(tree) == 1  # zero source files


def test_infer_level_excludes_node_modules_and_dist() -> None:
    """node_modules + dist + build excluded."""
    tree = (
        [f"node_modules/dep{i}/index.js" for i in range(50)]
        + [f"dist/bundle{i}.js" for i in range(50)]
        + ["src/index.ts", "src/util.ts"]
    )
    assert infer_level(tree) == 1  # only 2 source files counted


def test_infer_level_excludes_test_dirs() -> None:
    """tests/, __tests__/, spec/ excluded."""
    tree = (
        [f"tests/test_{i}.py" for i in range(20)]
        + ["src/main.py"]
    )
    assert infer_level(tree) == 1


def test_infer_level_l3_l4_boundary() -> None:
    """40 files = L4 boundary. 39 = L3, 41 = L4."""
    base_l3 = [f"src/m{i}.py" for i in range(39)]
    base_l4 = [f"src/m{i}.py" for i in range(41)]
    assert infer_level(base_l3) == 3
    assert infer_level(base_l4) == 4


def test_infer_level_clamps_to_10() -> None:
    """Massive repo → L10 ceiling."""
    huge = [f"src/m{i}.py" for i in range(20000)]
    assert infer_level(huge) == 10


# ── compute_delta ─────────────────────────────────────────────────


def test_compute_delta_empty_canonical() -> None:
    assert compute_delta([], ["src/main.py"]) == []


def test_compute_delta_all_present() -> None:
    canonical = [("src/index.ts", "entry"), ("package.json", "manifest")]
    current = ["src/index.ts", "package.json", "README.md"]
    assert compute_delta(canonical, current) == []


def test_compute_delta_returns_missing_only() -> None:
    canonical = [
        ("src/index.ts", "entry"),
        ("package.json", "manifest"),
        ("tsconfig.json", "framework-config"),
    ]
    current = ["src/index.ts", "README.md"]
    delta = compute_delta(canonical, current)
    paths = [p for p, _ in delta]
    assert "package.json" in paths
    assert "tsconfig.json" in paths
    assert "src/index.ts" not in paths


def test_compute_delta_dedupes_canonical() -> None:
    """Duplicate canonical entries collapse to one delta hit."""
    canonical = [
        ("src/index.ts", "entry"),
        ("src/index.ts", "entry"),
    ]
    delta = compute_delta(canonical, [])
    assert len(delta) == 1


def test_compute_delta_normalises_separators() -> None:
    """Backslashes treated as forward slashes for cross-platform."""
    canonical = [("src/index.ts", "entry")]
    current = ["src\\index.ts"]  # Windows-ish
    assert compute_delta(canonical, current) == []


def test_compute_delta_strips_trailing_slash_on_match() -> None:
    """Directory marker 'tests/' must match if 'tests' is in current."""
    canonical = [("tests/", "directory")]
    current = ["tests"]
    assert compute_delta(canonical, current) == []


def test_compute_delta_never_recommends_removal() -> None:
    """Files in current but NOT canonical are silently kept — delta is
    canonical \\ current, never current \\ canonical."""
    canonical = [("a.py", "src")]
    current = ["b.py", "c.py", "d.py"]
    delta = compute_delta(canonical, current)
    # Only "a.py" should appear (canonical missing); b/c/d ignored
    assert delta == [("a.py", "src")]


# ── render_shape_context ──────────────────────────────────────────


def test_render_empty_delta_returns_empty_string() -> None:
    assert render_shape_context(
        stack_id="mern", stack_name="MERN", level=2,
        matched_components=("React",), delta=[],
    ) == ""


def test_render_includes_stack_level_components() -> None:
    out = render_shape_context(
        stack_id="mern", stack_name="MERN", level=3,
        matched_components=("React", "Node.js"),
        delta=[("server/index.js", "entry")],
    )
    assert "MERN" in out
    assert "mern" in out
    assert "level 3" in out
    assert "React" in out
    assert "Node.js" in out
    assert "server/index.js" in out


def test_render_groups_by_top_dir() -> None:
    """Multiple files in the same top dir should appear under one
    group header."""
    delta = [
        ("src/index.ts", "entry"),
        ("src/util.ts", "lib"),
        ("config/eslint.json", "framework-config"),
    ]
    out = render_shape_context(
        stack_id="t3", stack_name="T3", level=3,
        matched_components=("TypeScript",), delta=delta,
    )
    # Group headers
    assert "  src/" in out
    assert "  config/" in out


def test_render_truncates_when_over_budget() -> None:
    """A massive delta gets truncated with a '…(N more)' notice."""
    delta = [(f"src/file{i}.ts", "src") for i in range(500)]
    out = render_shape_context(
        stack_id="t3", stack_name="T3", level=3,
        matched_components=("TypeScript",), delta=delta,
        max_chars=400,
    )
    assert len(out) <= 500  # must be near the budget
    assert "more" in out


def test_render_default_budget() -> None:
    """Default budget is the module constant."""
    delta = [(f"src/file{i}.ts", "src") for i in range(20)]
    out = render_shape_context(
        stack_id="t3", stack_name="T3", level=3,
        matched_components=("TypeScript",), delta=delta,
    )
    assert len(out) <= DEFAULT_MAX_CHARS + 100  # tolerance for truncation marker


def test_render_handles_root_paths() -> None:
    """Files at the repo root land in the '(root)' group."""
    out = render_shape_context(
        stack_id="mern", stack_name="MERN", level=2,
        matched_components=("Node.js",),
        delta=[("package.json", "manifest")],
    )
    assert "(root)" in out


def test_render_handles_empty_role() -> None:
    """Role-less paths render without the '[role]' suffix."""
    out = render_shape_context(
        stack_id="x", stack_name="X", level=1,
        matched_components=("Python",),
        delta=[("foo.py", "")],
    )
    assert "foo.py" in out
    assert "[]" not in out


# ── StackInferenceResult dataclass ────────────────────────────────


def test_stack_inference_result_is_frozen() -> None:
    res = StackInferenceResult(
        stack_id="mern", stack_name="MERN",
        matched_components=("React",), match_count=1,
    )
    import pytest
    with pytest.raises(Exception):
        res.stack_id = "other"  # type: ignore[misc]
