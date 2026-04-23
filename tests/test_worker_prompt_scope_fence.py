"""Tests for the worker prompt's SCOPE BOUNDARIES block — the
prompt-level fence added after rung-3 v13 found the worker over-
scoping a "Verify Test Coverage" task into a stdlib→psycopg2
rewrite. The patcher caught nothing because the rewrite was
syntactically valid; the only place to push back is the prompt."""

from __future__ import annotations

from gitoma.planner.prompts import worker_user_prompt


def _render(**overrides) -> str:
    """Minimal valid worker_user_prompt invocation."""
    defaults = dict(
        subtask_title="Fix SQL Injection in find_user_by_name",
        subtask_description="Replace f-string interpolation with a parameterised query.",
        file_hints=["src/db.py"],
        languages=["Python"],
        repo_name="bench-rung-3",
        current_files={"src/db.py": "def find_user_by_name(conn, name):\n    pass\n"},
        file_tree=["src/db.py", "tests/test_db.py", "pyproject.toml"],
    )
    defaults.update(overrides)
    return worker_user_prompt(**defaults)


# ── Block presence ──────────────────────────────────────────────────────


def test_boundaries_block_appears() -> None:
    rendered = _render()
    assert "SCOPE BOUNDARIES" in rendered
    assert "Files to touch" in rendered


def test_boundaries_block_lists_six_rules() -> None:
    """Six numbered boundaries — file-fence, no new imports, no signature
    change, no helper deletion, no phantom imports, minimal-change."""
    rendered = _render()
    for n in range(1, 7):
        assert f"  {n}. " in rendered, f"rule {n} missing"


# ── Rule 1: scope-fence on file_hints ───────────────────────────────────


def test_rule1_blocks_inventing_scaffolding() -> None:
    """The ``__init__.py`` / ``main.py`` / "structure refactor" pattern
    that wrecked rung-3 v13 must be explicitly named."""
    rendered = _render()
    assert "scaffolding" in rendered.lower()
    assert "__init__.py" in rendered
    assert "main.py" in rendered


# ── Rule 2: no new top-level imports unless explicitly requested ────────


def test_rule2_calls_out_psycopg2_rewrite_specifically() -> None:
    """v13's exact failure: stdlib sqlite3 → psycopg2 rewrite."""
    rendered = _render()
    assert "sqlite3" in rendered
    assert "psycopg2" in rendered
    assert "architectural rewrite" in rendered


def test_rule2_generalises_beyond_python() -> None:
    """Don't only hard-code Python — the rule covers any language."""
    rendered = _render()
    # Should mention import / use / require (Python / Rust / JS keywords)
    assert "import" in rendered
    assert "use" in rendered
    assert "require" in rendered


# ── Rule 3: signature stability ─────────────────────────────────────────


def test_rule3_protects_signatures() -> None:
    rendered = _render()
    assert "function signatures" in rendered
    assert "Tests and other callers" in rendered or "callers" in rendered


# ── Rule 4: no helper deletion ──────────────────────────────────────────


def test_rule4_protects_helpers_by_name() -> None:
    """Cite the exact rung-3 v13 case — get_conn/init_schema/seed.
    Specific examples land harder than abstract rules."""
    rendered = _render()
    assert "get_conn" in rendered
    assert "init_schema" in rendered
    assert "seed" in rendered


def test_rule4_includes_wrong_right_examples() -> None:
    """rung-3 v14 fallout: rule-4 was present but qwen3-8b violated
    it anyway, deleting helpers and emitting the lie ``# init_schema
    and seed remain unchanged``. WRONG/RIGHT examples in the prompt
    (same pattern that worked for the Defender) close that gap."""
    rendered = _render()
    # The WRONG block should reproduce the v14 actual output shape so
    # the model recognises its own pattern when about to produce it.
    assert "WRONG" in rendered
    assert "RIGHT" in rendered
    # Specific lie phrasing the v14 worker produced — naming it makes
    # the rule about what it actually does, not abstract style.
    assert "remains unchanged" in rendered or "remain unchanged" in rendered
    # Concrete instruction that comments don't substitute for code.
    assert "comment" in rendered.lower()
    assert "lying" in rendered.lower() or "lie" in rendered.lower()


# ── Rule 5: phantom imports ─────────────────────────────────────────────


def test_rule5_catches_phantom_imports() -> None:
    """The other v13 wreck: ``from .db import connect_to_database``
    where ``connect_to_database`` didn't exist anywhere."""
    rendered = _render()
    assert "connect_to_database" in rendered
    assert "ACTUALLY EXIST" in rendered or "actually exist" in rendered.lower()


# ── Rule 6: minimal-change ──────────────────────────────────────────────


def test_rule6_prefers_minimal_change() -> None:
    rendered = _render()
    assert "Minimal-change" in rendered or "smallest patch" in rendered


# ── Compose with retry feedback ─────────────────────────────────────────


def test_boundaries_appear_alongside_retry_feedback() -> None:
    """When the previous attempt broke the build, both the boundaries
    block AND the retry-feedback block must render — the boundaries
    are about scope, the retry is about the specific compiler error."""
    rendered = _render(compile_error_feedback="syntax error at line 1")
    assert "SCOPE BOUNDARIES" in rendered
    assert "PREVIOUS ATTEMPT FAILED TO COMPILE" in rendered


# ── Compose with file_hints empty fallback ──────────────────────────────


def test_boundaries_present_even_with_empty_file_hints() -> None:
    """Some planner outputs leave file_hints empty (``determine
    appropriate files``) — the scope-fence rules still apply."""
    rendered = _render(file_hints=[])
    assert "SCOPE BOUNDARIES" in rendered
    assert "Files to touch" in rendered


# ── Boundaries placed BEFORE the JSON schema ────────────────────────────


def test_boundaries_appear_before_json_schema() -> None:
    """LLMs read top-down. The fence must be in front of the schema
    so the model has the constraints internalised when it starts
    composing the patches array."""
    rendered = _render()
    boundaries_pos = rendered.find("SCOPE BOUNDARIES")
    schema_pos = rendered.find('"patches"')
    assert boundaries_pos != -1 and schema_pos != -1
    assert boundaries_pos < schema_pos
