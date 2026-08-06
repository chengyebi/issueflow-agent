from app.api import reviews


def test_review_ui_index_is_served(client):
    response = client.get("/ui/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "IssueFlow Review Console" in response.text
    assert "审核 Agent 提议，再安全写回 GitHub" in response.text
    assert 'id="review-list"' in response.text
    assert 'id="detail-panel"' in response.text
    assert 'id="system-status-label"' in response.text


def test_review_ui_static_assets_are_served(client):
    css_response = client.get("/ui/styles.css")
    js_response = client.get("/ui/app.js")

    assert css_response.status_code == 200
    assert css_response.headers["content-type"].startswith("text/css")
    assert ".review-item" in css_response.text
    assert ".detail-content" in css_response.text
    assert ".notification" in css_response.text

    assert js_response.status_code == 200
    assert "javascript" in js_response.headers["content-type"]
    assert "/review-tasks?status=" in js_response.text
    assert "Promise.all" in js_response.text
    assert "renderSelectedReview" in js_response.text


def test_review_ui_uses_safe_read_only_rendering(client):
    response = client.get("/ui/app.js")
    javascript = response.text

    assert ".innerHTML" not in javascript
    assert "insertAdjacentHTML" not in javascript
    assert "/approve" not in javascript
    assert "/reject" not in javascript
    assert 'method: "GET"' in javascript
    assert "textContent" in javascript


def test_review_ui_mount_does_not_shadow_existing_api(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_review_task_list_contract_for_ui(client, monkeypatch):
    item = {
        "review_task_id": 9,
        "review_status": "pending",
        "reviewer": None,
        "review_note": None,
        "created_at": "2026-08-07T00:00:00+08:00",
        "reviewed_at": None,
        "agent_run_id": 21,
        "result_json": {
            "category": "bug",
            "priority": "high",
            "risk_level": "low",
            "confidence": 0.92,
            "summary": "登录请求返回 500。",
            "suggested_reply": "请补充服务端日志。",
            "missing_repro_fields": ["错误日志"],
            "retrieval_mode": "hybrid",
            "retrieval_degraded": False,
            "similar_issues": [],
            "duplicate_assessment": {
                "is_duplicate": False,
                "confidence": 0.85,
                "candidate_issue_number": None,
                "rationale": "触发条件不同。",
                "evidence": [],
            },
        },
        "repo": "chengyebi/issueflow-agent",
        "issue_number": 12,
        "issue_title": "Login returns 500",
        "issue_body": "The login endpoint fails.",
        "commands": [
            {
                "id": 31,
                "command_type": "add_label",
                "payload": {"value": "bug"},
                "status": "proposed",
                "idempotency_key": "agent-run:21:action:0:add_label",
                "error_type": None,
                "error_message": None,
                "retry_safe": False,
            }
        ],
    }

    monkeypatch.setattr(
        reviews,
        "list_review_tasks",
        lambda status: [item] if status == "pending" else [],
    )

    response = client.get("/review-tasks?status=pending")

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["review_task_id"] == 9
    assert response.json()["items"][0]["result_json"]["category"] == "bug"
    assert response.json()["items"][0]["commands"][0]["status"] == "proposed"
