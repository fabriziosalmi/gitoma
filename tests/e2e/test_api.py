from fastapi.testclient import TestClient

from gitoma.api.server import app

client = TestClient(app)


def test_api_auth_missing_token(mocker):
    """
    Test that endpoints are secure against requests without headers.
    """
    mock_config = mocker.patch("gitoma.api.server.load_config")
    # Simulate a perfectly configured environment with an API Key
    mock_config.return_value.api_auth_token = "EXPECTED_TOKEN"

    response = client.get("/api/v1/health")
    assert response.status_code == 401
    assert response.json()["detail"] == "Not authenticated"


def test_api_auth_invalid_token(mocker):
    """
    Test that invalid Bearer tokens are gracefully rejected.
    """
    mock_config = mocker.patch("gitoma.api.server.load_config")
    mock_config.return_value.api_auth_token = "EXPECTED_TOKEN"

    headers = {"Authorization": "Bearer WRONG_TOKEN"}
    response = client.get("/api/v1/health", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid authentication token."


def test_api_auth_server_not_configured(mocker):
    """
    Test the strict fail-closed state where if the server admin forgot to
    setup an API check, all network requests are blocked manually.
    """
    mock_config = mocker.patch("gitoma.api.server.load_config")
    mock_config.return_value.api_auth_token = ""

    headers = {"Authorization": "Bearer SOME_TOKEN"}
    response = client.get("/api/v1/health", headers=headers)
    
    assert response.status_code == 500
    assert "not configured" in response.json()["detail"]


def test_api_health_endpoint(mocker):
    """
    Test successful health check.
    """
    mock_config = mocker.patch("gitoma.api.server.load_config")
    mock_config.return_value.api_auth_token = "SECRET_TOKEN"
    mock_config.return_value.github.token = "ghp_fake"
    
    # Mock LM Studio check
    from gitoma.planner.llm_client import HealthCheckResult, HealthLevel
    mocker.patch("gitoma.api.routers.check_lmstudio", return_value=HealthCheckResult(level=HealthLevel.OK, message="Mock message", available_models=["test-model"]))

    headers = {"Authorization": "Bearer SECRET_TOKEN"}
    response = client.get("/api/v1/health", headers=headers)
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["github_token_set"] is True
    assert "test-model" in data["lm_studio"]["available_models"]


def test_api_run_job_dispatch(mocker):
    """
    Test that trigger run successfully yields an async background task id
    and that the job status machinery tracks its lifecycle.
    """

    mock_config = mocker.patch("gitoma.api.server.load_config")
    mock_config.return_value.api_auth_token = "TOKEN"

    # Stub the actual CLI subprocess so the background task returns instantly.
    stub = mocker.MagicMock(returncode=0, stdout="ok", stderr="")
    mocker.patch("gitoma.api.routers.subprocess.run", return_value=stub)

    payload = {"repo_url": "https://github.com/mock/repo"}
    headers = {"Authorization": "Bearer TOKEN"}

    response = client.post("/api/v1/run", json=payload, headers=headers)
    assert response.status_code == 200

    data = response.json()
    assert "job_id" in data
    assert data["status"] == "started"

    job_id = data["job_id"]

    # Poll the job id status — the subprocess stub returns success immediately.
    status_resp = client.get(f"/api/v1/status/{job_id}", headers=headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] in ["running", "completed"]


def test_api_fix_ci_dispatch(mocker):
    """
    Test CI Agent launch through background task.
    """
    mock_config = mocker.patch("gitoma.api.server.load_config")
    mock_config.return_value.api_auth_token = "TOKEN"

    # Missing branch payload raises 400
    payload_bad = {"repo_url": "https://github.com/mock/repo"}
    headers = {"Authorization": "Bearer TOKEN"}
    resp_bad = client.post("/api/v1/fix-ci", json=payload_bad, headers=headers)
    assert resp_bad.status_code == 400

    # Good payload with mocked pipeline
    payload_good = {"repo_url": "https://github.com/mock/repo", "branch": "gitoma/test"}
    resp_good = client.post("/api/v1/fix-ci", json=payload_good, headers=headers)
    assert resp_good.status_code == 200
    assert "job_id" in resp_good.json()
