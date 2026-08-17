from app.agents.workflow import prepare_actions


def test_complete_feature_does_not_request_extra_information():
    state = {
        "repo": "microsoft/vscode",
        "issue_number": 10001,
        "category": "feature",
        "confidence": 0.95,
        "needs_clarification": False,

        # 即使模型残留了一些“有则更好”的字段，
        # needs_clarification=False 也不得产生追问评论。
        "missing_repro_fields": [
            "运行环境",
            "软件版本",
            "错误日志",
        ],
        "missing_info_confidence": 0.0,
        "duplicate_assessment": {
            "is_duplicate": False,
        },
    }

    result = prepare_actions(state)

    actions = result["proposed_actions"]

    assert len(actions) == 1
    assert actions[0]["type"] == "add_label"
    assert actions[0]["intent"] == "add_category_label"


def test_issue_that_really_needs_clarification_still_requests_information():
    state = {
        "repo": "microsoft/vscode",
        "issue_number": 10002,
        "category": "bug",
        "confidence": 0.96,
        "needs_clarification": True,
        "missing_repro_fields": [
            "复现步骤",
            "关键错误日志",
        ],
        "missing_info_confidence": 0.90,
        "duplicate_assessment": {
            "is_duplicate": False,
        },
    }

    result = prepare_actions(state)

    actions = result["proposed_actions"]

    assert [action["type"] for action in actions] == [
        "add_label",
        "post_comment",
    ]
    assert actions[1]["intent"] == "request_missing_information"
