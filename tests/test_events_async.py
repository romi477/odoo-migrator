"""A stream waits on the event loop, not on a thread of its own."""

import asyncio
import threading

from odoo_migrator.events import CLOSED, EventHub


def test_an_async_subscriber_hears_a_publish_from_another_thread():
    hub = EventHub()

    async def listen():
        heard = hub.subscribe_async()
        threading.Thread(target=hub.publish, args=("module", {"id": 1})).start()
        item = await asyncio.wait_for(heard.get(), 2)
        hub.unsubscribe(heard)

        return item

    assert asyncio.run(listen()) == ("module", {"id": 1})


def test_close_wakes_an_async_subscriber():
    hub = EventHub()

    async def listen():
        heard = hub.subscribe_async()
        threading.Thread(target=hub.close).start()

        return await asyncio.wait_for(heard.get(), 2)

    assert asyncio.run(listen()) is CLOSED


def test_many_waiting_streams_take_no_thread_each():
    """asyncio.to_thread parked one executor thread per open page: past
    min(32, cpu + 4) tabs a new stream's hello waited on a heartbeat."""
    hub = EventHub()

    async def listen():
        queues = [hub.subscribe_async() for _ in range(64)]
        waiting = [asyncio.create_task(q.get()) for q in queues]
        await asyncio.sleep(0.05)
        before = threading.active_count()
        hub.publish("lamp", {"state": "idle"})
        done = await asyncio.wait_for(asyncio.gather(*waiting), 2)

        return before, done

    before, done = asyncio.run(listen())
    assert len(done) == 64 and before < 10
