"""Dedicated channel for trusted Bank Runtime ingress."""

from __future__ import annotations

from qwenpaw.app.channels.console.channel import ConsoleChannel
from qwenpaw.app.channels.renderer import ChannelDisplayConfig


class BankRuntimeChannel(ConsoleChannel):
    """Console-compatible executor isolated under a distinct channel key."""

    channel = "bank-runtime"
    uses_manager_queue = False

    @classmethod
    def from_config(
        cls,
        process,
        config,
        on_reply_sent=None,
        display_config=None,
        no_text_debounce=True,
        workspace_dir=None,
    ):
        del no_text_debounce
        return cls(
            process=process,
            enabled=bool(getattr(config, "enabled", False)),
            bot_prefix=str(getattr(config, "bot_prefix", "") or ""),
            on_reply_sent=on_reply_sent,
            display_config=display_config or ChannelDisplayConfig.from_config(config),
            workspace_dir=workspace_dir,
            media_dir=str(getattr(config, "media_dir", "") or ""),
        )
