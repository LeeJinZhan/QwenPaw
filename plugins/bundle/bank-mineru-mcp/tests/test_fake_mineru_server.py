from __future__ import annotations

from fastapi.testclient import TestClient

from fake_mineru_server import create_app


def test_fake_server_supports_file_parse_and_tasks_without_content_in_stats() -> None:
    client = TestClient(create_app(expected_token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    files = {"files": ("file_file_001.pdf", b"%PDF fake", "application/pdf")}

    health = client.get("/health", headers=headers)
    direct = client.post("/file_parse", headers=headers, files=files)
    submitted = client.post("/tasks", headers=headers, files=files)
    task_id = submitted.json()["task_id"]
    status = client.get(f"/tasks/{task_id}", headers=headers)
    result = client.get(f"/tasks/{task_id}/result", headers=headers)
    stats = client.get("/_stats", headers=headers).json()

    assert health.json()["status"] == "healthy"
    assert direct.json()["results"]["file_file_001"]["page_count"] == 12
    assert status.json()["status"] == "completed"
    assert result.json()["results"]["file_file_001"]["page_count"] == 12
    assert stats == {
        "request_count": 5,
        "request_paths": [
            "/health",
            "/file_parse",
            "/tasks",
            f"/tasks/{task_id}",
            f"/tasks/{task_id}/result",
        ],
        "task_count": 1,
    }
    assert "Fake MinerU result" not in str(stats)


def test_fake_server_rejects_plain_filename_and_bad_token() -> None:
    client = TestClient(create_app(expected_token="test-token"))

    unauthorized = client.get("/health")
    plain_name = client.post(
        "/file_parse",
        headers={"Authorization": "Bearer test-token"},
        files={"files": ("原始文件名.pdf", b"%PDF fake", "application/pdf")},
    )

    assert unauthorized.status_code == 401
    assert plain_name.status_code == 422
