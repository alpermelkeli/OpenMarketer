"""Server-sent events: a stream that polls a reader and frames what it returns.

A stream starts with a ``retry:`` hint, then asks its reader for the events
after the last one it sent, about every ``poll_every`` seconds, and sends each
as ``id``, ``event`` and ``data`` lines. While there is nothing to send it
writes a comment every ``keep_alive_every`` seconds, so a proxy does not take an
idle connection for a dead one, and it ends after ``close_after`` seconds: the
browser's ``EventSource`` then reconnects with the ``Last-Event-ID`` of the last
event it got, which is the cursor the next stream starts from. A bounded life
means no request holds the server for ever, whatever a client does.

The reader is a plain function run in a worker thread, so a blocking database
read never holds the event loop; it opens what it needs for each read and
keeps nothing open between reads. A reader raises ``StreamEnded`` when nothing
more will come (what it read from is gone): the stream then ends as if its time
were up. Between reads the stream asks whether the client has gone, since a
server on ASGI 2.4 or later only notices a closed connection when a write
fails, and an idle stream writes rarely.

An event's data is one line, and ``frame`` refuses one that is not. A browser
splits the stream only at CR and LF, but a data line split anywhere else (at
U+2028, say, which ``str.splitlines`` takes for a line break and JSON may leave
in a string as it is) would reach the browser joined with a LF inside a JSON
string, and the event would not read. A route writes its data as JSON in ASCII
(``json_line``), which escapes every character that is not, so no title a web
page gave itself can break the frame of its event.

Each open stream reads the database once a second and holds a worker thread
while it reads, so a process keeps at most ``MAX_OPEN_STREAMS`` of them open
(``OpenStreams``): one more is ``TooManyStreams``, which a client is answered 503
for and tries again after a while. A stream gives its place back when it ends,
however it ends.

This module knows nothing of what the events are or where they come from; a
route builds the reader and the data of each event.
"""

from __future__ import annotations

import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import anyio
from anyio import to_thread
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

EVENT_STREAM_MEDIA_TYPE = "text/event-stream"
# The most streams one process keeps open at once: each polls the database once a second.
MAX_OPEN_STREAMS = 20


@dataclass(frozen=True)
class StreamTiming:
    """How often a stream reads and writes, and how long it lasts, in seconds."""

    poll_every: float = 1.0
    keep_alive_every: float = 15.0
    close_after: float = 300.0
    # Sent as the stream's `retry:` hint: how long a browser waits before it reconnects.
    reconnect_after: float = 1.0


@dataclass(frozen=True)
class StreamedEvent:
    """One event as a stream sends it: its cursor, its name and one line of data."""

    id: int
    event: str
    data: str


class StreamEnded(Exception):
    """What the stream reads from is gone; nothing more will come."""


class TooManyStreams(Exception):
    """The process has as many streams open as it keeps; the client tries again later."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"{limit} event streams are open already; try again in a moment")


class StreamPlace:
    """One open stream's place among a process's: given back once, however often asked."""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._given_back = False
        self._lock = threading.Lock()

    def give_back(self) -> None:
        with self._lock:
            if self._given_back:
                return
            self._given_back = True
        self._release()


class OpenStreams:
    """The streams a process has open, at most ``limit`` at once."""

    def __init__(self, limit: int = MAX_OPEN_STREAMS) -> None:
        self._limit = limit
        self._open = 0
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        return self._open

    def take(self) -> StreamPlace:
        """A place for one more stream. Raises ``TooManyStreams`` when none is left."""
        with self._lock:
            if self._open >= self._limit:
                raise TooManyStreams(self._limit)
            self._open += 1
        return StreamPlace(self._leave)

    def _leave(self) -> None:
        with self._lock:
            self._open -= 1


class EventStreamResponse(StreamingResponse):
    """A response that is a stream of server-sent events, never cached or buffered.

    With ``place``, the stream gives it back when it ends: when its text ends, and after
    the response, for a stream that never started.
    """

    media_type = EVENT_STREAM_MEDIA_TYPE

    def __init__(self, content: AsyncIterator[str], *, place: StreamPlace | None = None) -> None:
        super().__init__(
            content if place is None else _giving_back(content, place),
            # A proxy that buffers (nginx does unless told not to) would hold events back.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=None if place is None else BackgroundTask(place.give_back),
        )


async def _giving_back(content: AsyncIterator[str], place: StreamPlace) -> AsyncIterator[str]:
    try:
        async for chunk in content:
            yield chunk
    finally:
        place.give_back()


def json_line(value: Any) -> str:
    """``value`` as JSON on one line of ASCII: what an event's data is written as."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def frame(event: StreamedEvent) -> str:
    """The lines of one event. Its name and its data are one line each (see the module)."""
    _check_one_line(event.event, "event")
    _check_one_line(event.data, "data")
    return f"id: {event.id}\nevent: {event.event}\ndata: {event.data}\n\n"


def comment(text: str) -> str:
    """A comment line, which a browser ignores and a proxy sees as traffic."""
    _check_one_line(text, "comment")
    return f": {text}\n\n"


def retry_hint(seconds: float) -> str:
    return f"retry: {round(seconds * 1000)}\n\n"


async def event_stream(
    read_after: Callable[[int | None], Sequence[StreamedEvent]],
    *,
    after: int | None,
    timing: StreamTiming,
    disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    """The text of a stream of the events after ``after``, until its time is up.

    ``read_after(cursor)`` returns the events after ``cursor`` in order (all of
    them from the first when ``cursor`` is ``None``); it is called again at once
    after a read that returned events, so a backlog is sent without waiting.
    """
    started = anyio.current_time()
    yield retry_hint(timing.reconnect_after)
    last_write = anyio.current_time()
    cursor = after
    while not await disconnected():
        try:
            events = await to_thread.run_sync(read_after, cursor)
        except StreamEnded:
            return
        for event in events:
            yield frame(event)
            cursor = event.id
        now = anyio.current_time()
        if events:
            last_write = now
        elif now - last_write >= timing.keep_alive_every:
            yield comment("keep-alive")
            last_write = now
        left = timing.close_after - (now - started)
        if left <= 0:
            return
        if not events:
            await anyio.sleep(min(timing.poll_every, left))


def _check_one_line(text: str, what: str) -> None:
    if any(character in text for character in "\r\n\0"):
        raise ValueError(f"an event stream's {what} is one line")
