"""Content safety: what happens when the model's safety filters block a message or an answer.

Adapters raise ContentBlocked; the core records it, and the API answers with a polite refusal
instead. A block is logged and recorded by stage and category only, never with the text.
"""

from typing import Literal

Stage = Literal["input", "output"]


class ContentBlocked(Exception):
    def __init__(self, stage: Stage, categories: tuple[str, ...]) -> None:
        super().__init__(f"{stage}: {', '.join(categories) or 'unspecified'}")
        self.stage = stage
        self.categories = categories
