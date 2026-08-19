"""Fail-closed placeholder for the future Runtime ingress channel."""

from __future__ import annotations

from qwenpaw.app.channels.base import BaseChannel


class BankRuntimeChannel(BaseChannel):
    """Registration marker; Task 5 supplies the real ingress behavior."""

    channel = "bank-runtime"
    uses_manager_queue = False

    @classmethod
    def from_config(cls, *args, **kwargs):
        raise RuntimeError(
            "Bank Runtime ingress is disabled until its protocol is installed",
        )
