import asyncio
import json

import pytest

from app.core.events import EventBroker


@pytest.mark.asyncio
async def test_publish_reaches_subscriber():
    broker = EventBroker()
    broker.bind_loop(asyncio.get_running_loop())
    q = broker.subscribe()

    broker.publish("status", {"status": "running"})
    await asyncio.sleep(0)  # let call_soon_threadsafe run

    payload = await asyncio.wait_for(q.get(), timeout=1)
    obj = json.loads(payload)
    assert obj == {"type": "status", "data": {"status": "running"}}


@pytest.mark.asyncio
async def test_publish_is_noop_without_subscribers():
    broker = EventBroker()
    broker.bind_loop(asyncio.get_running_loop())
    # No subscribers -- must not raise, must not schedule anything.
    broker.publish("log", {"message": "hi"})
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_publish_before_loop_bound_is_silently_dropped():
    broker = EventBroker()
    q = broker.subscribe()
    broker.publish("status", {"x": 1})  # loop not bound yet
    await asyncio.sleep(0)
    assert q.empty()


@pytest.mark.asyncio
async def test_slow_subscriber_drops_oldest_not_newest():
    broker = EventBroker()
    broker.bind_loop(asyncio.get_running_loop())
    q = broker.subscribe()

    # Fill well past the queue's maxsize.
    from app.core.events import _QUEUE_MAXSIZE

    for i in range(_QUEUE_MAXSIZE + 10):
        broker.publish("n", {"i": i})
    await asyncio.sleep(0)

    seen = []
    while not q.empty():
        seen.append(json.loads(q.get_nowait())["data"]["i"])

    assert len(seen) == _QUEUE_MAXSIZE
    # The most recent event must have survived; the earliest must have been dropped.
    assert seen[-1] == _QUEUE_MAXSIZE + 9
    assert 0 not in seen


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    broker = EventBroker()
    broker.bind_loop(asyncio.get_running_loop())
    q = broker.subscribe()
    broker.unsubscribe(q)

    broker.publish("status", {"x": 1})
    await asyncio.sleep(0)
    assert q.empty()
    assert broker.subscriber_count == 0
