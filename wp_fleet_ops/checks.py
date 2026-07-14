from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
import socket
import ssl
import time
import urllib.request
from urllib.parse import urlparse, urlunparse


def normalize_site_url(url: str) -> str:
    candidate = url.strip()
    error = "Site URL must be a valid HTTP or HTTPS URL."
    if not candidate or any(char.isspace() for char in candidate):
        raise ValueError(error)
    if "://" not in candidate:
        explicit_scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate)
        host_with_port = re.match(r"^[^/:\s]+:\d+(?:/|$)", candidate)
        if explicit_scheme and not host_with_port:
            raise ValueError(error)
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError(error) from exc
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in netloc)
    ):
        raise ValueError(error)
    if parsed_port is not None and not 1 <= parsed_port <= 65535:
        raise ValueError(error)
    default_port = 443 if scheme == "https" else 80
    if parsed_port == default_port:
        hostname = parsed.hostname.lower()
        netloc = f"[{hostname}]" if ":" in hostname else hostname
    # URL fragments are resolved by browsers and never sent to the monitored
    # server, so retaining one would create duplicate records for one site.
    path = "" if parsed.path == "/" and not parsed.query else parsed.path
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


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
    elif ssl_days_remaining < 30:
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
    url = normalize_site_url(url)
    parsed = urlparse(url)
    started = time.monotonic()
    status = 0
    headers: dict[str, str] = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "WP FleetOps/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            headers = dict(resp.headers.items())
    except Exception:
        status = 0
    latency_ms = int((time.monotonic() - started) * 1000)
    ssl_days = ssl_days_remaining(url, timeout=timeout) if parsed.scheme == "https" else 0
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
