import ipaddress
import socket
from unittest.mock import patch, MagicMock
from urllib.parse import urlparse

import pytest

from core.content_fetcher import _is_private_ip, _is_domain_allowed, fetch_article_content

pytestmark = pytest.mark.unit


class TestIsPrivateIP:
    def _resolve_to(self, ip_str):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip_str, 0))]

    def test_loopback_blocked(self):
        with patch("core.content_fetcher.socket.getaddrinfo", return_value=self._resolve_to("127.0.0.1")):
            assert _is_private_ip("localhost") is True

    def test_private_10_range_blocked(self):
        with patch("core.content_fetcher.socket.getaddrinfo", return_value=self._resolve_to("10.0.0.1")):
            assert _is_private_ip("internal.host") is True

    def test_private_172_range_blocked(self):
        with patch("core.content_fetcher.socket.getaddrinfo", return_value=self._resolve_to("172.16.0.1")):
            assert _is_private_ip("internal.host") is True

    def test_private_192_range_blocked(self):
        with patch("core.content_fetcher.socket.getaddrinfo", return_value=self._resolve_to("192.168.1.1")):
            assert _is_private_ip("internal.host") is True

    def test_link_local_blocked(self):
        with patch("core.content_fetcher.socket.getaddrinfo", return_value=self._resolve_to("169.254.1.1")):
            assert _is_private_ip("internal.host") is True

    def test_public_ip_allowed(self):
        with patch("core.content_fetcher.socket.getaddrinfo", return_value=self._resolve_to("8.8.8.8")):
            assert _is_private_ip("dns.google") is False

    def test_another_public_ip_allowed(self):
        with patch("core.content_fetcher.socket.getaddrinfo", return_value=self._resolve_to("1.1.1.1")):
            assert _is_private_ip("cloudflare") is False

    def test_unresolvable_hostname_returns_false(self):
        with patch("core.content_fetcher.socket.getaddrinfo", side_effect=socket.gaierror):
            assert _is_private_ip("nonexistent.host.invalid") is False

    def test_mixed_ips_any_private_blocks(self):
        entries = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0)),
        ]
        with patch("core.content_fetcher.socket.getaddrinfo", return_value=entries):
            assert _is_private_ip("mixed.host") is True

    def test_ipv6_loopback_blocked(self):
        entries = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]
        with patch("core.content_fetcher.socket.getaddrinfo", return_value=entries):
            assert _is_private_ip("localhost") is True


class TestDomainAllowlist:
    def test_allowed_domain_exact_match(self):
        assert _is_domain_allowed("reuters.com") is True

    def test_allowed_domain_subdomain(self):
        assert _is_domain_allowed("www.reuters.com") is True

    def test_allowed_domain_nested_subdomain(self):
        assert _is_domain_allowed("news.reuters.com") is True

    def test_marketbeat_article_domain_and_subdomain_allowed(self):
        assert _is_domain_allowed("marketbeat.com") is True
        assert _is_domain_allowed("www.marketbeat.com") is True

    @pytest.mark.parametrize(
        "domain",
        [
            "crypto.news",
            "www.cryptopotato.com",
            "www.investors.com",
            "www.barchart.com",
            "stocktwits.com",
            "investorshub.advfn.com",
        ],
    )
    def test_current_newsroom_publishers_are_allowed(self, domain):
        assert _is_domain_allowed(domain) is True

    def test_blocked_domain(self):
        assert _is_domain_allowed("evil-phishing.com") is False

    def test_empty_hostname_blocked(self):
        assert _is_domain_allowed("") is False

    def test_none_hostname_blocked(self):
        assert _is_domain_allowed(None) is False

    def test_similar_domain_blocked(self):
        assert _is_domain_allowed("reuters.com.evil.com") is False
        assert _is_domain_allowed("stocktwits.com.evil.com") is False

    def test_dev_mode_allows_all(self):
        with patch("core.content_fetcher._DEV_MODE", True):
            assert _is_domain_allowed("anything.evil") is True


class TestFetchArticleContentSSRF:
    def test_empty_url_returns_none(self):
        assert fetch_article_content("") is None
        assert fetch_article_content(None) is None

    def test_blocked_scheme_raises(self):
        with pytest.raises(ValueError, match="disallowed scheme"):
            fetch_article_content("ftp://example.com/file")

    def test_file_scheme_raises(self):
        with pytest.raises(ValueError, match="disallowed scheme"):
            fetch_article_content("file:///etc/passwd")

    def test_javascript_scheme_raises(self):
        with pytest.raises(ValueError, match="disallowed scheme"):
            fetch_article_content("javascript:alert(1)")

    def test_private_ip_raises(self):
        with patch("core.content_fetcher._is_private_ip", return_value=True):
            with pytest.raises(ValueError, match="private IP"):
                fetch_article_content("http://internal.corp/admin")

    def test_disallowed_domain_raises(self):
        with patch("core.content_fetcher._is_private_ip", return_value=False):
            with patch("core.content_fetcher._is_domain_allowed", return_value=False):
                with pytest.raises(ValueError, match="allowlist"):
                    fetch_article_content("http://evil-phishing.com/article")

    def test_video_domain_returns_placeholder(self):
        result = fetch_article_content("https://www.youtube.com/watch?v=abc123")
        assert result is not None
        assert "[VIDEO]" in result

    def test_js_heavy_site_returns_placeholder(self):
        result = fetch_article_content("https://newsbtc.com/some-article")
        assert result is not None
        assert "[JS-SITE]" in result
