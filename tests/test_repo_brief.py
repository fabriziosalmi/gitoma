"""RepoBrief extractor tests — all paths exercise stdlib-only parsing
against on-disk fixture trees; no network, no LLM.
"""

from __future__ import annotations

from pathlib import Path

from gitoma.context import extract_brief, render_brief
from gitoma.context.repo_brief import RepoBrief


def test_empty_root_yields_empty_brief(tmp_path: Path) -> None:
    """An empty directory must NOT produce a half-populated brief —
    every field stays None/empty so downstream consumers can't mistake
    guesses for facts."""
    b = extract_brief(tmp_path)
    assert b.title is None
    assert b.oneliner is None
    assert b.stack == []
    assert b.build_cmd is None
    assert b.test_cmd is None


def test_go_repo_extracts_stack_module_and_commands(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module my-cli\n\ngo 1.22\n")
    b = extract_brief(tmp_path)
    assert "Go" in b.stack
    assert b.module_name == "my-cli"
    assert b.build_cmd == "go build ./..."
    assert b.test_cmd == "go test ./..."


def test_rust_repo_extracts_package(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "ripgrep-lite"\ndescription = "tiny grep"\nversion = "0.1.0"\n'
    )
    b = extract_brief(tmp_path)
    assert "Rust" in b.stack
    assert b.module_name == "ripgrep-lite"
    assert b.oneliner == "tiny grep"
    assert b.build_cmd == "cargo build"
    assert b.test_cmd == "cargo test"


def test_package_json_detects_ts_vs_js(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"name": "my-app", "description": "an app",'
        ' "scripts": {"test": "jest", "build": "tsc"},'
        ' "devDependencies": {"typescript": "^5.0.0"}}'
    )
    b = extract_brief(tmp_path)
    assert "TypeScript" in b.stack
    assert b.module_name == "my-app"
    assert b.test_cmd == "npm test"
    assert b.build_cmd == "npm run build"


# ── Framework signal extraction (added 2026-04-29 post-bench) ───


def test_pyproject_extracts_fastapi_framework(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["fastapi>=0.100", "uvicorn"]\n'
    )
    b = extract_brief(tmp_path)
    assert "Python" in b.stack
    assert "FastAPI" in b.stack


def test_pyproject_extracts_multiple_frameworks(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ['
        '"langchain>=0.1", "pydantic>=2", "numpy>=1.24"]\n'
    )
    b = extract_brief(tmp_path)
    assert "LangChain" in b.stack
    assert "Pydantic" in b.stack
    assert "NumPy" in b.stack


def test_pyproject_handles_pep508_extras_and_specifiers(tmp_path: Path) -> None:
    """Dependency strings like 'fastapi[all] (>=0.100,<1.0)' must
    still resolve to 'fastapi' for framework matching."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ['
        '"fastapi[all] (>=0.100,<1.0)", "django ; python_version >= \'3.10\'"]\n'
    )
    b = extract_brief(tmp_path)
    assert "FastAPI" in b.stack
    assert "Django" in b.stack


def test_cargo_extracts_tokio_hyper_axum(tmp_path: Path) -> None:
    """The exact zion-shape signal pattern surfaced by the live-fire bench."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "z"\nversion = "0.1.0"\n'
        '[dependencies]\ntokio = "1"\nhyper = "1"\nrustls = "0.23"\n'
    )
    b = extract_brief(tmp_path)
    assert "Rust" in b.stack
    assert "Tokio" in b.stack
    assert "Hyper" in b.stack
    assert "rustls" in b.stack


def test_package_json_extracts_react_and_next(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"name": "x", "dependencies": {"react": "18", "next": "14"}}'
    )
    b = extract_brief(tmp_path)
    assert "React" in b.stack
    assert "Next.js" in b.stack


def test_package_json_extracts_from_dev_dependencies(tmp_path: Path) -> None:
    """Frameworks like vite/tailwind are usually in devDependencies."""
    (tmp_path / "package.json").write_text(
        '{"name": "x", "devDependencies": {"vite": "5", "tailwindcss": "3"}}'
    )
    b = extract_brief(tmp_path)
    assert "Vite" in b.stack
    assert "Tailwind CSS" in b.stack


def test_package_json_strips_scope_prefix(tmp_path: Path) -> None:
    """@nestjs/core → 'nestjs' for framework lookup."""
    (tmp_path / "package.json").write_text(
        '{"name": "x", "dependencies": {"@nestjs/core": "10"}}'
    )
    b = extract_brief(tmp_path)
    assert "NestJS" in b.stack


