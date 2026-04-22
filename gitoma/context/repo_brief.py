"""Deterministic repo-wide brief extraction.

Produces a tight, structured summary of a repository (title, stack,
build/test/install commands, CI entrypoints, etc.) that is computed
ONCE at audit time and attached to every downstream agent prompt.

Philosophy — stolen from our own design lessons:
  * Deterministic first. Regex + tomllib + yaml.safe_load on
    well-known files. No LLM involvement for the common path.
  * Soft fields. Any field that cannot be extracted is ``None`` /
    ``[]`` — never guessed. A consumer reading a ``None`` field
    knows to fall back to its own heuristics (or to skip).
  * Compact rendering. ``render_brief()`` produces 5-10 lines of
    dense prose suitable to prepend to any prompt without
    blowing the token budget.

Files consulted:
  - README.md / README.rst (title, one-liner, section list)
  - pyproject.toml ([project.name, .description, .scripts], build deps)
  - package.json (name, description, scripts, main/bin)
  - Cargo.toml ([package], [bin])
  - go.mod (module name, go version)
  - Makefile (targets → likely build/test commands)
  - .github/workflows/*.yml (CI entrypoints + tools invoked)
  - Dockerfile (base image, CMD)
  - LICENSE (SPDX identifier)

Non-goals today:
  * LLM fallback for natural-language fields (optional, deferred).
  * docs/ deep scan (README + CI cover the 80% value).
  * Runtime introspection (importing the project to find entrypoints).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class RepoBrief:
    """Deterministic project summary — every field is either parsed
    from a well-known file or left ``None`` / empty."""

    title: str | None = None
    oneliner: str | None = None
    stack: list[str] = field(default_factory=list)           # ["Python", "FastAPI"]
    build_cmd: str | None = None
    test_cmd: str | None = None
    install_cmd: str | None = None
    ci_entrypoints: list[str] = field(default_factory=list)  # [".github/workflows/ci.yml"]
    ci_tools: list[str] = field(default_factory=list)        # ["ruff", "pytest", "cargo test"]
    readme_sections: list[str] = field(default_factory=list) # ["Installation", "Usage"]
    license_id: str | None = None                             # "MIT", "Apache-2.0"
    entry_points: list[str] = field(default_factory=list)    # CLI binaries
    module_name: str | None = None                            # Go/Rust/Python package name

    def to_dict(self) -> dict:
        return asdict(self)


# ── Deterministic extractors ────────────────────────────────────────────────

_README_CANDIDATES = ("README.md", "README.rst", "README.txt", "Readme.md", "readme.md")
_LICENSE_SPDX_MAP = (
    ("MIT", r"\bMIT License\b"),
    ("Apache-2.0", r"\bApache License,?\s+Version\s+2\.0\b"),
    ("BSD-3-Clause", r"\bBSD 3-Clause\b|Redistributions of source code must retain"),
    ("GPL-3.0", r"\bGNU GENERAL PUBLIC LICENSE[\s\S]{0,200}Version 3\b"),
    ("ISC", r"\bISC License\b"),
    ("MPL-2.0", r"\bMozilla Public License,?\s+v\.?\s+2\.0\b"),
)


def _read(p: Path) -> str | None:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _first_existing(root: Path, names: tuple[str, ...]) -> Path | None:
    for n in names:
        p = root / n
        if p.is_file():
            return p
    return None


def _extract_readme(root: Path, brief: RepoBrief) -> None:
    readme_path = _first_existing(root, _README_CANDIDATES)
    if readme_path is None:
        return
    txt = _read(readme_path)
    if not txt:
        return

    # Title = first h1
    if m := re.search(r"^#\s+(.+?)\s*$", txt, re.MULTILINE):
        brief.title = m.group(1).strip()[:100]

    # One-liner: first non-empty, non-heading, non-badge line after title (up to 200 chars)
    lines = txt.splitlines()
    in_title = False
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            in_title = True
            continue
        if not in_title:
            continue
        # Skip badges and image-only lines
        if s.startswith(("![", "[![", "<img", "<p", "<div", "|")):
            continue
        brief.oneliner = re.sub(r"\s+", " ", s).strip()[:200]
        break

    # Section list: all h2 headings
    brief.readme_sections = [
        m.group(1).strip() for m in re.finditer(r"^##\s+(.+?)\s*$", txt, re.MULTILINE)
    ][:12]


def _extract_pyproject(root: Path, brief: RepoBrief) -> None:
    p = root / "pyproject.toml"
    if not p.is_file():
        return
    try:
        import tomllib
    except ImportError:  # py<3.11
        return
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return

    project = data.get("project") or {}
    if not brief.module_name and isinstance(project.get("name"), str):
        brief.module_name = project["name"]
    if not brief.oneliner and isinstance(project.get("description"), str):
        brief.oneliner = project["description"][:200]

    brief.stack.append("Python")
    if "ruff" in (data.get("tool") or {}):
        brief.ci_tools.append("ruff")
    if "mypy" in (data.get("tool") or {}):
        brief.ci_tools.append("mypy")
    if "pytest" in (data.get("tool") or {}):
        brief.ci_tools.append("pytest")
    if "poetry" in (data.get("tool") or {}):
        brief.install_cmd = brief.install_cmd or "poetry install"
    else:
        # Default for PEP-621 pyproject
        brief.install_cmd = brief.install_cmd or "pip install -e ."

    # Scripts → entry points
    scripts = project.get("scripts") or {}
    if isinstance(scripts, dict):
        brief.entry_points.extend(scripts.keys())

    # Test command: if pytest is configured
    if "pytest" in brief.ci_tools:
        brief.test_cmd = brief.test_cmd or "pytest"


def _extract_package_json(root: Path, brief: RepoBrief) -> None:
    p = root / "package.json"
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return

    if not brief.module_name and isinstance(data.get("name"), str):
        brief.module_name = data["name"]
    if not brief.oneliner and isinstance(data.get("description"), str):
        brief.oneliner = data["description"][:200]

    # Detect TS vs JS via typescript in devDeps
    has_ts = "typescript" in (data.get("devDependencies") or {}) or (
        root / "tsconfig.json"
    ).is_file()
    brief.stack.append("TypeScript" if has_ts else "JavaScript")

    scripts = data.get("scripts") or {}
    if isinstance(scripts, dict):
        if "test" in scripts:
            brief.test_cmd = brief.test_cmd or "npm test"
        if "build" in scripts:
            brief.build_cmd = brief.build_cmd or "npm run build"
        if "start" in scripts or "dev" in scripts:
            brief.install_cmd = brief.install_cmd or "npm install"

    # bin entries
    bin_entries = data.get("bin")
    if isinstance(bin_entries, dict):
        brief.entry_points.extend(bin_entries.keys())
    elif isinstance(bin_entries, str) and data.get("name"):
        brief.entry_points.append(data["name"])


def _extract_cargo(root: Path, brief: RepoBrief) -> None:
    p = root / "Cargo.toml"
    if not p.is_file():
        return
    try:
        import tomllib
    except ImportError:
        return
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return
    package = data.get("package") or {}
    if not brief.module_name and isinstance(package.get("name"), str):
        brief.module_name = package["name"]
    if not brief.oneliner and isinstance(package.get("description"), str):
        brief.oneliner = package["description"][:200]
    brief.stack.append("Rust")
    brief.build_cmd = brief.build_cmd or "cargo build"
    brief.test_cmd = brief.test_cmd or "cargo test"
    brief.install_cmd = brief.install_cmd or "cargo build --release"
    for b in data.get("bin", []) or []:
        if isinstance(b, dict) and "name" in b:
            brief.entry_points.append(b["name"])


def _extract_go_mod(root: Path, brief: RepoBrief) -> None:
    p = root / "go.mod"
    if not p.is_file():
        return
    txt = _read(p)
    if not txt:
        return
    brief.stack.append("Go")
    if not brief.module_name:
        m = re.search(r"^module\s+(\S+)", txt, re.MULTILINE)
        if m:
            brief.module_name = m.group(1)
    brief.build_cmd = brief.build_cmd or "go build ./..."
    brief.test_cmd = brief.test_cmd or "go test ./..."


def _extract_makefile(root: Path, brief: RepoBrief) -> None:
    p = root / "Makefile"
    if not p.is_file():
        return
    txt = _read(p)
    if not txt:
        return
    targets = [
        m.group(1)
        for m in re.finditer(r"^([a-zA-Z][\w-]*):", txt, re.MULTILINE)
    ]
    # Heuristic: if a `test` target exists, prefer `make test` as test_cmd
    if "test" in targets and not brief.test_cmd:
        brief.test_cmd = "make test"
    if "build" in targets and not brief.build_cmd:
        brief.build_cmd = "make build"
    if "install" in targets and not brief.install_cmd:
        brief.install_cmd = "make install"


def _extract_ci(root: Path, brief: RepoBrief) -> None:
    wf_dir = root / ".github" / "workflows"
    if not wf_dir.is_dir():
        return
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        yaml = None  # type: ignore[assignment]

    for wf in sorted(wf_dir.glob("*.y*ml"))[:8]:
        rel = str(wf.relative_to(root))
        brief.ci_entrypoints.append(rel)
        txt = _read(wf)
        if not txt:
            continue
        # If pyyaml is available, parse properly
        if yaml is not None:
            try:
                data = yaml.safe_load(txt) or {}
            except Exception:
                data = {}
            for job in (data.get("jobs") or {}).values():
                for step in (job.get("steps") or []):
                    run = step.get("run") or ""
                    _collect_ci_tools(run, brief)
        else:
            _collect_ci_tools(txt, brief)
    brief.ci_tools = sorted(set(brief.ci_tools))


def _collect_ci_tools(text: str, brief: RepoBrief) -> None:
    tools = [
        ("pytest", r"\bpytest\b"),
        ("ruff", r"\bruff\b"),
        ("mypy", r"\bmypy\b"),
        ("black", r"\bblack\b"),
        ("cargo test", r"\bcargo\s+test\b"),
        ("cargo check", r"\bcargo\s+check\b"),
        ("go test", r"\bgo\s+test\b"),
        ("go vet", r"\bgo\s+vet\b"),
        ("npm test", r"\bnpm\s+test\b"),
        ("npm run build", r"\bnpm\s+run\s+build\b"),
        ("tsc", r"\btsc\b"),
        ("eslint", r"\beslint\b"),
        ("prettier", r"\bprettier\b"),
        ("docker", r"\bdocker\s+(build|run|compose)\b"),
    ]
    for name, pat in tools:
        if re.search(pat, text) and name not in brief.ci_tools:
            brief.ci_tools.append(name)


def _extract_license(root: Path, brief: RepoBrief) -> None:
    for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING"):
        p = root / name
        if p.is_file():
            txt = _read(p)
            if not txt:
                continue
            for spdx, pat in _LICENSE_SPDX_MAP:
                if re.search(pat, txt, re.IGNORECASE):
                    brief.license_id = spdx
                    return


def extract_brief(root: Path) -> RepoBrief:
    """Parse well-known files under ``root`` and return a RepoBrief.
    Never raises — unparseable files produce empty fields, not errors."""
    brief = RepoBrief()
    for fn in (
        _extract_readme,
        _extract_pyproject,
        _extract_package_json,
        _extract_cargo,
        _extract_go_mod,
        _extract_makefile,
        _extract_ci,
        _extract_license,
    ):
        try:
            fn(root, brief)
        except Exception:
            # One broken extractor should never kill the whole brief.
            continue
    # Dedup + order
    seen: set[str] = set()
    brief.stack = [s for s in brief.stack if not (s in seen or seen.add(s))]
    return brief


# ── Rendering ──────────────────────────────────────────────────────────────


def render_brief(brief: RepoBrief, max_chars: int = 900) -> str:
    """Compact 5-10-line summary suitable for prompt injection.

    Omits ``None`` / empty fields silently — the output carries only
    what we actually know, so a sparse repo produces a sparse brief
    rather than a list of 'unknown' noise.
    """
    lines: list[str] = ["== REPO BRIEF (deterministic — trust these fields) =="]
    if brief.title:
        lines.append(f"title:        {brief.title}")
    if brief.oneliner:
        lines.append(f"oneliner:     {brief.oneliner}")
    if brief.stack:
        lines.append(f"stack:        {', '.join(brief.stack)}")
    if brief.module_name:
        lines.append(f"module:       {brief.module_name}")
    if brief.install_cmd:
        lines.append(f"install_cmd:  {brief.install_cmd}")
    if brief.build_cmd:
        lines.append(f"build_cmd:    {brief.build_cmd}")
    if brief.test_cmd:
        lines.append(f"test_cmd:     {brief.test_cmd}")
    if brief.ci_entrypoints:
        lines.append(f"ci_entry:     {', '.join(brief.ci_entrypoints[:3])}")
    if brief.ci_tools:
        lines.append(f"ci_tools:     {', '.join(brief.ci_tools[:6])}")
    if brief.readme_sections:
        lines.append(f"sections:     {', '.join(brief.readme_sections[:6])}")
    if brief.entry_points:
        lines.append(f"entry_points: {', '.join(brief.entry_points[:4])}")
    if brief.license_id:
        lines.append(f"license:      {brief.license_id}")

    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 16] + "… (truncated)"
    return out
