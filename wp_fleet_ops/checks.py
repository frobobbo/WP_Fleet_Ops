from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from ipaddress import AddressValueError, IPv4Address, IPv6Address
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse, urlsplit, urlunsplit


MAX_SITE_NAME_LENGTH = 200
MAX_SITE_URL_LENGTH = 2048
MAX_CLIENT_NAME_LENGTH = 200
MAX_WORDPRESS_VERSION_LENGTH = 100
PERCENT_DECODE_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_~"
)


def normalize_site_name(name: str) -> str:
    """Return a canonical non-empty site label for persisted operational data."""
    normalized = name.strip()
    if not normalized:
        raise ValueError("Site name must not be blank.")
    if any(not char.isprintable() for char in normalized):
        raise ValueError("Site name must contain only printable characters.")
    if len(normalized) > MAX_SITE_NAME_LENGTH:
        raise ValueError(f"Site name must be {MAX_SITE_NAME_LENGTH} characters or fewer.")
    return normalized


def normalize_client_name(client: str) -> str:
    """Return a bounded client label while preserving an empty assignment."""
    normalized = client.strip()
    if any(not char.isprintable() for char in normalized):
        raise ValueError("Client name must contain only printable characters.")
    if len(normalized) > MAX_CLIENT_NAME_LENGTH:
        raise ValueError(f"Client name must be {MAX_CLIENT_NAME_LENGTH} characters or fewer.")
    return normalized


def normalize_wordpress_version(version: str) -> str:
    """Return a safe version label, using ``unknown`` when none was supplied."""
    normalized = version.strip()
    if not normalized:
        return "unknown"
    if any(not char.isprintable() for char in normalized):
        raise ValueError("WordPress version must contain only printable characters.")
    if len(normalized) > MAX_WORDPRESS_VERSION_LENGTH:
        raise ValueError(
            f"WordPress version must be {MAX_WORDPRESS_VERSION_LENGTH} characters or fewer."
        )
    return normalized


