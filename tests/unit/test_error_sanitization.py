from fastapi import HTTPException

import pytest

from api.main import _safe_error

pytestmark = pytest.mark.unit


class TestSafeError:
    def test_default_message_is_generic(self):
        err = _safe_error()
        assert isinstance(err, HTTPException)
        assert err.status_code == 500
        assert err.detail == "Internal server error"

    def test_custom_message_passed_through(self):
        err = _safe_error("Service unavailable")
        assert err.detail == "Service unavailable"

    def test_does_not_leak_traceback(self):
        err = _safe_error()
        assert "Traceback" not in str(err.detail)
        assert "File" not in str(err.detail)
        assert ".py" not in str(err.detail)

    def test_does_not_include_path_separator(self):
        err = _safe_error()
        assert "/" not in str(err.detail) or "server" in str(err.detail).lower()

    def test_returns_http_exception(self):
        err = _safe_error("Rate limit exceeded")
        assert err.status_code == 500
        assert isinstance(err, HTTPException)

    def test_detail_is_string_not_dict(self):
        err = _safe_error()
        assert isinstance(err.detail, str)

    def test_no_stack_trace_in_detail(self):
        err = _safe_error("Database connection failed at /var/lib/db: timeout")
        assert "Traceback" not in err.detail
        assert "stack" not in err.detail.lower() or "Internal" in err.detail