"""Small, bounded HTTP reader for untrusted public source URLs."""

import http.client
import ipaddress
import queue
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

from .jobda_logos import JOBDA_CONFIG_URL, JOBDA_DESIGN_URL, JOBDA_IMAGE_URL, JOBDA_TENANT

_WORKERS = threading.BoundedSemaphore(4)
_REDIRECTS = {301, 302, 303, 307, 308}
_TRANSLATED = tuple(map(ipaddress.ip_network, ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")))


class PublicHTTPError(OSError):
    def __init__(self, status):
        super().__init__(f"Public source returned HTTP {status}")
        self.status = status


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Public request timed out")
    return remaining


def _endpoint(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) <= 32 or c == "\\" for c in url):
        raise ValueError("Invalid public URL")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None:
        raise ValueError("Invalid public URL")
    if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
        raise ValueError("Only standard public HTTP ports are supported")
    return parsed


def _addresses(parsed):
    host = parsed.hostname.encode("idna").decode("ascii")
    if "%" in host:
        raise ValueError("Invalid public host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not records:
        raise OSError("Public host has no address")
    for _, _, _, _, address in records:
        ip = ipaddress.ip_address(address[0])
        if (not ip.is_global or ip.is_multicast or ip.is_reserved
                or getattr(ip, "ipv4_mapped", None) or any(ip in network for network in _TRANSLATED)):
            raise OSError("Public host resolved to a non-public address")
    return records


def _request(parsed, records, deadline, limit, jobda_tenant):
    client = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    host = parsed.hostname.encode("idna").decode("ascii")
    with_client = client(host, port=parsed.port, timeout=_remaining(deadline))

    def connect_address(_host, _timeout, _source):
        # Connect to the checked address directly while preserving the original HTTP host and TLS name.
        for index, (family, kind, protocol, _, address) in enumerate(records):
            sock = socket.socket(family, kind, protocol)
            try:
                sock.settimeout(min(_remaining(deadline), 2))
                sock.connect(address)
                sock.settimeout(_remaining(deadline))
                return sock
            except OSError:
                sock.close()
                if index + 1 == len(records):
                    raise

    with_client._create_connection = connect_address
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        headers = {"User-Agent": "Mozilla/5.0 (compatible; PersonalJobReader/0.1)", "Accept-Encoding": "identity"}
        if jobda_tenant is not None:
            headers["prefix"] = jobda_tenant
        with_client.request("GET", path, headers=headers)
        with with_client.getresponse() as response:
            headers = {name.lower(): value for name, value in response.getheaders()}
            if response.status != 200:
                return response.status, headers, b""
            encoding = headers.get("content-encoding", "identity").lower()
            # JOBDA labels raw JSON and raster bytes as Content-Encoding: UTF-8 on its design CDN.
            url = parsed.geturl()
            if encoding != "identity" and not (encoding == "utf-8" and
                    (JOBDA_CONFIG_URL.fullmatch(url) or JOBDA_IMAGE_URL.fullmatch(url))):
                raise OSError("Unsupported public response encoding")
            expected = response.length
            if expected is not None and expected > limit:
                raise OSError("Public response exceeds size limit")
            body = bytearray()
            while True:
                _remaining(deadline)
                piece = response.read1(min(65536, limit + 1 - len(body)))
                if not piece:
                    break
                body.extend(piece)
                if len(body) > limit:
                    raise OSError("Public response exceeds size limit")
            if expected is not None and len(body) != expected:
                raise OSError("Truncated public response")
            return response.status, headers, bytes(body)
    finally:
        with_client.close()


def _fetch(url, deadline, limit, jobda_tenant):
    for _ in range(4):
        _remaining(deadline)
        target = _endpoint(url)
        status, headers, body = _request(target, _addresses(target), deadline, limit, jobda_tenant)
        if status == 200:
            return url, body, headers.get("content-type", "").partition(";")[0].lower()
        if status not in _REDIRECTS or not headers.get("location"):
            raise PublicHTTPError(status)
        url = urljoin(url, headers["location"])
        jobda_tenant = None
    raise OSError("Too many redirects from public source")


def fetch_public(url, *, deadline, limit, jobda_tenant=None):
    """Enforce the caller's total budget even when the OS resolver stalls."""
    if jobda_tenant is not None and (url != JOBDA_DESIGN_URL or not isinstance(jobda_tenant, str)
                                     or not JOBDA_TENANT.fullmatch(jobda_tenant)):
        raise ValueError("Invalid JOBDA tenant design request")
    _remaining(deadline)
    if not _WORKERS.acquire(blocking=False):
        raise TimeoutError("Public requests are busy")
    result = queue.Queue(maxsize=1)

    def read():
        try:
            result.put((True, _fetch(url, deadline, limit, jobda_tenant)))
        except http.client.HTTPException as error:
            result.put((False, OSError(str(error))))
        except Exception as error:
            result.put((False, error))
        finally:
            _WORKERS.release()

    worker = threading.Thread(target=read, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _WORKERS.release()
        raise
    try:
        ok, value = result.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise TimeoutError("Public request timed out") from None
    if not ok:
        raise value
    return value
