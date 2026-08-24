import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import api, { type ChatSpec } from "../../../api";
import sessionApi from "../sessionApi";

const NATIVE_CHAT: ChatSpec = {
  id: "native-001",
  session_id: "console:default",
  user_id: "default",
  channel: "console",
  name: "QwenPaw test chat",
  created_at: "2026-08-24T10:00:00+08:00",
  updated_at: "2026-08-24T10:01:00+08:00",
  status: "idle",
  pinned: false,
};

describe("Runtime-managed conversations in the native Chat page", () => {
  beforeEach(() => {
    sessionApi.resetForTests();
    sessionStorage.setItem(
      "qwenpaw-runtime-external-identity-v1",
      JSON.stringify({ userId: "u001", orgId: "org001" }),
    );
    vi.spyOn(api, "listChats").mockResolvedValue([NATIVE_CHAT]);
    vi.spyOn(api, "getChat").mockRejectedValue(
      new Error("native detail must not be used for Runtime history"),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/runtime-console/conversations/runtime-conv-001")) {
          return new Response(
            JSON.stringify({
              conversation_id: "runtime-conv-001",
              title: "Runtime history",
              status: "completed",
              turns: [
                {
                  turn_id: "turn-1",
                  conversation_id: "runtime-conv-001",
                  role: "assistant",
                  content: "Runtime answer",
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            items: [
              {
                conversation_id: "runtime-conv-001",
                title: "Runtime history",
                status: "completed",
                turn_count: 1,
                created_at: "2026-08-24T09:00:00+08:00",
                updated_at: "2026-08-24T09:01:00+08:00",
              },
            ],
            total: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }),
    );
  });

  afterEach(() => {
    sessionApi.resetForTests();
    sessionStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("merges Runtime history into the existing list and reads detail through Runtime", async () => {
    const sessions = await sessionApi.getSessionList();

    expect(sessions.map((session) => session.id)).toEqual(
      expect.arrayContaining(["native-001", "runtime:runtime-conv-001"]),
    );

    const runtimeSession = await sessionApi.getSession(
      "runtime:runtime-conv-001",
    );
    const runtimeContext = runtimeSession as typeof runtimeSession & {
      channel: string;
      meta: Record<string, unknown>;
    };
    expect(runtimeContext.channel).toBe("bank-runtime");
    expect(runtimeContext.meta).toMatchObject({
      runtimeManaged: true,
      readOnly: true,
    });
    expect(runtimeSession.messages).toHaveLength(1);
    expect(api.getChat).not.toHaveBeenCalled();
  });
});
