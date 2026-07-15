"""Verifies the structured-concurrency contract in main.py's TaskGroup usage directly
(without spinning up the real Telegram/aiohttp stack): a real subsystem failure cancels
its siblings and propagates visibly, while cooperative cancellation (as triggered by a
shutdown signal) does not."""

import asyncio

import pytest


async def _forever() -> None:
    await asyncio.Event().wait()


async def _boom() -> None:
    await asyncio.sleep(0)
    raise RuntimeError("subsystem crashed")


def test_child_failure_cancels_siblings_and_raises_exception_group() -> None:
    async def run() -> None:
        async with asyncio.TaskGroup() as tg:
            good = tg.create_task(_forever())
            tg.create_task(_boom())
            await asyncio.sleep(0)  # let _boom() run and fail
            # `good` will be cancelled by the TaskGroup as part of teardown; nothing
            # else to assert here beyond the exception below.
            del good

    with pytest.raises(ExceptionGroup) as exc_info:
        asyncio.run(run())
    assert any(isinstance(e, RuntimeError) for e in exc_info.value.exceptions)


def test_explicit_task_cancellation_exits_cleanly_without_raising() -> None:
    async def run() -> None:
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(_forever()), tg.create_task(_forever())]
            await asyncio.sleep(0)
            for t in tasks:
                t.cancel()

    # A signal-driven shutdown that cancels every child directly must NOT raise —
    # CancelledError from an externally-cancelled task is not treated as a subsystem
    # failure by TaskGroup, so main_async() returns normally on graceful shutdown.
    asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
