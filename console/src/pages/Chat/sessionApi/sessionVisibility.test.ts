import { describe, expect, it } from "vitest";
import type { ChatSpec } from "../../../api";
import { filterConsoleVisibleChats } from ".";

const chat = (id: string, channel: string): ChatSpec =>
  ({
    id,
    session_id: `session-${id}`,
    user_id: "default",
    channel,
    name: id,
  }) as ChatSpec;

describe("filterConsoleVisibleChats", () => {
  it("keeps native and ordinary channel history out of Runtime-owned history", () => {
    const visible = filterConsoleVisibleChats([
      chat("console-chat", "console"),
      chat("dingtalk-chat", "dingtalk"),
      chat("runtime-chat", "bank-runtime"),
    ]);

    expect(visible.map((item) => item.id)).toEqual([
      "console-chat",
      "dingtalk-chat",
    ]);
  });
});
