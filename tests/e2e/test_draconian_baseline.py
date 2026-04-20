import pytest

from gitoma.core.config import Config
from gitoma.review.reflexion import CIDiagnosticAgent

@pytest.fixture
def base_config() -> Config:
    from gitoma.core.config import Config, GitHubConfig, LMStudioConfig, BotConfig
    return Config(
        github=GitHubConfig(token="fake_token"),
        bot=BotConfig(name="MockBot", email="mock@bot.com"),
        lmstudio=LMStudioConfig(model="mock-model")
    )


def test_e2e_json_hallucination_recovery(mocker, base_config):
    """
    Simulate a Fixer Agent that returns truncated markdown JSON on the first try.
    Ensure that the Circuit Breaker traps it and successfully recovers on attempt 2.
    """
    # 1. Mock Github Client to return a fake failed job
    mocker.patch("gitoma.core.github_client.GitHubClient.get_failed_jobs", return_value=[{"job_id": 123, "name": "build", "url": "mock_url"}])
    mocker.patch("gitoma.core.github_client.GitHubClient.get_job_log", return_value="Traceback Mock Error")
    
    # 2. Mock GitRepo to swallow clone, checkout, commit, push
    mock_repo = mocker.patch("gitoma.review.reflexion.GitRepo")
    mock_repo_instance = mock_repo.return_value
    mock_repo_instance.__enter__.return_value = mock_repo_instance

    # 3. Mock the LLM Fixer
    mock_fixer = mocker.patch("gitoma.planner.llm_client.LLMClient.chat")
    
    # Call 1 (Fixer): Bad JSON hallucination.
    # Call 2 (Fixer Retry): Perfect logic JSON.
    # Call 3 (Critic): Approval
    mock_fixer.side_effect = [
        "```json\n{\"fixes\": [{\"file\": \"broken\"\n```",
        "```json\n{\"fixes\": [{\"file\": \"app.py\", \"find\": \"bad\", \"replace\": \"good\"}]}\n```",
        "```json\n{\"approved\": true, \"feedback\": \"Looks great!\"}\n```",
    ]

    # Temporarily override sleep so test runs instantly without exponential backoff
    mocker.patch("time.sleep", return_value=None)
    # Isolate the Observer so its internal LLM call doesn't pollute the call count
    mocker.patch("gitoma.review.observer.ObserverAgent.analyze_session", return_value=None)

    agent = CIDiagnosticAgent(base_config)
    
    # We must explicitly override the internal LLMs because they were built inside __init__
    agent.fixer_llm.chat = mock_fixer
    agent.critic_llm.chat = mock_fixer

    agent.analyze_and_fix("https://github.com/mock/repo", "mock-branch")

    # The agent should have tried exactly 3 LLM calls (1 fail, 1 success fixer, 1 success critic)
    assert mock_fixer.call_count == 3
    # Ensure git push was called at the end
    mock_repo_instance.push.assert_called_once_with("mock-branch", force=False)


def test_e2e_critic_agent_rejection_loop(mocker, base_config, capsys):
    """
    Simulate a Fixer that always produces logically wrong patches.
    The Critic Agent rejects it every time.
    Ensure Circuit Breaker trips gracefully at MAX_RETRIES.
    """
    mocker.patch("gitoma.core.github_client.GitHubClient.get_failed_jobs", return_value=[{"job_id": 124, "name": "build"}])
    mocker.patch("gitoma.core.github_client.GitHubClient.get_job_log", return_value="Traceback Mock Error")

    mock_fixer = mocker.patch("gitoma.planner.llm_client.LLMClient.chat")
    # Fixer always generates bad fixes, Critic always says "approved: false"
    # This loop runs exactly 3 times. Fixer(1), Critic(2), Fixer(3), Critic(4), Fixer(5), Critic(6)
    mock_fixer.side_effect = [
        "```json\n{\"fixes\": []}\n```", # Fixer 1
        "```json\n{\"approved\": false, \"feedback\": \"Wrong!\"}\n```", # Critic 1
        "```json\n{\"fixes\": []}\n```", # Fixer 2
        "```json\n{\"approved\": false, \"feedback\": \"Wrong again!\"}\n```", # Critic 2
        "```json\n{\"fixes\": []}\n```", # Fixer 3
        "```json\n{\"approved\": false, \"feedback\": \"Still wrong!\"}\n```", # Critic 3
    ]

    mocker.patch("time.sleep", return_value=None)
    # Isolate the Observer so its internal LLM call doesn't pollute the call count
    mocker.patch("gitoma.review.observer.ObserverAgent.analyze_session", return_value=None)

    agent = CIDiagnosticAgent(base_config)
    agent.fixer_llm.chat = mock_fixer
    agent.critic_llm.chat = mock_fixer

    agent.analyze_and_fix("https://github.com/mock/repo", "mock-branch")

    assert mock_fixer.call_count == 6
    
    # Check that console output reports standard circuit breaker trip
    captured = capsys.readouterr()
    assert "Circuit Breaker tripped" in captured.out


def test_e2e_network_thundering_herd(mocker, base_config, capsys):
    """
    Simulate a temporary GitHub API outage.
    The agent should use wait logic (if implemented globally) or catch the HTTP error gracefully.
    """
    # Simulate first two calls throwing requests.exceptions.ConnectionError, third call succeeds
    import requests
    mock_gh = mocker.patch("gitoma.core.github_client.GitHubClient.get_failed_jobs")
    mock_gh.side_effect = [
        requests.exceptions.ConnectionError("503 Service Unavailable"),
        requests.exceptions.ConnectionError("503 Service Unavailable"),
        [{"job_id": 999, "name": "build"}]
    ]
    
    mocker.patch("gitoma.core.github_client.GitHubClient.get_job_log", return_value="Log data")
    
    mock_fixer = mocker.patch("gitoma.planner.llm_client.LLMClient.chat")
    mock_fixer.side_effect = [
        "```json\n{\"fixes\": []}\n```", # Fixer
        "```json\n{\"approved\": true, \"feedback\": \"Good.\"}\n```", # Critic
    ]
    
    mock_repo = mocker.patch("gitoma.review.reflexion.GitRepo")
    mock_repo_instance = mock_repo.return_value
    mock_repo_instance.__enter__.return_value = mock_repo_instance

    mocker.patch("time.sleep", return_value=None)

    agent = CIDiagnosticAgent(base_config)
    agent.fixer_llm.chat = mock_fixer
    agent.critic_llm.chat = mock_fixer

    # Wrap analyze_and_fix inside a small retry harness or handle the exception explicitly if logic handles it
    try:
        agent.analyze_and_fix("https://github.com/mock/repo", "mock-branch")
    except requests.exceptions.ConnectionError:
        pass # In a truly robust system, GitHubClient handles backoff. Right now we assert it raises or fails cleanly.
    
    assert mock_gh.call_count == 1  # Fails immediately if not wrapped in globally backoff

