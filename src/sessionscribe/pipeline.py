"""Round-robin consumption of durable guild queues, outside the recorder process."""

from __future__ import annotations

from collections.abc import Callable


class PipelineScheduler:
    def __init__(self, consumers: tuple[Callable[[], bool], ...]):
        self.consumers, self.cursor = consumers, 0

    def once(self) -> bool:
        for _ in self.consumers:
            consumer = self.consumers[self.cursor]
            self.cursor = (self.cursor + 1) % len(self.consumers)
            if consumer():
                return True
        return False
