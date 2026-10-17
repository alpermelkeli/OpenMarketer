"""Tests for the server-sent events stream: framing, polling a reader, keep-alive and its end.

The reader is a plain function over a list, and the client's connection a function the
test answers, so nothing here needs a server or a database.
"""

import json

import anyio
import pytest

from openmarketer_api.event_stream import (
    OpenStreams,
    StreamedEvent,
    StreamEnded,
    StreamTiming,
    TooManyStreams,
    comment,
    event_stream,
    frame,
    json_line,
    retry_hint,
)

QUICK = StreamTiming(poll_every=0.01, keep_alive_every=60, close_after=0.05, reconnect_after=2)
NEVER_ENDING = StreamTiming(poll_every=60, keep_alive_every=60, close_after=60)


def event(event_id: int, kind: str = "tool_called") -> StreamedEvent:
    return StreamedEvent(id=event_id, event=kind, data=f'{{"id": {event_id}}}')


async def still_connected() -> bool:
    return False


class Reads:
    """A reader that hands out prepared batches, then ends the stream, recording each cursor."""

    def __init__(self, *batches: list[StreamedEvent], end: bool = True) -> None:
        self.batches = list(batches)
        self.end = end
        self.cursors: list[int | None] = []

    def __call__(self, cursor: int | None) -> list[StreamedEvent]:
        self.cursors.append(cursor)
        if self.batches:
            return self.batches.pop(0)
        if self.end:
            raise StreamEnded
        return []


def streamed(reads, *, after=None, timing=NEVER_ENDING, disconnected=still_connected) -> list[str]:
    async def collect() -> list[str]:
        with anyio.fail_after(5):
            stream = event_stream(reads, after=after, timing=timing, disconnected=disconnected)
            return [chunk async for chunk in stream]

    return anyio.run(collect)


def test_an_event_is_framed_with_its_id_its_name_and_its_data():
    assert frame(event(7)) == 'id: 7\nevent: tool_called\ndata: {"id": 7}\n\n'


@pytest.mark.parametrize("data", ["one\ntwo", "one\rtwo", "one\0two"])
def test_data_of_more_than_one_line_is_refused(data):
    with pytest.raises(ValueError, match="one line"):
        frame(StreamedEvent(id=1, event="note", data=data))


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\x0b", "\x1e"])
def test_a_title_with_a_character_that_splits_lines_stays_in_one_data_line(separator):
    title = f"Pricing{separator}page"

    framed = frame(StreamedEvent(id=1, event="fetched", data=json_line({"title": title})))

    [data] = [line for line in framed.split("\n") if line.startswith("data: ")]
    assert separator not in framed
    assert json.loads(data.removeprefix("data: ")) == {"title": title}


def test_a_process_keeps_at_most_its_limit_of_streams_open():
    streams = OpenStreams(limit=2)
    streams.take()
    streams.take()

    with pytest.raises(TooManyStreams):
        streams.take()


def test_a_stream_that_gives_its_place_back_twice_frees_one_place():
    streams = OpenStreams(limit=2)
    place = streams.take()
    streams.take()

    place.give_back()
    place.give_back()

    assert streams.count == 1


@pytest.mark.parametrize("name", ["tool\ncalled", "tool\rcalled", "tool\0called"])
def test_an_event_name_of_more_than_one_line_is_refused(name):
    with pytest.raises(ValueError, match="one line"):
        frame(StreamedEvent(id=1, event=name, data="{}"))


def test_a_comment_and_the_retry_hint_are_framed_as_the_format_says():
    assert comment("keep-alive") == ": keep-alive\n\n"
    assert retry_hint(1.5) == "retry: 1500\n\n"


def test_a_stream_starts_with_its_retry_hint_and_sends_events_in_order():
    chunks = streamed(Reads([event(1), event(2)]))

    assert chunks == ["retry: 1000\n\n", frame(event(1)), frame(event(2))]


def test_each_read_continues_after_the_last_event_sent():
    reads = Reads([event(4)], [event(5), event(6)])

    streamed(reads, after=3)

    assert reads.cursors == [3, 4, 6]


def test_a_stream_without_a_cursor_reads_from_the_first_event():
    reads = Reads()

    streamed(reads)

    assert reads.cursors == [None]


def test_a_backlog_is_read_again_at_once_without_waiting_for_the_next_poll():
    reads = Reads([event(1)], [event(2)], [event(3)])

    chunks = streamed(reads, timing=NEVER_ENDING)

    assert chunks[1:] == [frame(event(1)), frame(event(2)), frame(event(3))]


def test_events_stored_while_the_stream_waits_are_sent_by_a_later_read():
    reads = Reads([], [], [event(9)])

    chunks = streamed(reads, timing=StreamTiming(poll_every=0.01, close_after=5))

    assert chunks[-1] == frame(event(9))
    assert reads.cursors == [None, None, None, 9]


def test_an_idle_stream_sends_keep_alive_comments():
    reads = Reads(end=False)
    idle = StreamTiming(poll_every=0.01, keep_alive_every=0.02, close_after=0.2)

    chunks = streamed(reads, timing=idle)

    assert chunks.count(": keep-alive\n\n") >= 2
    assert all(chunk in ("retry: 1000\n\n", ": keep-alive\n\n") for chunk in chunks)


def test_a_stream_that_sends_events_sends_no_keep_alive():
    reads = Reads([event(1)], [event(2)])

    chunks = streamed(reads, timing=StreamTiming(poll_every=0.01, keep_alive_every=0))

    assert ": keep-alive\n\n" not in chunks


def test_a_stream_ends_when_its_time_is_up():
    reads = Reads(end=False)

    chunks = streamed(reads, timing=QUICK)

    assert chunks[0] == "retry: 2000\n\n"
    assert 1 < len(reads.cursors) < 50


def test_a_stream_stops_reading_once_the_client_has_gone():
    reads = Reads([event(1)], [event(2)], end=False)
    answers = iter([False, True])

    async def disconnected() -> bool:
        return next(answers)

    chunks = streamed(reads, disconnected=disconnected)

    assert chunks == ["retry: 1000\n\n", frame(event(1))]
    assert reads.cursors == [None]
