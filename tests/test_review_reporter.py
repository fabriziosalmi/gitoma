"""Regression guard for the Rich-theme/style bug in the review reporter.

The bug: ``Table(header_style="bold secondary")`` looked reasonable but
Rich parses style strings "modifier + tokens" where each token is
expected to be a built-in color (`red`, `#123456`, …) NOT a theme name.
A theme entry like ``secondary`` resolves only when used on its own
(``style="secondary"``). Combining ``bold`` + ``secondary`` made Rich
try to parse ``secondary`` as a color and raise ``MissingStyle`` —
which crashed ``gitoma review`` on any PR with reviews or comments.

These tests exercise the render path end-to-end against the real Rich
theme so the bug can't silently come back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gitoma.review import reporter as reporter_module


@dataclass
class _FakeComment:
    id: int = 1
    author: str = "copilot"
    body: str = "nit: consider renaming"
    path: str = "src/x.py"
    line: int | None = 12
    url: str = "https://github.com/o/r/pull/5#comment-1"


@dataclass
class _FakeReviewStatus:
    pr_number: int
    pr_url: str
    reviews: list[dict[str, Any]]
    all_comments: list[_FakeComment]

    @property
    def total_comments(self) -> int:
        return len(self.all_comments)

    @property
    def copilot_comments(self) -> list[_FakeComment]:
        return [c for c in self.all_comments if c.author == "copilot"]


def test_reviews_table_renders_without_missing_style_error(capsys):
    status = _FakeReviewStatus(
        pr_number=5,
        pr_url="https://github.com/o/r/pull/5",
        reviews=[
            {"user": "copilot", "state": "COMMENTED", "body": "looks fine"},
            {"user": "fabriziosalmi", "state": "APPROVED", "body": ""},
        ],
        all_comments=[_FakeComment()],
    )
    # Must NOT raise rich.errors.MissingStyle. The regression it guards is:
    #   rich.errors.MissingStyle: Failed to get style 'bold secondary';
    #   unable to parse 'secondary' as color
    reporter_module.display_review_status(status)  # type: ignore[arg-type]
    # Capsys flushes rich's output — any render error would have surfaced above.
    capsys.readouterr()


def test_empty_status_renders_no_tables(capsys):
    status = _FakeReviewStatus(
        pr_number=5, pr_url="https://github.com/o/r/pull/5",
        reviews=[], all_comments=[],
    )
    reporter_module.display_review_status(status)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "No review comments yet" in out


def test_console_theme_styles_that_are_already_bold_must_not_be_used_with_bold_prefix():
    """Static guard: any theme entry that bakes in ``bold`` must not be
    used as ``"bold <name>"`` anywhere, because Rich would try to parse
    ``<name>`` as a color and fail at render time.

    Enumerates the gitoma theme, finds entries starting with ``bold``,
    then scans the source for ``"bold <name>"`` occurrences.
    """
    import pathlib
    import re

    from gitoma.ui.console import GITOMA_THEME

    bold_names = [
        name for name, style in GITOMA_THEME.styles.items()
        if str(style).startswith("bold ")
    ]
    assert bold_names, "theme introspection broken — no bold entries found"

    gitoma_root = pathlib.Path(__file__).resolve().parent.parent / "gitoma"
    offenders: list[str] = []
    for py in gitoma_root.rglob("*.py"):
        src = py.read_text()
        for name in bold_names:
            if re.search(rf'"bold {re.escape(name)}"', src):
                offenders.append(f"{py.relative_to(gitoma_root.parent)}: \"bold {name}\"")
    assert not offenders, (
        "Theme-bold-name anti-pattern found (Rich will fail to parse "
        "these at render time):\n  " + "\n  ".join(offenders)
    )
