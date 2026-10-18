from fastapi.testclient import TestClient

from app.main import app, create_app
from app.services.i18n_service import i18n


def test_i18n_service_translation():
    # Assert direct English translation
    assert i18n.translate("nav.dashboard", "en") == "Dashboard"
    # Assert direct Japanese translation
    assert i18n.translate("nav.dashboard", "ja") == "ダッシュボード"
    # Assert nested dot lookup
    assert i18n.translate("dashboard.metrics.active_mock_exams", "en") == "Active Tests"
    assert i18n.translate("dashboard.metrics.active_mock_exams", "ja") == "実施中のテスト"
    # Assert missing key fallback to key name
    assert i18n.translate("dashboard.non_existent_key", "en") == "dashboard.non_existent_key"


def test_new_visible_locale_keys_exist_in_en_and_ja() -> None:
    for lang in ("en", "ja"):
        assert i18n.translate("builder.generic_title", lang) != "builder.generic_title"
        assert i18n.translate("builder.sheet_name", lang) != "builder.sheet_name"
        assert i18n.translate("scans.duplicate_sheet", lang) != "scans.duplicate_sheet"
        assert i18n.translate("packages.invalid_token", lang) != "packages.invalid_token"
        assert i18n.translate("packages.expired", lang) != "packages.expired"


def test_i18n_fallback_to_english():
    # Add a temporary test key to translations
    i18n.translations["en"]["test_fallback"] = "English Fallback Value"
    if "test_fallback" in i18n.translations["ja"]:
        del i18n.translations["ja"]["test_fallback"]

    assert i18n.translate("test_fallback", "ja") == "English Fallback Value"


def test_language_switch_endpoint():
    client = TestClient(app)
    # Get request to lang switch endpoint without following redirect
    response = client.get("/lang/ja", headers={"referer": "/dashboard"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    # Check it sets the lang cookie
    cookies = response.headers.get("set-cookie", "")
    assert "lang=ja" in cookies


def test_ja_locale_translates_key_frontend_pages() -> None:
    with TestClient(create_app()) as client:
        switch = client.get("/lang/ja", headers={"referer": "/templates"}, follow_redirects=False)
        assert switch.status_code == 303

        templates = client.get("/templates")
        assert templates.status_code == 200
        assert "テストテンプレート" in templates.text
        assert "新規テンプレート" in templates.text
        assert "More actions" not in templates.text

        create = client.post(
            "/templates/new",
            data={
                "name": "日本語テンプレート",
                "assessment_type": "generic_test",
            },
            follow_redirects=False,
        )
        assert create.status_code == 303

        builder = client.get(create.headers["location"])
        assert builder.status_code == 200
        assert "テンプレート編集" in builder.text
        assert "Loading template builder" not in builder.text
