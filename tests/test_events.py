"""EventHub observability: a full subscriber queue drops (never blocks) and that drop
is now counted + the live subscriber count is tracked, so a slow client is visible to
operators instead of silently desyncing."""
import asyncio

from llm_wiki import metrics
from llm_wiki.events import EventHub


def test_full_queue_drops_event_and_counts_it():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    q.put_nowait({"seq": 1})                       # fill to capacity
    before = metrics.WS_EVENTS_DROPPED._value.get()
    EventHub._offer(q, {"seq": 2})                 # must not raise
    after = metrics.WS_EVENTS_DROPPED._value.get()
    assert after == before + 1
    assert q.qsize() == 1 and q.get_nowait() == {"seq": 1}  # original kept, new one dropped


def test_subscriber_gauge_tracks_subscribe_unsubscribe():
    hub = EventHub()
    q = hub.subscribe()
    assert metrics.WS_SUBSCRIBERS._value.get() == 1
    hub.unsubscribe(q)
    assert metrics.WS_SUBSCRIBERS._value.get() == 0
