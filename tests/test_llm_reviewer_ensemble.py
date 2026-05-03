"""Reviewer-ENSEMBLE LLMClient factory tests (2026-05-02).

Pins ``LLMClient.for_reviewer_ensemble`` + the per-instance
``base_url_override`` / ``model_override`` overrides that let
ensemble members each carry their own (endpoint, model) while
sharing role="reviewer" anti-thinking + max_tokens lookups.
"""

from __future__ import annotations

import pytest

from gitoma.core.config import Config, LMStudioConfig
from gitoma.planner.llm_client import LLMClient


@pytest.fixture
def base_config() -> Config:
    cfg = Config()
    cfg.lmstudio = LMStudioConfig(
        base_url="http://planner:1234/v1",
        model="qwen/qwen3-8b",
        api_key="lm-studio",
    )
    return cfg


def test_for_reviewer_ensemble_returns_empty_when_unconfigured(base_config: Config) -> None:
    assert LLMClient.for_reviewer_ensemble(base_config) == []


def test_for_reviewer_ensemble_returns_empty_when_lengths_mismatch(base_config: Config) -> None:
    base_config.lmstudio.review_base_urls = "u1,u2,u3"
    base_config.lmstudio.review_models = "m1,m2"  # mismatch
    assert LLMClient.for_reviewer_ensemble(base_config) == []


def test_for_reviewer_ensemble_returns_empty_for_single_member(base_config: Config) -> None:
    """N=1 is the solo path — ensemble must require ≥2 members."""
    base_config.lmstudio.review_base_urls = "http://localhost:1234/v1"
    base_config.lmstudio.review_models = "qwen/qwen3-8b"
    assert LLMClient.for_reviewer_ensemble(base_config) == []


def test_for_reviewer_ensemble_builds_n_clients(base_config: Config) -> None:
    base_config.lmstudio.review_base_urls = (
        "http://localhost:1234/v1,http://mm1:1234/v1,http://mm2:1234/v1"
    )
    base_config.lmstudio.review_models = (
        "qwen/qwen3-8b,qwen/qwen3-8b,qwen/qwen3.5-9b"
    )
    members = LLMClient.for_reviewer_ensemble(base_config)
    assert len(members) == 3
    assert [c.role for c in members] == ["reviewer", "reviewer", "reviewer"]
    # Each member carries its own endpoint + model.
    assert members[0]._resolve_base_url() == "http://localhost:1234/v1"
    assert members[1]._resolve_base_url() == "http://mm1:1234/v1"
    assert members[2]._resolve_base_url() == "http://mm2:1234/v1"
    assert members[0].model == "qwen/qwen3-8b"
    assert members[2].model == "qwen/qwen3.5-9b"


def test_ensemble_members_override_review_base_url_singular(base_config: Config) -> None:
    """If both singular and plural are set, the per-instance override
    on each ensemble member takes precedence over the singular
    review_base_url / review_model — the plurals win for ensemble
    members. Singular is left intact for the solo fallback path."""
    base_config.lmstudio.review_base_url = "http://solo:1234/v1"
    base_config.lmstudio.review_model = "solo-model"
    base_config.lmstudio.review_base_urls = "http://m1:1234/v1,http://m2:1234/v1"
    base_config.lmstudio.review_models = "ens-A,ens-B"
    members = LLMClient.for_reviewer_ensemble(base_config)
    assert len(members) == 2
    assert members[0]._resolve_base_url() == "http://m1:1234/v1"
    assert members[0].model == "ens-A"


def test_for_reviewer_solo_unaffected_when_only_solo_set(base_config: Config) -> None:
    """Existing solo-reviewer path must keep behaving exactly as before."""
    base_config.lmstudio.review_base_url = "http://solo:1234/v1"
    base_config.lmstudio.review_model = "solo-model"
    client = LLMClient.for_reviewer(base_config)
    assert client.role == "reviewer"
    assert client.model == "solo-model"
    assert client._resolve_base_url() == "http://solo:1234/v1"


def test_base_url_override_kwarg_explicit(base_config: Config) -> None:
    """The override kwargs work standalone too — useful for tests
    or future routing experiments without touching config."""
    client = LLMClient(
        base_config,
        role="reviewer",
        base_url_override="http://override:9999/v1",
        model_override="custom-x",
    )
    assert client._resolve_base_url() == "http://override:9999/v1"
    assert client.model == "custom-x"
