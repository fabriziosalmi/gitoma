"""Multi-persona critic panel (M7).

Runs after ``patcher.apply_patches`` and BEFORE ``committer.commit_patches``
during a WORK subtask. Inspects the diff that's about to be committed and
emits structured findings (severity + category + lines), so the agent has
adversarial signal *inside the same run* — not just after the fact via
self-review or CI.

Walking skeleton, iteration 1: only the ``dev`` persona, advisory mode.
Devil's advocate, refinement turn, and meta-eval will land in subsequent
iterations once the baseline numbers tell us if the design holds up.

See gitoma/core/config.py::CriticPanelConfig for the kill-switch and
tests/fixtures/slop_audit_b2v_pr10.json for the regression baseline.
"""

from gitoma.critic.devil import DevilsAdvocate
from gitoma.critic.meta import MetaEval
from gitoma.critic.panel import CriticPanel
from gitoma.critic.refiner import Refiner
from gitoma.critic.types import Finding, PanelResult

__all__ = [
    "CriticPanel",
    "DevilsAdvocate",
    "Finding",
    "MetaEval",
    "PanelResult",
    "Refiner",
]
