"""Message-level atomic blocks and the ordered block store.

Each message added to the kernel becomes exactly one block; blocks are the
atomic unit of compression. staffDeck has no standard function calling, so
block boundaries are message-level rather than tool-call/result pairs.
"""

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Block:
    """An immutable message-level block.

    ``is_summary`` marks blocks produced by compression; their original
    content lives in the checkpoint record (``checkpoint_id``) so the
    visible state stays small while recovery stays possible.
    """

    block_id: int
    message_id: str
    content: str
    tier: int = 1
    is_summary: bool = False
    checkpoint_id: int | None = None
    skip: bool = False


class BlockStore:
    """Ordered collection of blocks with stable, monotonically increasing ids."""

    def __init__(self) -> None:
        self._blocks: list[Block] = []
        self._next_id = 1

    def add(self, message_id: str, content: str, *, skip: bool = False) -> Block:
        block = Block(
            block_id=self._next_id,
            message_id=message_id,
            content=content,
            skip=skip,
        )
        self._next_id += 1
        self._blocks.append(block)
        return block

    def add_many(self, messages: Sequence[tuple[str, str]]) -> list[Block]:
        return [self.add(message_id, content) for message_id, content in messages]

    def get(self, block_id: int) -> Block | None:
        for block in self._blocks:
            if block.block_id == block_id:
                return block
        return None

    def all(self) -> tuple[Block, ...]:
        return tuple(self._blocks)

    def slice(self, seq_start: int, seq_end: int) -> list[Block]:
        return self._blocks[seq_start : seq_end + 1]

    def replace(self, seq_start: int, seq_end: int, new_block: Block) -> list[Block]:
        removed = self._blocks[seq_start : seq_end + 1]
        self._blocks[seq_start : seq_end + 1] = [new_block]
        return removed

    def replace_by_id(self, block_id: int, replacement: Sequence[Block]) -> Block | None:
        for index, block in enumerate(self._blocks):
            if block.block_id == block_id:
                removed = self._blocks[index]
                self._blocks[index : index + 1] = list(replacement)
                return removed
        return None

    def next_id(self) -> int:
        return self._next_id

    def __len__(self) -> int:
        return len(self._blocks)