def normalize_site_url(url: str) -> str:
    candidate = url.strip()
    error = "Site URL must be a valid HTTP or HTTPS URL."
    if len(candidate) > MAX_SITE_URL_LENGTH:
        raise ValueError(f"Site URL must be {MAX_SITE_URL_LENGTH} characters or fewer.")
    # WHATWG-style HTTP clients may treat a raw backslash as a slash while
    # urllib.parse preserves it inside the authority or path. Reject that
    # ambiguous spelling instead of persisting or probing a misleading target.
    # Also reject non-printing characters. urlparse preserves Unicode format
    # controls as well as several C0/DEL bytes, but dashboards and downstream
    # HTTP clients cannot safely or consistently display/request such URLs.
    # Apply the same rule to percent-encoded controls, and reject malformed
    # percent escapes, so persisted request targets cannot be decoded differently
    # by urllib, a reverse proxy, and the monitored origin.
    if (
        not candidate
        or "\\" in candidate
        or any(char.isspace() or not char.isprintable() for char in candidate)
        or re.search(r"%(?![0-9a-f]{2})", candidate, re.IGNORECASE)
        or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", candidate, re.IGNORECASE)
    ):
        raise ValueError(error)
    # Percent-escape hex digits are case-insensitive. Persist one spelling so
    # equivalent request targets cannot create separate monitored site records.
    # Decode ASCII alphanumerics and the non-dot unreserved characters because
    # their encoded and literal forms identify the same URI. Keep delimiters and
    # encoded dots escaped until the URL is split: complete ``.`` and ``..`` path
    # segments have special resolution behavior, while query dots do not.
    candidate = re.sub(
        r"%([0-9a-f]{2})",
        lambda match: (
            decoded
            if (decoded := chr(int(match.group(1), 16))) in PERCENT_DECODE_SAFE
            else f"%{match.group(1).upper()}"
        ),
        candidate,
        flags=re.IGNORECASE,
    )
    if "://" not in candidate:
        explicit_scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate)
        # A scheme-less host may put a query or fragment immediately after its
        # port; both delimit the authority just as a slash does. Recognize all
        # three delimiters before rejecting an apparent unsupported scheme.
        host_with_port = re.match(r"^[^/:\s]+:\d+(?:[/?#]|$)", candidate)
        if explicit_scheme and not host_with_port:
            raise ValueError(error)
        candidate = f"https://{candidate}"
    # An explicitly empty query is still part of the HTTP request target and
    # can be routed differently from a URL with no query delimiter. urlsplit
    # represents both forms with an empty ``query``, so retain the delimiter's
    # presence separately (while ignoring question marks inside the fragment).
    has_query_delimiter = "?" in candidate.partition("#")[0]
    # urlparse splits the final path segment's semicolon parameters into a
    # separate field and cannot distinguish no parameter delimiter from an
    # explicitly empty one (``/status`` versus ``/status;``). urlsplit keeps the
    # complete request path so canonicalization cannot merge distinct targets.
    try:
        parsed = urlsplit(candidate)
        parsed_port = parsed.port
        parsed_hostname = parsed.hostname
    except ValueError as exc:
        # ``urlsplit`` rejects malformed bracketed IPv6 authorities before it
        # returns a parsed result. Translate every parser failure into the same
        # stable validation contract used by malformed ports and hostnames.
        raise ValueError(error) from exc
    scheme = parsed.scheme.lower()
    raw_netloc = parsed.netloc
    if (
        scheme not in {"http", "https"}
        or not parsed_hostname
        or parsed.username is not None
        or parsed.password is not None
        # urlsplit reports no port for an explicit empty ``:`` suffix. Reject
        # the malformed authority instead of silently canonicalizing it to the
        # same target as a URL whose port was actually omitted.
        or raw_netloc.endswith(":")
        or any(char.isspace() for char in raw_netloc)
    ):
        raise ValueError(error)
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise ValueError(error)
    # URI percent-encoding permits dots in registered hostnames. Decode dots only
    # in non-IPv6 hosts: encoded dots in paths stay escaped because complete ``.``
    # and ``..`` path segments have special resolution behavior.
    hostname = parsed_hostname.lower()
    if ":" not in hostname:
        hostname = re.sub(r"%2e", ".", hostname, flags=re.IGNORECASE)
    if not hostname:
        raise ValueError(error)
    if ":" in hostname:
        try:
            # IPv6 has many equivalent textual spellings. Persist the RFC 5952-
            # style compressed form so one endpoint cannot become several sites.
            hostname = IPv6Address(hostname).compressed
        except AddressValueError as exc:
            raise ValueError(error) from exc
    else:
        try:
            # Network clients use the ASCII-compatible form of international
            # domains. Persist it too so Unicode and punycode inputs dedupe.
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(error) from exc
        # A terminal dot marks an absolute DNS name but resolves to the same host.
        # Canonicalize it after IDNA conversion because the codec maps Unicode DNS
        # root separators such as ``。`` to an ASCII dot.
        hostname = hostname.removesuffix(".")
        if not hostname:
            raise ValueError(error)
        try:
            hostname = IPv4Address(hostname).compressed
        except AddressValueError:
            # POSIX resolvers may interpret shortened, integer, octal, and hex
            # host spellings as IPv4 addresses (for example 127.1 resolves to
            # 127.0.0.1). Reject those ambiguous legacy forms rather than
            # persisting a hostname that points somewhere different than it
            # appears to. Ordinary DNS names remain accepted.
            numeric_component = r"(?:0x[0-9a-f]+|[0-9]+)"
            if re.fullmatch(rf"{numeric_component}(?:\.{numeric_component}){{0,3}}", hostname):
                raise ValueError(error)
            # IDNA conversion alone does not reject every malformed DNS label;
            # Python's built-in codec accepts leading/trailing hyphens and
            # underscores. Keep persisted/fetched hosts within the ordinary DNS
            # hostname grammar so operators cannot register misleading or
            # predictably unresolvable targets.
            dns_label = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
            if len(hostname) > 253 or any(
                dns_label.fullmatch(label) is None for label in hostname.split(".")
            ):
                raise ValueError(error)
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    if parsed_port is not None and parsed_port != default_port:
        netloc = f"{netloc}:{parsed_port}"
    # URL fragments are resolved by browsers and never sent to the monitored
    # server, so retaining one would create duplicate records for one site.
    # A root path is implicit even when a query string is present. Both URL
    # spellings produce the same HTTP request target, so persist one form.
    path = "" if parsed.path == "/" else parsed.path
    # urllib's HTTP request layer requires an ASCII URI. Convert printable IRI
    # path and query characters to UTF-8 percent escapes while preserving
    # existing validated escapes and each component's RFC delimiters. urlsplit
    # keeps semicolon parameters in the path, including an empty delimiter.
    # This also gives raw-Unicode and already-encoded spellings one site identity.
    path = quote(path, safe="/:@-._~!$&'()*+,;=%")
    # A dot is an ordinary unreserved query character and has no dot-segment
    # behavior outside the path. Decode its escaped spelling here so equivalent
    # query targets cannot create separate monitored site identities.
    query = re.sub(r"%2e", ".", parsed.query, flags=re.IGNORECASE)
    query = quote(query, safe="/?:@-._~!$&'()*+,;=%")
    normalized = urlunsplit((scheme, netloc, path, query, ""))
    if has_query_delimiter and not query:
        normalized += "?"
    # A bare host gains an implicit ``https://`` prefix, and IDNA conversion may
    # expand Unicode labels. Bound the canonical value that will actually be
    # persisted, not only the shorter operator-supplied spelling.
    if len(normalized) > MAX_SITE_URL_LENGTH:
        raise ValueError(f"Site URL must be {MAX_SITE_URL_LENGTH} characters or fewer.")
    return normalized


