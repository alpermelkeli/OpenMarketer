"""Fetching one public page safely: HTTPS only, robots.txt, size and type limits, no private hosts.

The researcher's ``fetch_page`` action goes through this module. What it
refuses, it refuses in code, before anything is read: an address that is not
``https``; a host that resolves to a private, loopback, link-local, reserved or
multicast address (so a page can never aim the worker at the installation's own
network or a metadata service); a host whose ``robots.txt`` disallows this
crawler's user agent; a page that is not ``text/html``, ``text/plain`` or
``application/xhtml+xml``; a body over ``MAX_HTML_BYTES``, which is refused
while it is being read, never downloaded whole. Redirects are followed at most
``MAX_REDIRECTS`` times, each hop vetted again the same way, and the robots
file of every host a hop reaches is read first.

When plain HTTP returns a page that looks like a script shell (little text and
scripts), and a renderer is configured, the page is rendered with the
installation's headless browser instead; a render that fails leaves the page as
HTTP returned it. What comes back is bytes and nothing else: they are stored by
``page_store.py`` and read by ``page_text.plain_text``, and are never executed,
opened or trusted.

Every refusal is ``FetchRefused`` (the same URL will be refused again) and
every temporary failure is ``FetchUnavailable`` (the same URL may work later);
a caller decides per failure whether the run goes on without the page.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from openmarketer_core.research.page_render import PageRenderer, RenderUnavailable
from openmarketer_core.research.page_text import plain_text

logger = logging.getLogger(__name__)

# How this crawler introduces itself; robots rules are asked about this name.
USER_AGENT = "OpenMarketer/0.1 (+https://github.com/alpermelkeli/OpenMarketer)"
MAX_URL_CHARS = 2048
MAX_REDIRECTS = 3
MAX_HTML_BYTES = 2 * 1024 * 1024
# A robots.txt is small; one over this is taken for something else and refused.
MAX_ROBOTS_BYTES = 256 * 1024
ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# Below this much text, an HTML page that also loads scripts is taken for a script shell and
# rendered with the headless browser. A short static page may be rendered unnecessarily; a
# long page never is.
MIN_STATIC_TEXT_CHARS = 200
DEFAULT_TIMEOUT_S = 30.0


class FetchRefused(Exception):
    """The page may not be fetched: not HTTPS, a private host, robots.txt, a type or size."""


class FetchUnavailable(Exception):
    """The page could not be fetched now: the network, a timeout, an answering failure."""


@dataclass(frozen=True)
class FetchedPage:
    """One page as it was fetched: its final address, its type and its bytes."""

    url: str  # after any redirects
    content_type: str
    body: bytes
    rendered: bool  # whether the headless browser produced it


class PageFetcher(Protocol):
    """What the researcher's fetch action needs: one address in, one page out."""

    def fetch(self, url: str) -> FetchedPage: ...


