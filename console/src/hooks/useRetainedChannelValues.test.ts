import { describe, expect, it } from "vitest";
import { renderHook } from "@testing-library/react";
import { useRetainedChannelValues } from "./useRetainedChannelValues";
import { MCP_CHANNEL_SOURCE_VALUES } from "../pages/Agent/MCP/accessPolicy";

describe("channel selection recovery", () => {
  it("retains removed custom channels and accepts newly loaded channels", () => {
    const { result, rerender } = renderHook(
      ({ values }) => useRetainedChannelValues(values),
      { initialProps: { values: ["bank-runtime", "custom-channel"] } },
    );
    rerender({ values: ["bank-runtime"] });
    expect(result.current).toContain("custom-channel");
    rerender({ values: ["bank-runtime", "another-channel", "bank-runtime"] });
    expect(result.current).toEqual([
      "bank-runtime",
      "custom-channel",
      "another-channel",
    ]);
  });
  it("offers the platform channel in MCP rules", () => {
    expect(MCP_CHANNEL_SOURCE_VALUES).toContain("bank-runtime");
  });
});
