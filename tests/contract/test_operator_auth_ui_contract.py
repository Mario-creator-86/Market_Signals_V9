from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_operator_auth_assets_load_before_application_code():
    markup = (PROJECT_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    assert 'href="styles_auth.css?v=1.0"' in markup
    assert 'src="auth_client.js?v=1.1"' in markup
    app_script = 'src="app.js?v='
    assert app_script in markup
    assert markup.index('src="auth_client.js?v=1.1"') < markup.index(app_script)


def test_operator_auth_uses_httponly_session_exchange_not_key_storage():
    script = (PROJECT_ROOT / "frontend" / "auth_client.js").read_text(encoding="utf-8")

    assert "/api/auth/login" in script
    assert "/api/auth/logout" in script
    assert "/api/auth/status" in script
    assert "X-CSRF-Token" in script
    assert "window.MSAuthReady = new Promise" in script
    assert "releaseApplication()" in script
    assert "credentials = options.credentials || 'same-origin'" in script
    assert "sessionStorage.setItem(CSRF_STORAGE_KEY" in script
    assert "localStorage.setItem" not in script
    assert "sessionStorage.setItem('api" not in script.lower()


def test_operator_auth_modal_is_accessible_and_does_not_echo_key():
    script = (PROJECT_ROOT / "frontend" / "auth_client.js").read_text(encoding="utf-8")
    styles = (PROJECT_ROOT / "frontend" / "styles_auth.css").read_text(encoding="utf-8")

    assert "aria-modal" in script
    assert "aria-live" in script
    assert 'type="password"' in script
    assert "input.value = ''" in script
    assert "setAuthNotice('Sesiunea a fost închisă.')" in script
    assert "window.location.reload()" in script
    assert ".operator-auth" in styles
    assert "@media (max-width: 760px)" in styles


def test_application_polling_waits_for_auth_bootstrap():
    app_script = (PROJECT_ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    stats_script = (PROJECT_ROOT / "frontend" / "api_stats_widget.js").read_text(encoding="utf-8")

    assert app_script.count("await (window.MSAuthReady || Promise.resolve())") >= 3
    assert "await (window.MSAuthReady || Promise.resolve())" in stats_script