class PublicPageFetcher:
    """Fetching public pages, with the guards of this module's docstring."""

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        renderer: PageRenderer | None = None,
        resolve: Callable[[str], list[str]] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        """``transport`` and ``resolve`` are for tests; production uses DNS and the network."""
        self._transport = transport
        self._renderer = renderer
        self._resolve = resolve or _addresses_of
        self._timeout_s = timeout_s
        # A robots file per host, for the life of this fetcher; a run and its attempts share
        # one fetcher, so one host is asked once however many of its pages a run reads.
        self._robots: dict[str, RobotFileParser | None] = {}

    def fetch(self, url: str) -> FetchedPage:
        """The page at ``url``, or ``FetchRefused``/``FetchUnavailable``.

        The returned address is where redirects led, which is the address the
        page is stored under and told to a reading model.
        """
        checked = self._vetted(url)
        self._require_robots(checked)
        page = self._follow(checked)
        if self._renderer is None or page.content_type != "text/html":
            return page
        if not _looks_like_a_script_shell(page.body):
            return page
        try:
            rendered = self._renderer.render(page.url)
        except RenderUnavailable as e:
            # Rendering is an enhancement: the page as HTTP returned it is still the page.
            logger.warning("a page was not rendered, HTTP's answer is used: %s", e)
            return page
        body = rendered.encode()
        if len(body) > MAX_HTML_BYTES:
            raise FetchRefused("the rendered page is larger than this crawler fetches")
        return FetchedPage(url=page.url, content_type="text/html", body=body, rendered=True)

    # ------------------------------------------------------------------ guards
    def _vetted(self, url: str) -> str:
        if len(url) > MAX_URL_CHARS:
            raise FetchRefused("the address is longer than this crawler fetches")
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise FetchRefused("only https addresses are fetched")
        if not parts.hostname:
            raise FetchRefused("the address names no host")
        self._require_public(parts.hostname)
        return url

    def _require_public(self, host: str) -> None:
        """Refuse a host that is, or resolves to, anything but a public address."""
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            addresses = self._resolve(host)
            if not addresses:
                raise FetchUnavailable(f"the host {host} does not resolve") from None
        else:
            addresses = [str(literal)]
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                raise FetchUnavailable("the host resolved to something unreadable") from None
            if not _is_public(parsed):
                raise FetchRefused(f"the host {host} is not a public one")

    def _require_robots(self, url: str) -> None:
        """Refuse ``url`` when the host's robots.txt disallows this crawler."""
        host = urlsplit(url).hostname or ""
        if host not in self._robots:
            self._robots[host] = self._read_robots(host)
        parser = self._robots[host]
        if parser is not None and not parser.can_fetch(USER_AGENT, url):
            raise FetchRefused(f"robots.txt of {host} disallows this crawler on this page")

    def _read_robots(self, host: str) -> RobotFileParser | None:
        """The host's robots, parsed; ``None`` when the host has none. Raises when unreadable."""
        answer = self._get(
            f"https://{host}/robots.txt", limit=MAX_ROBOTS_BYTES, what="the robots.txt of this host"
        )
        if answer.status_code >= 400:
            if answer.status_code < 500:
                # 4xx (404 among them): no robots.txt, and everything is allowed (RFC 9309).
                return None
            raise FetchUnavailable(f"the robots.txt of {host} answered HTTP {answer.status_code}")
        parser = RobotFileParser()
        parser.parse(answer.body.decode(errors="replace").splitlines())
        return parser

    # ------------------------------------------------------------------ fetching
    def _follow(self, url: str) -> FetchedPage:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            answer = self._get(
                current,
                limit=MAX_HTML_BYTES,
                what="the page",
                types=ALLOWED_CONTENT_TYPES,
            )
            if answer.status_code in REDIRECT_STATUSES:
                location = answer.headers.get("location")
                if not location:
                    raise FetchUnavailable("the page redirected without saying where")
                current = self._vetted(urljoin(current, location))
                self._require_robots(current)
                continue
            if answer.status_code == 200:
                return FetchedPage(
                    url=current,
                    content_type=answer.content_type,
                    body=answer.body,
                    rendered=False,
                )
            if 400 <= answer.status_code < 500:
                raise FetchRefused(f"the page answered HTTP {answer.status_code}")
            raise FetchUnavailable(f"the page answered HTTP {answer.status_code}")
        raise FetchRefused("the page redirected more often than this crawler follows")

    def _get(
        self,
        url: str,
        *,
        limit: int,
        what: str,
        types: tuple[str, ...] | None = None,
    ) -> _Answer:
        try:
            with httpx.Client(
                timeout=self._timeout_s,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as response:
                    content_type = response.headers.get("content-type", "")
                    content_type = content_type.split(";")[0].strip().lower()
                    if response.status_code == 200 and types is not None:
                        if content_type not in types:
                            raise FetchRefused(
                                f"{what} is not text ({content_type[:60] or 'no type'})"
                            )
                    if response.status_code != 200:
                        # Nothing of a failed or redirecting answer is kept, not even its body.
                        return _Answer(response.status_code, dict(response.headers), b"")
                    body = self._read_at_most(response, limit, what)
                    return _Answer(response.status_code, dict(response.headers), body)
        except httpx.HTTPError as e:
            raise FetchUnavailable(f"{what} could not be reached ({type(e).__name__})") from e

    def _read_at_most(self, response: httpx.Response, limit: int, what: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise FetchRefused(f"{what} is larger than this crawler fetches")
            chunks.append(chunk)
        return b"".join(chunks)


@dataclass(frozen=True)
class _Answer:
    """One HTTP answer: what it answered, its headers, and at most the bounded body."""

    status_code: int
    headers: dict[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


def _addresses_of(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return []
    return sorted({str(info[4][0]) for info in infos})


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _looks_like_a_script_shell(body: bytes) -> bool:
    page = body.decode(errors="replace")
    return "<script" in page.lower() and len(plain_text(page)) < MIN_STATIC_TEXT_CHARS
