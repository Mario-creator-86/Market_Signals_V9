import os
import re
import logging
import ipaddress
import socket
from typing import Optional, Set
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

from core.http_utils import http_get
from core.data_quality import get_health_dashboard, QualifiedValue

logger = logging.getLogger("ContentFetcher")

_MAX_RESPONSE_BYTES = 512 * 1024

_ALLOWED_DOMAINS: Set[str] = {
    "reuters.com", "bloomberg.com", "cnbc.com", "finance.yahoo.com", "yahoo.com",
    "marketwatch.com", "marketbeat.com", "wsj.com", "ft.com", "barrons.com", "investing.com",
    "seekingalpha.com", "benzinga.com", "thestreet.com", "fool.com",
    "businessinsider.com", "forbes.com", "fortune.com", "economist.com",
    "techcrunch.com", "theverge.com", "arstechnica.com", "wired.com",
    "coindesk.com", "cointelegraph.com", "decrypt.co", "theblock.co",
    "bitcoinist.com", "newsbtc.com", "coingape.com", "crypto.news",
    "cryptopotato.com", "cryptonews.com", "beincrypto.com", "blockspace.media",
    # Market publications currently emitted by the Yahoo/top-stories feeds.
    # Keep these explicit: this remains an SSRF allowlist, not an open-web fetcher.
    "247wallst.com", "investors.com", "barchart.com", "proactiveinvestors.com",
    "trefis.com", "entrepreneur.com", "stocktwits.com", "investorshub.advfn.com",
    # Crypto exchange announcements + keyless crypto news (Faza 4 Part D).
    # Needed so fetch_article_content can extract full text from these sources
    # for AI summarization. http_get itself does not enforce this allowlist
    # (only blocks FMP), but full-article extraction does.
    "binance.com", "kucoin.com", "bitcoinmagazine.com",
    # eToro post/news pages: a large share of the news-tab feed is eToro social
    # content; without this the WIIM takeaway fetcher failed on every such URL.
    "etoro.com",
    "apnews.com", "bbc.com", "cnn.com", "nytimes.com", "washingtonpost.com",
    "theguardian.com", "aljazeera.com", "npr.org",
    "sec.gov", "federalreserve.gov", "whitehouse.gov",
    "google.com", "googleapis.com",
    "youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "dailymotion.com", "twitch.tv",
}

_DEV_MODE = os.getenv("MS_DEV_MODE", "0").strip() in ("1", "true", "yes")


def _is_private_ip(hostname: str) -> bool:
    try:
        resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    for entry in resolved:
        ip_str = entry[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            # ::ffff:127.0.0.1 must be judged as the IPv4 address it wraps.
            ip = mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_unspecified
            or ip.is_multicast
        ):
            return True
    return False


_MAX_REDIRECT_HOPS = 5


