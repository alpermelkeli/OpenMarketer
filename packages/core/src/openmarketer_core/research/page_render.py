"""Rendering a page with a headless browser, when plain HTTP returns a script shell.

Some public pages return an empty document with scripts that fill it in. A
``PageRenderer`` turns such a page's address into the DOM the browser built:
``CdpPageRenderer`` speaks just enough of the Chrome DevTools Protocol over a
websocket to open a target, navigate, wait for the load event and read
``document.documentElement.outerHTML``. The browser is the installation's own —
the dev stack's Lightpanda (``lightpanda serve``), which speaks the same
protocol — and no page, script or cookie is kept: every render opens a fresh
target on a browser that loads no profile, and the connection closes after.

What a rendered page holds is untrusted exactly as a fetched one is; nothing
here parses or runs it. Every way a render can fail (a browser that is down, a
protocol that answers something unexpected, a page that never loads) becomes
``RenderUnavailable``; the caller decides whether to fall back to the page
plain HTTP returned or to leave the page out.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol

import httpx
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as websocket_connect

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0


class RenderUnavailable(Exception):
    """The browser could not be used: it did not answer, or refused the work."""


class _Socket(Protocol):
    """What the renderer needs of a websocket: send a message, receive one, time out."""

    def send(self, message: str) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...
    def close(self) -> None: ...


class PageRenderer(Protocol):
    """What fetching needs when a page looks like a script shell: its address in, a DOM out."""

    def render(self, url: str) -> str: ...


class CdpPageRenderer:
    """A headless browser over CDP, on the installation's own address."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        connect: Callable[[str], AbstractContextManager[_Socket]] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        """``transport`` and ``connect`` are for tests; production uses the network."""
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._timeout_s = timeout_s
        self._connect = connect or (
            lambda url: websocket_connect(url, open_timeout=timeout_s, close_timeout=5)
        )

    def render(self, url: str) -> str:
        """The rendered HTML of ``url``, or ``RenderUnavailable``.

        The page is loaded with JavaScript executed; nothing is clicked, typed
        or submitted, and the target is closed when the render is done.
        """
        websocket_url = self._websocket_url()
        try:
            with self._connect(websocket_url) as socket:
                return _Conversation(socket, self._timeout_s).render(url)
        except RenderUnavailable:
            raise
        except (OSError, TimeoutError, WebSocketException, json.JSONDecodeError, KeyError) as e:
            raise RenderUnavailable(f"the browser could not be used ({type(e).__name__})") from e

    def _websocket_url(self) -> str:
        try:
            with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
                response = client.get(f"{self._base_url}/json/version")
        except httpx.HTTPError as e:
            raise RenderUnavailable(f"the browser is unreachable ({type(e).__name__})") from e
        if response.status_code != 200:
            raise RenderUnavailable(f"the browser answered HTTP {response.status_code}")
        try:
            told = response.json().get("webSocketDebuggerUrl")
        except (ValueError, AttributeError) as e:
            raise RenderUnavailable("the browser did not say how to reach it") from e
        if not isinstance(told, str) or not told.startswith(("ws://", "wss://")):
            raise RenderUnavailable("the browser did not say how to reach it")
        return told


class _Conversation:
    """One CDP conversation: numbered calls, and the events seen on the way."""

    def __init__(self, socket: _Socket, timeout_s: float) -> None:
        self._socket = socket
        self._timeout_s = timeout_s
        self._next_id = 0
        self._events: list[dict[str, Any]] = []

    def render(self, url: str) -> str:
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        try:
            session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})[
                "sessionId"
            ]
            self.call("Page.enable", session=session)
            self.call("Page.navigate", {"url": url}, session=session)
            # Some pages never fire it within the timeout; their DOM is read as it is then.
            self.wait_for_event("Page.loadEventFired", session=session)
            dom = (
                self.call(
                    "Runtime.evaluate",
                    {"expression": "document.documentElement.outerHTML", "returnByValue": True},
                    session=session,
                )
                .get("result", {})
                .get("value")
            )
        finally:
            self._close_target(target)
        if not isinstance(dom, str) or not dom:
            raise RenderUnavailable("the browser returned no page")
        return dom

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None
    ) -> dict[str, Any]:
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        self._socket.send(json.dumps(message))
        expected = self._next_id
        self._next_id += 1
        while True:
            answer = self._read()
            if answer.get("id") == expected:
                if "error" in answer:
                    # The browser's own words go to the log; a run is shown none of them.
                    logger.warning("the browser refused %s: %r", method, answer["error"])
                    raise RenderUnavailable(f"the browser refused {method}")
                result = answer.get("result", {})
                if not isinstance(result, dict):
                    raise RenderUnavailable(f"the browser answered {method} unexpectedly")
                return result
            if "method" in answer:
                self._events.append(answer)

    def wait_for_event(self, method: str, *, session: str | None = None) -> bool:
        """Whether the event came within this conversation's timeout; drops it when it did."""
        for index, event in enumerate(self._events):
            if _is_event(event, method, session):
                del self._events[index]
                return True
        try:
            while True:
                answer = self._read()
                if _is_event(answer, method, session):
                    return True
                if "method" in answer:
                    self._events.append(answer)
        except TimeoutError:
            return False

    def _read(self) -> dict[str, Any]:
        answer = json.loads(self._socket.recv(timeout=self._timeout_s))
        if not isinstance(answer, dict):
            raise RenderUnavailable("the browser wrote something unexpected")
        return answer

    def _close_target(self, target: str) -> None:
        try:
            self.call("Target.closeTarget", {"targetId": target})
        except (RenderUnavailable, OSError, TimeoutError, WebSocketException):
            # Closing is a courtesy: the connection ends with the render either way.
            logger.debug("the browser did not close the render's target")


def _is_event(answer: dict[str, Any], method: str, session: str | None) -> bool:
    return answer.get("method") == method and (
        session is None or answer.get("sessionId") == session
    )
