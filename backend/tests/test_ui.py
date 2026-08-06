from app.api import reviews
from app.services.outbox import DispatchResult


def test_review_ui_index_is_served(client):
    response = client.get("/ui/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "IssueFlow Review Console" in response.text
    assert "审核 Agent 提议，再安全写回 GitHub" in response.text
    assert 'id="review-list"' in response.text
    assert 'id="detail-panel"' in response.text
    assert 'id="system-status-label"' in response.text
    assert 'id="unlock-dialog"' in response.text
    assert 'id="unlock-form"' in response.text
    assert 'id="review-admin-token"' in response.text
    assert 'type="password"' in response.text
    assert 'id="lock-button"' in response.text
    assert "Content-Security-Policy" in response.text
    assert 'name="referrer" content="no-referrer"' in response.text


def test_review_ui_static_assets_are_served(client):
    css_response = client.get("/ui/styles.css")
    js_response = client.get("/ui/app.js")

    assert css_response.status_code == 200
    assert css_response.headers["content-type"].startswith("text/css")
    assert ".review-item" in css_response.text
    assert ".detail-content" in css_response.text
    assert ".notification" in css_response.text
    assert ".review-decision-form" in css_response.text
    assert ".decision-button-approve" in css_response.text
    assert ".notification.is-success" in css_response.text
    assert ".unlock-dialog" in css_response.text
    assert ".unlock-dialog::backdrop" in css_response.text
    assert ".unlock-input" in css_response.text
    assert ".lock-button" in css_response.text

    assert js_response.status_code == 200
    assert "javascript" in js_response.headers["content-type"]
    assert "/review-tasks?status=" in js_response.text
    assert "Promise.all" in js_response.text
    assert "renderSelectedReview" in js_response.text


def test_review_ui_uses_safe_rendering_and_explicit_decisions(client):
    response = client.get("/ui/app.js")
    javascript = response.text

    assert ".innerHTML" not in javascript
    assert "insertAdjacentHTML" not in javascript
    assert "outerHTML" not in javascript
    assert 'method: "GET"' in javascript
    assert 'method: "POST"' in javascript
    assert "/approve" in javascript
    assert "/reject" in javascript
    assert "JSON.stringify" in javascript
    assert "window.confirm" in javascript
    assert "reviewerInput.value" in javascript
    assert "maxLength = 200" in javascript
    assert "maxLength = 4000" in javascript
    assert "textContent" in javascript
    assert "setInterval" not in javascript
    assert 'parts[0].toLowerCase() === "local"' in javascript
    assert "!Number.isInteger(number)" in javascript
    assert "number <= 0" in javascript
    assert 'const REVIEW_ADMIN_HEADER_NAME = "X-Review-Admin-Token"' in javascript
    assert "window.sessionStorage" in javascript
    assert "localStorage" not in javascript
    assert "REVIEW_ADMIN_TOKEN_STORAGE_KEY" in javascript
    assert "unlockDialog.showModal()" in javascript
    assert "unlockDialog.addEventListener(\"cancel\"" in javascript
    assert "unlockForm.addEventListener(\"submit\"" in javascript
    assert "lockReviewConsole" in javascript
    assert "clearReviewAdminToken" in javascript
    assert "ReviewAuthenticationError" in javascript
    assert "ReviewAuthenticationConfigurationError" in javascript
    assert "[REVIEW_ADMIN_HEADER_NAME]" in javascript
    assert "console.log" not in javascript
    assert "eval(" not in javascript
    assert "new Function" not in javascript


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


def test_review_approval_response_contract_for_ui(client, monkeypatch):
    captured = {}

    def decide(review_task_id, decision, reviewer, review_note):
        captured.update(
            {
                "review_task_id": review_task_id,
                "decision": decision,
                "reviewer": reviewer,
                "review_note": review_note,
            }
        )
        return {
            "review_task": {
                "id": review_task_id,
                "status": decision,
                "reviewer": reviewer,
                "review_note": review_note,
            },
            "updated_command_ids": [31],
            "outbox_event_key": f"review-commands:{review_task_id}",
        }

    monkeypatch.setattr(reviews, "decide_review_task", decide)
    monkeypatch.setattr(
        reviews,
        "dispatch_event",
        lambda key: DispatchResult(
            event_key=key,
            status="dispatched",
            rq_job_id="job-review-9",
            recovery_pending=False,
        ),
    )

    response = client.post(
        "/review-tasks/9/approve",
        json={
            "reviewer": "chengyebi",
            "review_note": "确认标签和回复内容。",
        },
    )

    assert response.status_code == 200
    assert response.json()["review_task"]["status"] == "approved"
    assert response.json()["rq_job_id"] == "job-review-9"
    assert response.json()["recovery_pending"] is False
    assert captured == {
        "review_task_id": 9,
        "decision": "approved",
        "reviewer": "chengyebi",
        "review_note": "确认标签和回复内容。",
    }


def test_review_decision_validation_contract_for_ui(client):
    missing_reviewer = client.post(
        "/review-tasks/9/reject",
        json={"reviewer": "", "review_note": None},
    )
    oversized_note = client.post(
        "/review-tasks/9/reject",
        json={"reviewer": "chengyebi", "review_note": "x" * 4001},
    )

    assert missing_reviewer.status_code == 422
    assert oversized_note.status_code == 422


def test_review_ui_csp_restricts_scripts_and_connections(client):
    response = client.get("/ui/")
    html = response.text

    assert "default-src 'self'" in html
    assert "script-src 'self'" in html
    assert "style-src 'self'" in html
    assert "connect-src 'self'" in html
    assert "object-src 'none'" in html
    assert "base-uri 'none'" in html
    assert "form-action 'self'" in html
    assert "<script>" not in html
    assert "onclick=" not in html


def test_review_ui_does_not_place_admin_token_in_url_or_body(client):
    response = client.get("/ui/app.js")
    javascript = response.text

    assert "REVIEW_ADMIN_HEADER_NAME" in javascript
    assert "review_admin_token" not in javascript
    assert "URLSearchParams" not in javascript
    assert "location.search" not in javascript
    assert "location.hash" not in javascript
    assert "token=" not in javascript
    assert "JSON.stringify({\n      reviewer," in javascript
    assert "review_note: reviewNote || null" in javascript
