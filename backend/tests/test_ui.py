def test_review_ui_index_is_served(client):
    response = client.get("/ui/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "IssueFlow Review Console" in response.text
    assert "审核 Agent 提议，再安全写回 GitHub" in response.text


def test_review_ui_static_assets_are_served(client):
    css_response = client.get("/ui/styles.css")
    js_response = client.get("/ui/app.js")

    assert css_response.status_code == 200
    assert css_response.headers["content-type"].startswith("text/css")
    assert ".workspace" in css_response.text

    assert js_response.status_code == 200
    assert "javascript" in js_response.headers["content-type"]
    assert "updateRefreshTime" in js_response.text


def test_review_ui_mount_does_not_shadow_existing_api(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
