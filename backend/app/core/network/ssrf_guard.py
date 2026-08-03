"""
SSRF Prevention Module

Validates URLs to prevent Server-Side Request Forgery (SSRF) attacks
in proxy health checks and other outbound requests.
"""

import ipaddress
import re
import urllib.parse
from typing import Optional


# Private/reserved IP ranges that should NEVER be accessed
BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::ffff:0:0/96"),
]

ALLOWED_SCHEMES = {"http", "https"}

BLOCKED_HOST_PATTERNS = [
    re.compile(r"^localhost$", re.I),
    re.compile(r"^localhost\.", re.I),
    re.compile(r"^127\.", re.I),
    re.compile(r"^0\.", re.I),
    re.compile(r"^10\.", re.I),
    re.compile(r"^172\.(1[6-9]|2[0-9]|3[01])\.", re.I),
    re.compile(r"^192\.168\.", re.I),
    re.compile(r"^169\.254\.", re.I),
    re.compile(r"^192\.0\.0\.", re.I),
    re.compile(r"^192\.0\.2\.", re.I),
    re.compile(r"^198\.(1[89])\.", re.I),
    re.compile(r"^198\.51\.100\.", re.I),
    re.compile(r"^203\.0\.113\.", re.I),
    re.compile(r"^224\.", re.I),
    re.compile(r"^240\.", re.I),
    re.compile(r"^255\.", re.I),
    re.compile(r"^169\.254\.169\.254$"),
    re.compile(r"\.internal$", re.I),
    re.compile(r"\.local$", re.I),
    re.compile(r"\.svc$", re.I),
    re.compile(r"\.cluster\.local$", re.I),
]


def is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        for network in BLOCKED_NETWORKS:
            if ip in network:
                return True
    except ValueError:
        pass
    return False


def is_blocked_hostname(hostname: str) -> bool:
    if not hostname:
        return True
    hostname_lower = hostname.lower().strip()
    for pattern in BLOCKED_HOST_PATTERNS:
        if pattern.search(hostname_lower):
            return True
    return False


def validate_url(
    url: str,
    allowed_schemes: Optional[set] = None,
    allowed_hosts: Optional[set] = None,
    require_public_ip: bool = True,
) -> tuple[bool, str]:
    if not url or not isinstance(url, str):
        return False, "URL is empty or invalid"

    url = url.strip()

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        return False, f"Failed to parse URL: {e}"

    schemes = allowed_schemes or ALLOWED_SCHEMES
    if not parsed.scheme:
        return False, "URL must have a scheme (http:// or https://)"
    if parsed.scheme.lower() not in schemes:
        return False, f"URL scheme '{parsed.scheme}' is not allowed"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"

    if allowed_hosts:
        if hostname.lower() in {h.lower() for h in allowed_hosts}:
            return True, "ok"

    if is_blocked_hostname(hostname):
        return False, f"Hostname '{hostname}' is in blocked list"

    if require_public_ip:
        if is_blocked_ip(hostname):
            return False, f"IP address '{hostname}' is in a blocked private/reserved range"
        try:
            ipaddress.ip_address(hostname)
            return False, "Direct IP addresses are not allowed (use hostname)"
        except ValueError:
            pass

    if parsed.username or parsed.password:
        return False, "URLs with embedded credentials are not allowed"

    if parsed.port:
        if parsed.scheme == "http" and parsed.port == 80:
            pass
        elif parsed.scheme == "https" and parsed.port == 443:
            pass
        else:
            return False, f"Non-standard port {parsed.port} is not allowed"

    if not parsed.netloc:
        return False, "URL has no network location"

    return True, "ok"


def get_safe_health_check_url(
    url: Optional[str] = None,
    fallback: str = "https://api.ipify.org?format=json",
) -> str:
    if url:
        is_valid, error = validate_url(url)
        if is_valid:
            return url
        import structlog
        logger = structlog.get_logger()
        logger.warning("ssrf_unsafe_health_check_url_rejected", url=url, error=error)

    is_valid, error = validate_url(fallback)
    if is_valid:
        return fallback

    return "https://api.ipify.org?format=json"
