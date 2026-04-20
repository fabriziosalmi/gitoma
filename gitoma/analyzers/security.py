"""Security analyzer — hardcoded secrets, security policy."""

from __future__ import annotations

import re

from gitoma.analyzers.base import BaseAnalyzer, MetricResult

# Patterns that suggest hardcoded secrets
SECRET_PATTERNS = [
    (re.compile(r'(?i)(api[_-]?key|apikey)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{20,}'), "API key"),
    (re.compile(r'(?i)(secret[_-]?key|secret)\s*[=:]\s*["\']?[A-Za-z0-9_\-]{16,}'), "Secret"),
    (re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["\']?.{6,}'), "Password"),
    (re.compile(r'ghp_[A-Za-z0-9]{36}'), "GitHub PAT"),
    (re.compile(r'sk-[A-Za-z0-9]{32,}'), "OpenAI key"),
    (re.compile(r'AKIA[0-9A-Z]{16}'), "AWS Access Key"),
    (re.compile(r'(?i)bearer\s+[A-Za-z0-9\-._~+/]{20,}'), "Bearer token"),
]

IGNORE_EXTS = {".lock", ".sum", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2"}
MAX_FILE_SIZE = 512 * 1024  # 512 KB


class SecurityAnalyzer(BaseAnalyzer):
    name = "security"
    display_name = "Security"
    weight = 1.6

    def analyze(self) -> MetricResult:
        score = 0.0
        suggestions: list[str] = []
        findings: list[str] = []

        # ── Secrets scan ───────────────────────────────────────────────────
        scanned = 0
        secret_hits: list[tuple[str, str]] = []  # (file, type)

        for path in self.root.rglob("*"):
            if path.is_dir() or ".git" in path.parts:
                continue
            if path.suffix.lower() in IGNORE_EXTS:
                continue
            if path.stat().st_size > MAX_FILE_SIZE:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                scanned += 1
                rel = str(path.relative_to(self.root))
                for pattern, label in SECRET_PATTERNS:
                    if pattern.search(content):
                        secret_hits.append((rel, label))
                        break
            except Exception:
                pass

        if secret_hits:
            score = 0.1
            for f, label in secret_hits[:3]:
                findings.append(f"Possible {label} in {f}")
            suggestions.append("Hardcoded secrets detected! Move to environment variables or a secrets manager")
            suggestions.append("Add .env to .gitignore and use python-dotenv / os.getenv")
        else:
            score += 0.45

        # ── .gitignore covers secrets ──────────────────────────────────────
        gitignore = self.read(".gitignore") or ""
        if ".env" in gitignore:
            score += 0.15
        else:
            suggestions.append("Add .env to .gitignore to prevent accidental secret commits")

        # ── SECURITY.md ────────────────────────────────────────────────────
        if self.file_exists("SECURITY.md", ".github/SECURITY.md"):
            score += 0.20
        else:
            suggestions.append(
                "Add SECURITY.md with a vulnerability disclosure policy"
            )

        # ── pip-audit / cargo-audit / govulncheck awareness ────────────────
        ci_content = ""
        gha_dir = self.root / ".github" / "workflows"
        if gha_dir.is_dir():
            for wf in gha_dir.glob("*.yml"):
                ci_content += wf.read_text(errors="replace").lower()

        if any(k in ci_content for k in ["pip-audit", "cargo-audit", "govulncheck", "npm audit", "trivy"]):
            score += 0.20
            findings.append("Dependency audit in CI")
        else:
            suggestions.append(
                "Add dependency audit to CI: pip-audit (Python), cargo-audit (Rust), "
                "govulncheck (Go), npm audit (JS)"
            )

        if findings:
            details = "; ".join(findings[:3])
        elif secret_hits:
            details = f"{len(secret_hits)} potential secret(s) found across {scanned} files"
        else:
            details = f"No obvious secrets detected in {scanned} files scanned"

        return MetricResult.from_score(
            self.name, self.display_name, min(score, 1.0), details, suggestions, self.weight
        )
