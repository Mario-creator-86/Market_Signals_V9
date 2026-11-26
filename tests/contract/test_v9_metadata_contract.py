from pathlib import Path

from api.main import app
from core.observability import app_version


ROOT = Path(__file__).resolve().parents[2]


def test_runtime_metadata_identifies_v9(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)

    assert app.title == "Market Signals V9 - Web API"
    assert app.version == "9.0.0"
    assert app_version() == "9.0.0"


def test_user_facing_static_metadata_has_no_stale_v7_branding():
    for relative in ("frontend/index.html", "frontend/app.js", "frontend/news_hub.html"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "Market Signals V7" not in text
        assert "Market Scanner V7" not in text
        assert "MarketScanner V7" not in text
        assert "Enhanced V7" not in text
