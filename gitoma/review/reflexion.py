"""CI Auto-Remediation with LLM Reflexion and Thundering Herd Protection."""

from __future__ import annotations

import json
import time
from typing import Any

from rich.console import Console

from gitoma.core.config import Config
from gitoma.core.github_client import GitHubClient
from gitoma.core.repo import GitRepo
from gitoma.planner.llm_client import LLMClient

console = Console()

class CircuitBreakerError(Exception):
    pass


class CIDiagnosticAgent:
    """An autonomous agent capable of resolving CI pipeline failures."""

    MAX_RETRIES = 3

    def __init__(self, config: Config) -> None:
        import copy
        self.config = config
        
        # Main Fixer uses default configured model (e.g. gemma)
        self.fixer_llm = LLMClient(config)
        
        # Critic Agent uses CRITIC_MODEL or falls back to main model
        critic_config = copy.deepcopy(config)
        critic_config.lmstudio.model = config.lmstudio.critic_model or config.lmstudio.model
        self.critic_llm = LLMClient(critic_config)
        
        # Observer Agent strictly watches and learns
        from gitoma.review.observer import ObserverAgent
        self.observer = ObserverAgent(config)
        
        self.gh = GitHubClient(config)
        self._last_session_data: dict[str, Any] = {}

    def analyze_and_fix(self, repo_url: str, branch: str) -> None:
        """Entrypoint for the CI fix loop."""
        owner, name = repo_url.replace("https://github.com/", "").split("/")
        
        console.print(f"[info]Searching for failed CI jobs on branch [bold]{branch}[/bold]...[/info]")
        
        failed_jobs = self.gh.get_failed_jobs(owner, name, branch)
        if not failed_jobs:
            console.print("[success]No failed CI jobs found![/success]")
            return
            
        for job in failed_jobs:
            console.print(f"\n[danger]✗ Job '{job['name']}' failed![/danger]")
            
            # Anti-thundering herd: limited fast-retries
            retries = 0
            success = False
            
            while retries < self.MAX_RETRIES and not success:
                try:
                    success = self._attempt_remediation(owner, name, branch, repo_url, job)
                except Exception as e:
                    console.print(f"[warning]Attempt {retries+1} failed during execution: {e}[/warning]")
                
                if not success:
                    retries += 1
                    if retries < self.MAX_RETRIES:
                        console.print(f"[muted]Backing off before retry... ({retries}/{self.MAX_RETRIES})[/muted]")
                        time.sleep(2 ** retries)  # Exponential backoff
                        
                        
            if not success:
                console.print(f"[danger]Circuit Breaker tripped for job {job['name']} after {self.MAX_RETRIES} attempts. Human intervention required.[/danger]")
                self._last_session_data["status"] = "BREAKER_TRIPPED"
                
            # Trigger Observer asynchronously or sequentially at the end
            if self._last_session_data:
                self.observer.analyze_session(self._last_session_data)


    def _attempt_remediation(self, owner: str, name: str, branch: str, repo_url: str, job: dict[str, Any]) -> bool:
        """Fetch logs, generate patch, run reflexion, and commit."""
        log_text = self.gh.get_job_log(owner, name, job["job_id"])
        if log_text.startswith("Could not fetch"):
            console.print(log_text)
            return False
            
        # ── 1. Fixer Agent ───────────────────────────────────────────────────
        console.print("[info]🧠 Fixer Agent analyzing failure logs...[/info]")
        patch_plan = self._generate_patch(log_text)
        
        self._last_session_data = {
            "ci_logs": log_text,
            "fixer_raw": patch_plan if patch_plan else "JSONDecodeError or None",
            "critic_raw": "N/A",
            "status": "FAILED_FIXER_JSON"
        }
        
        if not patch_plan:
            return False
            
        # ── 2. Reflexion Critic Agent ────────────────────────────────────────
        console.print("[info]🧐 Critic Agent verifying proposed fix...[/info]")
        approved, feedback = self._evaluate_patch(log_text, patch_plan)
        
        self._last_session_data["critic_raw"] = feedback
        self._last_session_data["status"] = "APPROVED" if approved else "REJECTED_BY_CRITIC"
        
        if not approved:
            console.print(f"[warning]Critic rejected the patch: {feedback}[/warning]")
            return False
            
        console.print("[success]Critic approved the patch! Applying...[/success]")
        
        # ── 3. Apply & Push ──────────────────────────────────────────────────
        r = GitRepo(repo_url, self.config)
        with r:
            r.clone()
            r.repo.git.checkout(branch)
            for fix in patch_plan.get("fixes", []):
                file_path = r.root / fix["file"]
                if file_path.exists():
                    current_content = file_path.read_text()
                    new_content = current_content.replace(fix["find"], fix["replace"])
                    file_path.write_text(new_content)
            
            r.stage_all()
            r.commit("fix(ci): auto-remediate pipeline failure", author_name=self.config.bot.name, author_email=self.config.bot.email)
            r.push(branch, force=False)
            
        return True


    def _generate_patch(self, logs: str) -> dict[str, Any] | None:
        prompt = f"""You are a senior DevOps engineer fixing a CI/CD pipeline failure.
Here are the final lines of the job log:

{logs}

Provide the precise modifications needed to fix this build.
Respond STRICTLY with a valid JSON document matching this schema:
{{
  "fixes": [
     {{"file": "path/to/file", "find": "exact text to replace", "replace": "new text"}}
  ]
}}"""
        resp = self.fixer_llm.chat([{"role": "user", "content": prompt}])
        try:
            cleaned = resp.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed: dict[str, Any] = json.loads(cleaned)
            return parsed
        except json.JSONDecodeError as e:
            console.print(f"[dim warning]Failed to parse Fixer JSON: {e}[/dim warning]")
            return None


    def _evaluate_patch(self, logs: str, patch: dict[str, Any]) -> tuple[bool, str]:
        prompt = f"""You are a strict code reviewer checking a proposed CI fix.
        
CI Logs:
{logs}

Proposed Patch:
{json.dumps(patch, indent=2)}

Determine if this patch correctly and safely addresses the CI failure. 
Respond STRICTLY with a valid JSON document matching this schema:
{{
  "approved": true/false,
  "feedback": "Reasoning here."
}}"""
        resp = self.critic_llm.chat([{"role": "user", "content": prompt}])
        try:
            cleaned = resp.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(cleaned)
            return data.get("approved", False), data.get("feedback", "No feedback provided")
        except json.JSONDecodeError as e:
            return False, f"Failed to parse critic response: {e}"
