"""Tests for the reviewer-ensemble config plumbing (2026-05-02).

Covers ``LMStudioConfig.parsed_review_base_urls()`` /
``parsed_review_models()`` / ``is_review_ensemble()`` plus the
``LM_STUDIO_REVIEW_BASE_URLS`` / ``LM_STUDIO_REVIEW_MODELS`` env-var
plumbing through ``load_config``.
"""

from __future__ import annotations

import os
from unittest.mock import patch

from gitoma.core.config import LMStudioConfig, load_config


def test_parsed_review_base_urls_splits_and_strips():
    cfg = LMStudioConfig(review_base_urls="http://a:1/v1, http://b:2/v1 ,http://c:3/v1")
    assert cfg.parsed_review_base_urls() == [
        "http://a:1/v1",
        "http://b:2/v1",
        "http://c:3/v1",
    ]


def test_parsed_review_models_splits_and_strips():
    cfg = LMStudioConfig(review_models=" m1 ,m2,  m3 ")
    assert cfg.parsed_review_models() == ["m1", "m2", "m3"]


def test_parsed_returns_empty_when_unset():
    cfg = LMStudioConfig()
    assert cfg.parsed_review_base_urls() == []
    assert cfg.parsed_review_models() == []


def test_parsed_skips_empty_segments():
    """Trailing commas + double commas must not yield empty entries."""
    cfg = LMStudioConfig(review_base_urls="a,,,b,")
    assert cfg.parsed_review_base_urls() == ["a", "b"]


def test_is_review_ensemble_requires_both_lists():
    cfg = LMStudioConfig(review_base_urls="a,b,c", review_models="")
    assert cfg.is_review_ensemble() is False
    cfg = LMStudioConfig(review_base_urls="", review_models="m1,m2,m3")
    assert cfg.is_review_ensemble() is False


def test_is_review_ensemble_requires_matching_lengths():
    cfg = LMStudioConfig(review_base_urls="a,b,c", review_models="m1,m2")
    assert cfg.is_review_ensemble() is False


def test_is_review_ensemble_requires_at_least_two_members():
    """A single (url, model) pair is just the solo path with extra
    syntax — not an ensemble. Avoids confusing operators."""
    cfg = LMStudioConfig(review_base_urls="a", review_models="m1")
    assert cfg.is_review_ensemble() is False


def test_is_review_ensemble_true_when_balanced_and_two_plus():
    cfg = LMStudioConfig(
        review_base_urls="http://a:1/v1,http://b:2/v1",
        review_models="m1,m2",
    )
    assert cfg.is_review_ensemble() is True

    cfg3 = LMStudioConfig(
        review_base_urls="a,b,c",
        review_models="m1,m2,m3",
    )
    assert cfg3.is_review_ensemble() is True


def test_load_config_picks_up_plural_envs(tmp_path, monkeypatch):
    """``LM_STUDIO_REVIEW_BASE_URLS`` / ``LM_STUDIO_REVIEW_MODELS`` /
    ``LM_STUDIO_REVIEW_ENSEMBLE_MIN_AGREE`` plumb through to the
    LMStudioConfig fields."""
    monkeypatch.setenv("LM_STUDIO_REVIEW_BASE_URLS", "u1,u2,u3")
    monkeypatch.setenv("LM_STUDIO_REVIEW_MODELS", "m1,m2,m3")
    monkeypatch.setenv("LM_STUDIO_REVIEW_ENSEMBLE_MIN_AGREE", "3")
    # Point HOME at an empty tmp dir so we don't accidentally read the
    # real ~/.gitoma/.env / config.toml during the test run.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    with patch("gitoma.core.config.GITOMA_DIR", tmp_path / ".gitoma"), \
         patch("gitoma.core.config.CONFIG_FILE", tmp_path / ".gitoma" / "config.toml"), \
         patch("gitoma.core.config.ENV_FILE", tmp_path / ".gitoma" / ".env"):
        cfg = load_config()
    assert cfg.lmstudio.review_base_urls == "u1,u2,u3"
    assert cfg.lmstudio.review_models == "m1,m2,m3"
    assert cfg.lmstudio.review_ensemble_min_agree == 3
    assert cfg.lmstudio.is_review_ensemble() is True