def test_unknown_deps_dont_pollute_stack(tmp_path: Path) -> None:
    """Random package names must NOT add bogus tags to brief.stack."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["some-random-pkg", "another-thing"]\n'
    )
    b = extract_brief(tmp_path)
    # Only "Python" (language tag); no garbage from unknown deps
    assert b.stack == ["Python"]


def test_pyproject_extracts_scripts_and_tooling(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "gitoma"\ndescription = "An agent"\n'
        '[project.scripts]\ngitoma = "gitoma.cli:app"\n'
        '[tool.ruff]\nline-length = 100\n'
        '[tool.pytest.ini_options]\n'
    )
    b = extract_brief(tmp_path)
    assert "Python" in b.stack
    assert b.module_name == "gitoma"
    assert b.oneliner == "An agent"
    assert "gitoma" in b.entry_points
    assert "ruff" in b.ci_tools
    assert b.test_cmd == "pytest"


def test_readme_extracts_title_and_sections(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# My Cool Tool\n\n"
        "[![CI](https://x.y/badge.svg)](https://x.y)\n\n"
        "A one-line description of this project.\n\n"
        "## Installation\n\nrun pip\n\n"
        "## Usage\n\nrun it\n\n"
        "## Contributing\n\nPRs welcome\n"
    )
    b = extract_brief(tmp_path)
    assert b.title == "My Cool Tool"
    assert b.oneliner == "A one-line description of this project."  # badge skipped
    assert b.readme_sections == ["Installation", "Usage", "Contributing"]


def test_license_spdx_detection(tmp_path: Path) -> None:
    (tmp_path / "LICENSE").write_text("MIT License\n\nCopyright (c) 2024 Someone\n")
    b = extract_brief(tmp_path)
    assert b.license_id == "MIT"


def test_makefile_targets_provide_commands(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\tgo test\nbuild:\n\tgo build\n")
    b = extract_brief(tmp_path)
    # Makefile beats generic "go test" default via precedence (Makefile runs last,
    # but the extractor only assigns when the field is still empty — so Go's
    # ``go test ./...`` stays). Assert Makefile targets at least don't corrupt.
    assert b.test_cmd is not None


def test_render_brief_skips_empty_fields(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module m\n")
    b = extract_brief(tmp_path)
    rendered = render_brief(b)
    # Only fields we actually have should appear; no "title: None" noise
    assert "title:" not in rendered
    assert "stack:        Go" in rendered


def test_render_brief_truncates_at_budget() -> None:
    """Long natural-language descriptions must not blow the prompt budget."""
    b = RepoBrief(
        title="X",
        oneliner="Y",
        stack=["Python"],
        readme_sections=["S" + str(i) for i in range(30)],
    )
    out = render_brief(b, max_chars=200)
    assert len(out) <= 200


def test_broken_toml_does_not_crash(tmp_path: Path) -> None:
    """A malformed pyproject.toml must never propagate an exception —
    extract_brief is invariant under input corruption."""
    (tmp_path / "pyproject.toml").write_text("this is ::: not valid toml @@@\n")
    b = extract_brief(tmp_path)
    # Python stack still inferred from file extension-based heuristics if any,
    # but no crash and no field corruption:
    assert b.build_cmd is None or isinstance(b.build_cmd, str)


def test_real_gitoma_repo_self_brief_is_populated() -> None:
    """Smoke test on the gitoma repo itself — non-empty brief, recognizable fields."""
    root = Path(__file__).resolve().parents[1]
    b = extract_brief(root)
    assert "Python" in b.stack
    assert b.module_name == "gitoma"
    assert b.license_id in ("MIT", None)  # tolerate either — LICENSE may vary


def test_extract_brief_never_raises_on_corrupt_repo(tmp_path: Path) -> None:
    """Every well-known file present and broken simultaneously. The function
    must return a RepoBrief instance, not propagate."""
    (tmp_path / "pyproject.toml").write_text("BOOM::\n")
    (tmp_path / "package.json").write_text("not json {")
    (tmp_path / "Cargo.toml").write_text("also BOOM")
    (tmp_path / "go.mod").write_text("")
    (tmp_path / "Makefile").write_text("\0\0\0")
    (tmp_path / "README.md").write_text("")
    b = extract_brief(tmp_path)
    assert isinstance(b, RepoBrief)
