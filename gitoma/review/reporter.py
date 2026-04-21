"""Rich terminal reporter for review status display."""

from __future__ import annotations

from typing import Any

from rich.table import Table
from rich.panel import Panel
from rich import box

from gitoma.review.watcher import ReviewStatus
from gitoma.ui.console import console


def display_review_status(status: ReviewStatus) -> None:
    """Render a full review status report in the terminal."""

    if status.total_comments == 0:
        console.print(
            Panel(
                "[muted]No review comments yet. Copilot may still be processing.[/muted]\n"
                f"[dim]PR #{status.pr_number} — {status.pr_url}[/dim]",
                title="[secondary]🔍 Review Status[/secondary]",
                border_style="secondary",
            )
        )
        return

    # Reviews summary
    if status.reviews:
        console.print()
        _display_reviews_table(status.reviews)

    # Comments table
    if status.all_comments:
        console.print()
        _display_comments_table(status.all_comments)

    # Copilot highlight
    copilot = status.copilot_comments
    if copilot:
        console.print()
        console.print(
            Panel(
                f"[warning]🤖 {len(copilot)} Copilot comment(s) detected.[/warning]\n"
                "Run [primary]gitoma review --integrate[/primary] to auto-fix them.",
                title="[accent]✨ Copilot Comments[/accent]",
                border_style="accent",
            )
        )


def _display_reviews_table(reviews: list[dict[str, Any]]) -> None:
    table = Table(
        title="📋 PR Reviews",
        box=box.ROUNDED,
        border_style="secondary",
        show_header=True,
        header_style="secondary",
        title_style="heading",
    )
    table.add_column("Reviewer", style="primary")
    table.add_column("State", justify="center")
    table.add_column("Summary", style="muted")

    state_map = {
        "APPROVED": "[success]✅ APPROVED[/success]",
        "CHANGES_REQUESTED": "[danger]❌ CHANGES REQUESTED[/danger]",
        "COMMENTED": "[warning]💬 COMMENTED[/warning]",
        "PENDING": "[muted]⏳ PENDING[/muted]",
        "DISMISSED": "[muted]🚫 DISMISSED[/muted]",
    }

    for r in reviews:
        state_label = state_map.get(r["state"], r["state"])
        body = (r.get("body") or "")[:80]
        table.add_row(r["user"], state_label, body or "[dim](no body)[/dim]")

    console.print(table)


def _display_comments_table(comments: list[Any]) -> None:
    table = Table(
        title=f"💬 Review Comments ({len(comments)})",
        box=box.ROUNDED,
        border_style="primary",
        show_header=True,
        header_style="primary",
        title_style="heading",
    )
    table.add_column("#", style="muted", width=4)
    table.add_column("Author", style="accent", width=18)
    table.add_column("File", style="code", width=30)
    table.add_column("Comment", style="info", no_wrap=False)

    for i, c in enumerate(comments, 1):
        author = c.author
        if "copilot" in author.lower():
            author = f"[warning]🤖 {author}[/warning]"

        file_ref = (c.path or "[dim]general[/dim]")[:28]
        if c.line:
            file_ref += f"[dim]:{c.line}[/dim]"

        body = c.body[:150].replace("\n", " ")
        if len(c.body) > 150:
            body += "…"

        table.add_row(str(i), author, file_ref, body)

    console.print(table)
