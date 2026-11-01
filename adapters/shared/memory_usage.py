"""UsageSink that keeps records in memory: for tests and local runs without a database."""

from core.usage import LLMUsage


class MemoryUsageSink:
    def __init__(self) -> None:
        self.records: list[LLMUsage] = []

    async def record(self, usage: LLMUsage) -> None:
        self.records.append(usage)
