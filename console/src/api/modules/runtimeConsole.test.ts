import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  RUNTIME_MANAGED_CHANNEL,
  runtimeConsoleApi,
  runtimeConversationToChatHistory,
  runtimeSummaryToChatSpec,
} from "./runtimeConsole";

describe("runtimeConsoleApi", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
  });

  it("keeps only the external identity in this browser tab", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            data: {
              connected: true,
              identity: { user_id: "u001", org_id: "org001" },
            },
            trace_id: "trace-test",
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );

    await runtimeConsoleApi.connect("u001", "org001");

    expect(runtimeConsoleApi.isConnected()).toBe(true);
    expect(runtimeConsoleApi.currentIdentity()).toEqual({
      userId: "u001",
      orgId: "org001",
    });
    expect(localStorage.getItem("qwenpaw-runtime-external-identity-v1")).toBeNull();
  });

  it("sends external identity only to the fixed QwenPaw proxy", async () => {
    sessionStorage.setItem(
      "qwenpaw-runtime-external-identity-v1",
      JSON.stringify({ userId: "u001", orgId: "org001" }),
    );
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await runtimeConsoleApi.listConversations();

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/runtime-console/conversations?page=1&page_size=100",
    );
    expect(fetchMock.mock.calls[0][1].headers).toMatchObject({
      "X-Runtime-External-User-Id": "u001",
      "X-Runtime-External-Org-Id": "org001",
    });
  });

  it("clears only the Runtime identity when the proxy rejects it", async () => {
    sessionStorage.setItem(
      "qwenpaw-runtime-external-identity-v1",
      JSON.stringify({ userId: "u001", orgId: "org001" }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Runtime identity required" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(runtimeConsoleApi.listConversations()).rejects.toThrow(
      "Runtime identity required",
    );
    expect(runtimeConsoleApi.isConnected()).toBe(false);
  });
});

describe("Runtime conversation mapping", () => {
  it("uses a collision-safe id and marks Runtime history read-only", () => {
    const chat = runtimeSummaryToChatSpec({
      conversation_id: "conv-001",
      title: "制度总结",
      status: "active",
      last_task_status: "completed",
      turn_count: 2,
      created_at: "2026-08-03T12:00:00+08:00",
      updated_at: "2026-08-03T12:01:00+08:00",
    });

    expect(chat.id).toBe("runtime:conv-001");
    expect(chat.channel).toBe(RUNTIME_MANAGED_CHANNEL);
    expect(chat.meta).toMatchObject({
      runtimeManaged: true,
      runtimeConversationId: "conv-001",
      readOnly: true,
    });
  });

  it("maps Runtime turns to ordinary QwenPaw messages", () => {
    const history = runtimeConversationToChatHistory({
      conversation_id: "conv-001",
      status: "completed",
      turns: [
        {
          turn_id: "turn-1",
          conversation_id: "conv-001",
          task_id: "task-1",
          role: "user",
          content: "请总结制度",
          created_at: "2026-08-03T12:00:00+08:00",
        },
        {
          turn_id: "turn-2",
          conversation_id: "conv-001",
          task_id: "task-1",
          role: "assistant",
          content: "制度摘要",
          created_at: "2026-08-03T12:01:00+08:00",
        },
      ],
    });

    expect(history.messages).toHaveLength(2);
    expect(history.messages[0]).toMatchObject({
      role: "user",
      content: [{ type: "text", text: "请总结制度" }],
    });
    expect(history.messages[1]).toMatchObject({
      type: "message",
      role: "assistant",
      content: [{ type: "text", text: "制度摘要" }],
    });
  });
});
