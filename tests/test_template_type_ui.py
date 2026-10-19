from pathlib import Path


def _template(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_peer_review_template_creation_is_enabled() -> None:
    new_template = _template("app/templates/test_templates/new.html")

    # Peer review is now a first-class, selectable template type.
    assert 'name="assessment_type" value="peer_review"' in new_template
    assert 'value="peer_review" disabled' not in new_template


def test_peer_review_preview_does_not_show_reviewer_field() -> None:
    new_template = _template("app/templates/test_templates/new.html")
    peer_preview = new_template.split('id="preview-peer"', 1)[1].split("</table>", 1)[0]

    assert "Reviewer:" not in peer_preview
    assert "reviewer') }}: ___________" not in peer_preview
    # Anonymity note is localized via the peer_review.* keys.
    assert "peer_review.reusable_note" in peer_preview


def test_peer_review_preview_mentions_anonymous_reusable_sheet() -> None:
    new_template = _template("app/templates/test_templates/new.html")

    assert "peer_review.anonymous" in new_template


def test_generic_builder_heading_is_generic() -> None:
    builder_template = _template("app/templates/test_templates/builder.html")

    assert "builder.generic_title" in builder_template
    assert 'template.assessment_type == "generic_test"' in builder_template


def test_template_creation_route_routes_peer_to_builder() -> None:
    route_source = _template("app/routes/test_templates.py")

    # Peer-review templates are created and routed to their own WYSIWYG builder.
    assert 'if template.assessment_type == "peer_review":' in route_source
    assert "/templates/{template.id}/peer-review" in route_source