@dataclass(frozen=True)
class SiteCheck:
    name: str
    url: str
    http_status: int
    latency_ms: int
    ssl_days_remaining: int
    wordpress_version: str
    update_count: int
    backup_age_hours: int
    security_headers: dict[str, str]
    score: int
    status: str
    summary: str
    actions: list[str]
    checked_at: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["security_headers"] = dict(self.security_headers)
        return data


def status_from_score(score: int) -> str:
    return "green" if score >= 85 else ("yellow" if score >= 65 else "red")


def evaluate_site(
    name: str,
    url: str,
    http_status: int,
    latency_ms: int,
    ssl_days_remaining: int,
    wordpress_version: str,
    update_count: int,
    backup_age_hours: int,
    security_headers: dict[str, str] | None = None,
) -> SiteCheck:
    if http_status != 0 and not 100 <= http_status <= 599:
        raise ValueError("http_status must be 0 or between 100 and 599.")
    for field, value in (
        ("latency_ms", latency_ms),
        ("ssl_days_remaining", ssl_days_remaining),
        ("update_count", update_count),
        ("backup_age_hours", backup_age_hours),
    ):
        if value < 0:
            raise ValueError(f"{field} must not be negative.")
    name = normalize_site_name(name)
    wordpress_version = normalize_wordpress_version(wordpress_version)
    headers = {k.lower(): v for k, v in (security_headers or {}).items()}
    score = 100
    actions: list[str] = []
    if http_status < 200 or http_status >= 400:
        score -= 45
        actions.append(f"Investigate uptime: HTTP status is {http_status}.")
    if latency_ms > 1200:
        score -= 10
        actions.append(f"Improve performance: homepage response time is {latency_ms} ms.")
    if ssl_days_remaining < 14:
        score -= 25
        actions.append(f"Renew SSL certificate: only {ssl_days_remaining} day(s) remaining.")
    elif ssl_days_remaining <= 30:
        score -= 10
        actions.append(f"Plan SSL renewal: {ssl_days_remaining} day(s) remaining.")
    if update_count > 0:
        score -= min(20, update_count * 3)
        actions.append(f"Apply WordPress/plugin/theme updates: {update_count} pending updates.")
    if backup_age_hours > 72:
        score -= 20
        actions.append(f"Verify backups: latest backup appears {backup_age_hours} hours old.")
    elif backup_age_hours > 36:
        score -= 8
        actions.append(f"Check backup freshness: latest backup is {backup_age_hours} hours old.")
    if "strict-transport-security" not in headers:
        score -= 4
        actions.append("Add or verify HSTS security header.")
    if "x-frame-options" not in headers and "content-security-policy" not in headers:
        score -= 4
        actions.append("Add clickjacking protection header.")
    score = max(0, min(100, score))
    status = status_from_score(score)
    if status == "green":
        summary = f"{name} looks healthy. Minor recommendations can be handled during normal maintenance."
    elif status == "yellow":
        summary = f"{name} is stable but has maintenance items to schedule."
    else:
        summary = f"{name} needs attention before the next client report."
    return SiteCheck(
        name,
        normalize_site_url(url),
        http_status,
        latency_ms,
        ssl_days_remaining,
        wordpress_version,
        update_count,
        backup_age_hours,
        headers,
        score,
        status,
        summary,
        actions,
        datetime.now(timezone.utc).isoformat(),
    )


