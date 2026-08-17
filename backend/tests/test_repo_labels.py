"""仓库级 category -> GitHub label 解析测试（P1.3）。"""

import pytest

from app.automation.repo_labels import (
    REPO_CATEGORY_LABELS,
    has_valid_mapping,
    resolve_category_label,
)


class TestRepoLabelResolver:
    def test_vscode_feature(self):
        assert resolve_category_label("microsoft/vscode", "feature") == "feature-request"

    def test_vscode_bug(self):
        assert resolve_category_label("microsoft/vscode", "bug") == "bug"

    def test_node_bug(self):
        assert resolve_category_label("nodejs/node", "bug") == "confirmed-bug"

    def test_node_documentation(self):
        assert resolve_category_label("nodejs/node", "documentation") == "doc"

    def test_node_question(self):
        assert resolve_category_label("nodejs/node", "question") == "question"

    def test_rust_bug(self):
        assert resolve_category_label("rust-lang/rust", "bug") == "C-bug"

    def test_rust_feature_unsupported(self):
        assert resolve_category_label("rust-lang/rust", "feature") is None

    def test_unknown_repo_unsupported(self):
        assert resolve_category_label("unknown/repo", "bug") is None

    def test_unknown_category_unsupported(self):
        assert resolve_category_label("microsoft/vscode", "other") is None

    def test_has_valid_mapping(self):
        assert has_valid_mapping("microsoft/vscode", "bug") is True
        assert has_valid_mapping("rust-lang/rust", "feature") is False
        assert has_valid_mapping("unknown/repo", "bug") is False
