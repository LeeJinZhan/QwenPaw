"""Bank Runtime managed chats must stay outside the QwenPaw control plane."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from qwenpaw.app.chats.api import (
    BatchChatIds,
    ProjectDirectoryUpdate,
    archive_chat,
    batch_archive_chats,
    batch_delete_chats,
    batch_unarchive_chats,
    create_chat,
    clear_chat_project_dir,
    delete_chat,
    get_chat,
    get_chat_project_dir,
    list_chats,
    set_chat_project_dir,
    unarchive_chat,
    update_chat,
)
from qwenpaw.app.chats.models import ChatSpec, ChatUpdate


def _chat(chat_id: str, channel: str) -> ChatSpec:
    return ChatSpec(
        id=chat_id,
        name=f"chat-{chat_id}",
        session_id=f"session-{chat_id}",
        user_id="user-a",
        channel=channel,
    )


class _Tracker:
    async def get_status(self, _chat_id: str) -> str:
        return "idle"


class _Manager:
    def __init__(self) -> None:
        self.visible = _chat("visible", "console")
        self.protected = _chat("protected", "bank-runtime")
        self.mutations: list[tuple[str, object]] = []

    async def list_chats(self, **_kwargs):
        return [self.visible, self.protected]

    async def get_chat(self, chat_id: str):
        return {
            self.visible.id: self.visible,
            self.protected.id: self.protected,
        }.get(chat_id)

    async def create_chat(self, spec: ChatSpec):
        self.mutations.append(("create", spec.id))
        return spec

    async def patch_chat(self, chat_id: str, _spec: ChatUpdate):
        self.mutations.append(("update", chat_id))
        return await self.get_chat(chat_id)

    async def delete_chats(self, chat_ids: list[str]):
        self.mutations.append(("delete", tuple(chat_ids)))
        return True

    async def archive_chat(self, chat_id: str, **_kwargs):
        self.mutations.append(("archive", chat_id))
        return await self.get_chat(chat_id)

    async def unarchive_chat(self, chat_id: str):
        self.mutations.append(("unarchive", chat_id))
        return await self.get_chat(chat_id)

    async def batch_archive(self, *, chat_ids: list[str], **_kwargs):
        self.mutations.append(("batch_archive", tuple(chat_ids)))
        return {"succeeded": chat_ids, "failed": []}

    async def batch_unarchive(self, *, chat_ids: list[str]):
        self.mutations.append(("batch_unarchive", tuple(chat_ids)))
        return {"succeeded": chat_ids, "failed": []}

    async def set_project_dir(self, chat_id: str, project_dir: str | None):
        self.mutations.append(("project_dir", (chat_id, project_dir)))
        return await self.get_chat(chat_id)


class _Session:
    async def get_session_state_dict(self, *_args, **_kwargs):
        return {
            "agent": {
                "memory": {
                    "content": "sensitive managed conversation content",
                },
            },
        }


@pytest.fixture
def manager() -> _Manager:
    return _Manager()


@pytest.fixture
def workspace() -> SimpleNamespace:
    return SimpleNamespace(
        task_tracker=_Tracker(),
        config=SimpleNamespace(backend="qwenpaw"),
    )


@pytest.mark.asyncio
async def test_list_chats_hides_bank_runtime_managed_sessions(
    manager: _Manager,
    workspace: SimpleNamespace,
) -> None:
    result = await list_chats(mgr=manager, workspace=workspace)

    assert [chat.id for chat in result] == ["visible"]


@pytest.mark.asyncio
async def test_chat_detail_treats_bank_runtime_session_as_not_found(
    manager: _Manager,
    workspace: SimpleNamespace,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_chat(
            "protected",
            mgr=manager,
            session=_Session(),
            workspace=workspace,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_create_rejects_bank_runtime_channel(manager: _Manager) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await create_chat(manager.protected, mgr=manager)

    assert exc_info.value.status_code == 403
    assert manager.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["update", "delete", "archive", "unarchive"],
)
async def test_single_mutations_cannot_touch_bank_runtime_sessions(
    operation: str,
    manager: _Manager,
    workspace: SimpleNamespace,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        if operation == "update":
            await update_chat(
                "protected",
                ChatUpdate(name="changed"),
                mgr=manager,
            )
        elif operation == "delete":
            await delete_chat("protected", mgr=manager, workspace=workspace)
        elif operation == "archive":
            await archive_chat("protected", mgr=manager, workspace=workspace)
        else:
            await unarchive_chat("protected", mgr=manager)

    assert exc_info.value.status_code == 404
    assert manager.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "set", "clear"])
async def test_project_directory_routes_cannot_touch_bank_runtime_sessions(
    operation: str,
    manager: _Manager,
    workspace: SimpleNamespace,
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        if operation == "get":
            await get_chat_project_dir(
                "protected",
                mgr=manager,
                workspace=workspace,
            )
        elif operation == "set":
            await set_chat_project_dir(
                "protected",
                ProjectDirectoryUpdate(project_dir="/tmp"),
                mgr=manager,
                workspace=workspace,
            )
        else:
            await clear_chat_project_dir(
                "protected",
                mgr=manager,
                workspace=workspace,
            )

    assert exc_info.value.status_code == 404
    assert manager.mutations == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["delete", "archive", "unarchive"],
)
async def test_batch_mutations_fail_closed_when_bank_runtime_session_is_present(
    operation: str,
    manager: _Manager,
    workspace: SimpleNamespace,
) -> None:
    protected_ids = ["visible", "protected"]

    with pytest.raises(HTTPException) as exc_info:
        if operation == "delete":
            await batch_delete_chats(
                protected_ids,
                mgr=manager,
                workspace=workspace,
            )
        elif operation == "archive":
            await batch_archive_chats(
                BatchChatIds(chat_ids=protected_ids),
                mgr=manager,
                workspace=workspace,
            )
        else:
            await batch_unarchive_chats(
                BatchChatIds(chat_ids=protected_ids),
                mgr=manager,
            )

    assert exc_info.value.status_code == 404
    assert manager.mutations == []
