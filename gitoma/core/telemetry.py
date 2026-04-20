"""Telemetry subsystem to save AI agent interactions."""

import datetime
import json
from pathlib import Path
from typing import Any

from gitoma.core.config import GITOMA_DIR

TELEMETRY_DIR = GITOMA_DIR / "telemetry"

def save_telemetry_report(agent_type: str, session_data: dict[str, Any], report: str) -> Path:
    """
    Save a meta-cognitive report along with the raw session data for developer observation.
    Returns the file path.
    """
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = TELEMETRY_DIR / f"{agent_type}_{timestamp}.md"
    
    output = f"""# 👁️ Meta-Cognitive Observer Report

**Timestamp**: {datetime.datetime.now().isoformat()}
**Agent Pipeline**: {agent_type}
**Status**: {session_data.get('status', 'UNKNOWN')}

## 🧠 Diagnostic Advice from Observer

{report}

---

## 🔬 Raw Interaction Context

**CI Failure Logs**
```text
{session_data.get('ci_logs', 'N/A')}
```

**Fixer's Final Output**
```json
{json.dumps(session_data.get('fixer_raw', {}), indent=2) if isinstance(session_data.get('fixer_raw'), dict) else session_data.get('fixer_raw', '{}')}
```

**Critic's Final Verdict**
```json
{session_data.get('critic_raw', '{}')}
```
"""
    file_path.write_text(output)
    return file_path
