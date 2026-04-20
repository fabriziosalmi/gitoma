"""Shared Rich console with Gitoma's custom dark theme."""

from rich.console import Console
from rich.theme import Theme

GITOMA_THEME = Theme(
    {
        # Base palette
        "primary": "bold #C084FC",       # violet-400
        "secondary": "bold #67E8F9",     # cyan-300
        "accent": "bold #F472B6",        # pink-400
        "muted": "#6B7280",              # gray-500
        "success": "bold #4ADE80",       # green-400
        "warning": "bold #FBBF24",       # amber-400
        "danger": "bold #F87171",        # red-400
        "info": "#818CF8",               # indigo-400
        # Semantic aliases
        "metric.pass": "bold #4ADE80",
        "metric.warn": "bold #FBBF24",
        "metric.fail": "bold #F87171",
        "metric.score": "#A5B4FC",
        "phase": "bold #C084FC",
        "commit": "#67E8F9",
        "pr": "bold #F472B6",
        "task.done": "bold #4ADE80",
        "task.current": "bold #FBBF24",
        "task.pending": "#6B7280",
        "task.failed": "bold #F87171",
        "heading": "bold #E2E8F0",
        "code": "#F1F5F9",
        "url": "underline #67E8F9",
        "dim": "#4B5563",
    }
)

console = Console(theme=GITOMA_THEME, highlight=True)

BANNER = r"""
[primary]
   _____ _ _                        
  / ____(_) |                       
 | |  __ _| |_ ___  _ __ ___   __ _ 
 | | |_ | | __/ _ \| '_ ` _ \ / _` |
 | |__| | | || (_) | | | | | | (_| |
  \_____|_|\__\___/|_| |_| |_|\__,_|
[/primary]"""

BANNER_SUBTITLE = "[muted]AI-powered GitHub repository improvement agent[/muted]"