def _validate_fetch_target(url: str, *, origin_hostname: Optional[str] = None) -> str:
    """Raise ValueError unless ``url`` may be requested; return its hostname.

    Applied to the original URL AND to every redirect target before it is
    requested.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f"Blocked URL with disallowed scheme: {parsed.scheme}")
    hostname = parsed.hostname
    label = "redirect to" if origin_hostname else "URL resolving to"
    if not hostname:
        raise ValueError("Blocked URL without a hostname")
    if _is_private_ip(hostname):
        suffix = f" (original: {origin_hostname})" if origin_hostname else ""
        raise ValueError(f"Blocked {label} private IP: {hostname}{suffix}")
    if not _is_domain_allowed(hostname):
        if origin_hostname:
            logger.warning(f"Redirect domain blocked by allowlist: {hostname} (original: {origin_hostname})")
            raise ValueError(f"Blocked redirect domain not in allowlist: {hostname}")
        logger.warning(f"Domain blocked by allowlist: {hostname}")
        raise ValueError(f"Blocked URL domain not in allowlist: {hostname}")
    return hostname


def _get_following_validated_redirects(url: str, headers: dict, hostname: str):
    """GET ``url`` following redirects one hop at a time.

    ``allow_redirects=True`` let the HTTP client contact every hop first and
    only the FINAL url was checked afterwards, so a redirect to a loopback /
    private / metadata address had already been requested (and intermediate
    hops were never checked at all). Each Location is now validated before the
    request for it is sent.
    """
    current = url
    for _hop in range(_MAX_REDIRECT_HOPS + 1):
        resp = http_get(
            current,
            provider="news",
            headers=headers,
            timeout=(2, 6),
            allow_redirects=False,
            max_retries=1,
        )
        if resp is None:
            return None
        status = int(getattr(resp, "status_code", 0) or 0)
        location = ""
        try:
            location = str((getattr(resp, "headers", None) or {}).get("Location") or "").strip()
        except Exception:
            location = ""
        if status not in (301, 302, 303, 307, 308) or not location:
            return resp
        next_url = urljoin(current, location)
        _validate_fetch_target(next_url, origin_hostname=hostname)
        current = next_url
    raise ValueError(f"Blocked URL with more than {_MAX_REDIRECT_HOPS} redirects: {url}")


def _is_domain_allowed(hostname: str) -> bool:
    if _DEV_MODE:
        return True
    if not hostname:
        return False
    lower = hostname.lower()
    for allowed in _ALLOWED_DOMAINS:
        if lower == allowed or lower.endswith("." + allowed):
            return True
    return False


def fetch_article_content(url: str, max_length: int = 8000, max_response_bytes: int = _MAX_RESPONSE_BYTES) -> Optional[str]:
    """
    Fetch URL and extract readable text content using BeautifulSoup.
    Prioritizes article bodies to avoid navigation/footer noise.
    """
    if not url:
        return None

    # Skip video platforms (can't be scraped without JS) - return placeholder
    video_domains = ['youtube.com', 'youtu.be', 'vimeo.com', 'tiktok.com', 'dailymotion.com', 'twitch.tv']
    if any(domain in url.lower() for domain in video_domains):
        logger.info(f"Video URL detected (no text extraction): {url}")
        return "[VIDEO] This is a video link - click to watch."

    # JS-heavy sites that require browser rendering
    js_heavy_sites = ['newsbtc.com', 'bitcoinist.com', 'coingape.com']
    if any(site in url.lower() for site in js_heavy_sites):
        logger.info(f"JS-heavy site detected (requires browser): {url}")
        return "[JS-SITE] This site requires JavaScript - click to view in browser."

    # SSRF protection: validate scheme and block private IPs
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise ValueError(f"Blocked URL with disallowed scheme: {parsed.scheme}")
    hostname = parsed.hostname
    if hostname and _is_private_ip(hostname):
        raise ValueError(f"Blocked URL resolving to private IP: {hostname}")
    if not _is_domain_allowed(hostname):
        logger.warning(f"Domain blocked by allowlist: {hostname}")
        raise ValueError(f"Blocked URL domain not in allowlist: {hostname}")

    try:
        # Simple headers - avoid triggering GDPR consent walls
        # Note: Complex Sec-Fetch-* headers can trigger consent walls on EU IPs
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        
        # Redirect SSRF protection: every hop is validated BEFORE it is
        # requested (see _get_following_validated_redirects).
        resp = _get_following_validated_redirects(url, headers, hostname)
        if resp is None:
            get_health_dashboard().register("content_fetcher", QualifiedValue.unavailable("content_fetcher", "Circuit open"))
            logger.warning(f"Circuit open for news provider: {url}")
            return None

        # Defence in depth: the final URL must still be an allowed public host.
        final_url = str(getattr(resp, "url", "") or url)
        final_parsed = urlparse(final_url)
        final_hostname = final_parsed.hostname
        if final_hostname and _is_private_ip(final_hostname):
            raise ValueError(f"Blocked redirect to private IP: {final_hostname} (original: {hostname})")
        if not _is_domain_allowed(final_hostname):
            logger.warning(f"Redirect domain blocked by allowlist: {final_hostname} (original: {hostname})")
            raise ValueError(f"Blocked redirect domain not in allowlist: {final_hostname}")

        if max_response_bytes and len(resp.content) > max_response_bytes:
            logger.warning(f"Response too large ({len(resp.content)} bytes, limit {max_response_bytes}): {url}")
            return None

        if resp.status_code in [401, 403]:
            get_health_dashboard().register("content_fetcher", QualifiedValue.unavailable("content_fetcher", f"HTTP {resp.status_code}"))
            logger.info(f"Site blocked access ({resp.status_code}): {url}")
            return "[BLOCKED] This site requires authentication - click to view in browser."
        
        if resp.status_code != 200:
            get_health_dashboard().register("content_fetcher", QualifiedValue.unavailable("content_fetcher", f"HTTP {resp.status_code}"))
            logger.warning(f"Failed to fetch {url}: {resp.status_code}")
            return None
            
        soup = BeautifulSoup(resp.text, 'html.parser')

        # specific cleanup
        for tag in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'form', 'noscript']):
            tag.decompose()

        # Target site-specific article bodies
        content_div = None
        
        # Priority 1: Reuters
        if 'reuters.com' in url:
            content_div = soup.find('div', {'data-testid': 'ArticleBody'}) or soup.find('article')
        
        # Priority 2: Yahoo Finance Specific
        if not content_div:
            content_div = soup.find('div', class_='caas-body')
        
        # Priority 3: Generic Article Tags
        if not content_div:
            content_div = soup.find('article')
        
        # Priority 4: Main content div
        if not content_div:
            content_div = soup.find('main') or soup.find('div', class_=re.compile(r'article|content|story|post', re.I))
            
        # Priority 5: Fallback to body
        if not content_div:
            content_div = soup.body

        if not content_div:
            get_health_dashboard().register("content_fetcher", QualifiedValue.unavailable("content_fetcher", "No article content extracted"))
            return "[NO-CONTENT] No article content could be extracted - click to view in browser."

        # Get text and clean up whitespace
        text = content_div.get_text(separator=' ', strip=True)
        text = re.sub(r'\s+', ' ', text).strip()

        get_health_dashboard().register("content_fetcher", QualifiedValue.verified(len(text), "content_fetcher"))
        if "Oops, something went wrong" in text or "Sign in to continue" in text:
             logger.warning(f"Likely blocked/error page for {url}")

        return text[:max_length]

    except Exception as e:
        get_health_dashboard().register("content_fetcher", QualifiedValue.unavailable("content_fetcher", str(e)))
        logger.error(f"Error fetching article content: {e}")
        return None
