"""Security primitives: SSRF protection, URL validation, safe filenames.

Two layers protect against server-side request forgery:

1. `validate_url` checks the scheme, port, credentials, host name and every
   address the host currently resolves to.
2. `egress_guard` patches name resolution while a download or extraction is
   running so that redirects and DNS rebinding can only ever connect to public
   addresses. Network clients that bypass Python's `socket` module (for example
   curl-impersonation backends) are NOT covered; keep them uninstalled and use
   network-level egress rules in production as well.
"""
import ipaddress
import re
import socket
import threading
import unicodedata
from contextlib import contextmanager
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from .errors import AppError, ErrorCode

ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {None, 80, 443}
BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet", ".private")
BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal", "instance-data", "kubernetes.default.svc"}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTO4 = ipaddress.ip_network("2002::/16")

_REAL_GETADDRINFO = socket.getaddrinfo
_GUARD_LOCK = threading.Lock()
_GUARD_DEPTH = 0


def is_public_ip(addr) -> bool:
    """True only for globally routable addresses (IPv4-mapped / NAT64 / 6to4 unwrapped)."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            return is_public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        elif ip in _SIXTO4:
            return is_public_ip(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF)) and ip.is_global
    return ip.is_global and not ip.is_multicast


def _guarded_getaddrinfo(*args, **kwargs):
    results = _REAL_GETADDRINFO(*args, **kwargs)
    if _GUARD_DEPTH <= 0:
        return results
    safe = [r for r in results if is_public_ip(r[4][0])]
    if not safe:
        raise socket.gaierror(socket.EAI_NONAME, "non-public address blocked")
    return safe


socket.getaddrinfo = _guarded_getaddrinfo


@contextmanager
def egress_guard():
    """While active, every name resolution may only yield public IP addresses."""
    global _GUARD_DEPTH
    with _GUARD_LOCK:
        _GUARD_DEPTH += 1
    try:
        yield
    finally:
        with _GUARD_LOCK:
            _GUARD_DEPTH -= 1


def guard_depth() -> int:
    return _GUARD_DEPTH


def _invalid(detail: str) -> AppError:
    return AppError(ErrorCode.INVALID_URL, detail=detail)


def clean_user_input(raw: str) -> str:
    """Strip whitespace and the markdown/bracket characters that get copied along with links."""
    text = (raw or "").strip()
    chars = "[]()<>\\\"'"
    while True:  # strip wrappers and trailing punctuation that arrive with copied links, in any order
        new = text.strip(chars).rstrip(".,;:!").strip()
        if new == text:
            return text
        text = new


def resolve_public(host: str, port: int = 443):
    """Resolve `host` and require every returned address to be public."""
    try:
        infos = _REAL_GETADDRINFO(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _invalid(f"dns failure for {host}: {exc}")
    addrs = [i[4][0] for i in infos]
    if not addrs:
        raise _invalid("no addresses")
    for a in addrs:
        if not is_public_ip(a):
            raise _invalid(f"{host} resolves to non-public address {a}")
    return addrs


def normalize_url(raw: str) -> str:
    """Parse, validate syntactically and normalise a URL. Does not touch the network."""
    cleaned = clean_user_input(raw)
    if not cleaned or len(cleaned) > 2048 or any(c in cleaned for c in "\r\n\t\x00 "):
        raise _invalid("empty, too long or contains whitespace/control characters")
    try:
        parts = urlsplit(cleaned)
        port = parts.port
    except ValueError as exc:
        raise _invalid(f"unparseable: {exc}")
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise _invalid(f"scheme {scheme!r} not allowed")
    if parts.username or parts.password:
        raise _invalid("credentials in url")
    if port not in ALLOWED_PORTS:
        raise _invalid(f"port {port} not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise _invalid("missing host")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if not is_ip:
        if host in BLOCKED_HOSTS or host.endswith(BLOCKED_HOST_SUFFIXES) or "." not in host:
            raise _invalid(f"host {host} blocked")
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise _invalid("bad idna host")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if scheme == "https" else 80):
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def validate_url(raw: str) -> str:
    """Full validation: syntax, blocked names, and DNS resolution to public addresses only."""
    url = normalize_url(raw)
    parts = urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        ipaddress.ip_address(host)
        if not is_public_ip(host):
            raise _invalid(f"non-public ip literal {host}")
    except ValueError:
        resolve_public(host, port)
    return url


_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_filename(name: str, default: str = "save-hack", ext: str = "", max_len: int = 120) -> str:
    """Return a filesystem- and header-safe file name. Never contains path separators."""
    name = unicodedata.normalize("NFKC", name or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem or len(suffix) > 8 or not re.fullmatch(r"[A-Za-z0-9]+", suffix):
        stem, suffix = name, ""
    suffix = suffix.lower() or ext.lstrip(".").lower()
    stem = stem.strip(" .") or default
    if stem.lower() in _RESERVED:
        stem = "_" + stem
    suffix = re.sub(r"[^a-z0-9]", "", suffix)
    room = max_len - (len(suffix) + 1 if suffix else 0)
    stem = stem.encode("utf-8")[:max(room, 1)].decode("utf-8", "ignore").rstrip(" .") or default
    return f"{stem}.{suffix}" if suffix else stem


def filename_from_url(url: str) -> str:
    path = urlsplit(url).path
    return PurePosixPath(path).name or ""
