"""Typed control transfer for a plugin-owned durable suspension."""
from __future__ import annotations


class RuntimeSuspension(BaseException):
    """Unwind an execution after its durable checkpoint was acknowledged.

    This is not a tool failure. A framework catching ordinary Exception must
    not convert it into model feedback and continue a suspended execution.
    """
    def __init__(self, checkpoint_id: str, waiting_child_task_ids: list[str], *, waiting_inputs: bool = False) -> None:
        super().__init__("Execution suspended")
        self.checkpoint_id = checkpoint_id
        self.waiting_child_task_ids = tuple(waiting_child_task_ids)
        self.waiting_inputs = waiting_inputs