def ssl_days_remaining(url: str, timeout: int = 10) -> int:
    parsed = urlparse(normalize_site_url(url))
    host = parsed.hostname
    if not host:
        return 0
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, parsed.port or 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return max(0, int((expires - datetime.now(timezone.utc)).total_seconds() // 86400))
    except Exception:
        return 0


def fetch_basic_site_check(name: str, url: str, timeout: int = 10) -> SiteCheck:
    name = normalize_site_name(name)
    url = normalize_site_url(url)
    effective_url = url
    started = time.monotonic()
    status = 0
    headers: dict[str, str] = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WP FleetOps/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            headers = dict(resp.headers.items())
            effective_url = normalize_site_url(resp.geturl())
    except urllib.error.HTTPError as exc:
        # HTTPError still represents a completed HTTP response. Preserve its
        # status and headers so operators see the actual server-side failure.
        status = exc.code
        headers = dict(exc.headers.items()) if exc.headers else {}
        effective_url = normalize_site_url(exc.geturl())
    except Exception:
        status = 0
    latency_ms = int((time.monotonic() - started) * 1000)
    # urllib follows redirects. Inspect the certificate at the effective HTTPS
    # destination so an HTTP-to-HTTPS redirect does not look like an expired
    # certificate, while retaining the operator-configured URL in the record.
    effective_scheme = urlparse(effective_url).scheme
    ssl_days = ssl_days_remaining(effective_url, timeout=timeout) if effective_scheme == "https" else 0
    return evaluate_site(name, url, status, latency_ms, ssl_days, "unknown", 0, 0, headers)


def summarize_care_report(checks: list[SiteCheck]) -> str:
    total = len(checks)
    green = sum(1 for c in checks if c.status == "green")
    yellow = sum(1 for c in checks if c.status == "yellow")
    red = sum(1 for c in checks if c.status == "red")
    lines = [
        "# Monthly WordPress Care Report",
        "",
        f"Sites reviewed: {total}",
        f"Healthy: {green} | Maintenance: {yellow} | Needs attention: {red}",
        "",
    ]
    for c in checks:
        heading = "Healthy" if c.status == "green" else ("Maintenance scheduled" if c.status == "yellow" else "Needs attention")
        lines += [f"## {c.name} — {heading}", "", f"Score: {c.score}/100", f"URL: {c.url}", c.summary, ""]
        if c.actions:
            lines.append("Recommended actions:")
            lines += [f"- {a}" for a in c.actions]
            lines.append("")
    return "\n".join(lines).strip() + "\n"
