from app.rag.prepare_duplicate_labels import _extract_reference, _similarity


def test_explicit_duplicate_reference_is_extracted_with_evidence():
    number, evidence = _extract_reference("Maintainer: /duplicate of #326445 thanks")
    assert number == 326445
    assert "/duplicate of #326445" in evidence


def test_hard_negative_similarity_uses_shared_topic_terms():
    query = {"title": "Color theme menu flashes and closes", "labels": [{"name": "bug"}]}
    related = {"title": "Color theme picker closes immediately", "labels": [{"name": "bug"}]}
    unrelated = {"title": "Terminal font rendering", "labels": [{"name": "terminal"}]}

    related_score, shared = _similarity(query, related)
    unrelated_score, _ = _similarity(query, unrelated)
    assert related_score > unrelated_score
    assert "color" in shared
    assert "theme" in shared
