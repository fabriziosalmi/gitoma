"""Meta-Cognitive Observer Agent for Architectural Self-Reflection."""

from __future__ import annotations

from rich.console import Console

from gitoma.core.config import Config
from gitoma.core.telemetry import save_telemetry_report
from gitoma.planner.llm_client import LLMClient

console = Console()

class ObserverAgent:
    """Watches the pipeline flow and generates optimization advice for the developers."""

    def __init__(self, config: Config) -> None:
        self.config = config
        # Use the primary LLM
        self.llm = LLMClient(config)

    def analyze_session(self, session_data: dict) -> None:
        """
        Takes raw interactions from the pipeline and generates an architectural review.
        """
        console.print("[dim info]👁️  Observer Agent compiling Meta-Cognitive Telemetry...[/dim info]")
        
        prompt = f"""You are a Meta-Cognitive AI Architect reviewing an autonomous coding agent loop.
We have a "Fixer Agent" that tries to resolve CI errors, and a "Critic Agent" that approves or rejects its code.

Here is the context of the recent execution:
---
CI logs:
{session_data.get('ci_logs', 'N/A')}

Fixer's Generated Output:
{session_data.get('fixer_raw', 'N/A')}

Critic's Final Output:
{session_data.get('critic_raw', 'N/A')}

Final State of this Job: {session_data.get('status', 'UNKNOWN')}
---

Your goal is to provide EXCLUSIVELY advice to human developers on how to improve this AI system.
1. Why did the Fixer fail or succeed? (e.g. lack of context, bad prompt parsing, good reasoning).
2. Was the Critic's logic sound or overly pedantic?
3. Suggest 2-3 concrete prompt engineering or architectural tweaks.

Do not write code for the repo. Output a markdown formatted analysis report.
"""
        try:
            report = self.llm.chat([{"role": "system", "content": "You are a senior Meta-Cognitive AI Systems Architect."},
                                    {"role": "user", "content": prompt}])
            
            filepath = save_telemetry_report("CI_Reflexion", session_data, report)
            console.print(f"[success]👁️  Meta-Cognitive Telemetry saved: {filepath}[/success]")
            
        except Exception as e:
            console.print(f"[dim warning]Observer failed to generate telemetry: {e}[/dim warning]")
