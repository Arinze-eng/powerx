import { describe, expect, it } from "vitest";

import type { BrokerAccount } from "@/lib/api";
import { brokerConnectionLabel } from "@/lib/broker-connection";

function account(connection?: BrokerAccount["connection"]): BrokerAccount {
  return {
    broker: "Deriv",
    server: "Deriv-Demo",
    login: "41261482",
    is_default: true,
    connection,
  };
}

describe("broker connection status", () => {
  it("says an account that has never been used is not connected", () => {
    const label = brokerConnectionLabel(account());
    expect(label.tone).toBe("idle");
    expect(label.text).toContain("Not connected yet");
  });

  it("treats an empty status the same as a missing one", () => {
    const label = brokerConnectionLabel(
      account({ status: "", connected: false, message: "", at: "" }),
    );
    expect(label.tone).toBe("idle");
  });

  it("reports a stored account that never logged in as not connected, not as connected", () => {
    // The whole point of the field: `has_password` is not a login.
    const label = brokerConnectionLabel(account());
    expect(label.text).not.toContain("Connected");
  });

  it("shows when the last successful login was", () => {
    const label = brokerConnectionLabel(
      account({
        status: "connected",
        connected: true,
        message: "",
        at: "2026-10-17T07:00:00+00:00",
      }),
    );
    expect(label.tone).toBe("connected");
    expect(label.text).toContain("Connected");
    expect(label.text).toContain("2026");
  });

  it("still reports a connection whose timestamp is unreadable", () => {
    const label = brokerConnectionLabel(
      account({ status: "connected", connected: true, message: "", at: "not-a-date" }),
    );
    expect(label.tone).toBe("connected");
    expect(label.text).toBe("Connected");
  });

  it("carries the terminal's reason for a failed login, truncated in the row", () => {
    const reason = "x".repeat(400);
    const label = brokerConnectionLabel(
      account({
        status: "failed",
        connected: false,
        message: reason,
        at: "2026-10-17T07:00:00+00:00",
      }),
    );
    expect(label.tone).toBe("failed");
    expect(label.text).toContain("last login failed");
    expect(label.text.length).toBeLessThan(260);
    // The row shows a clipped reason; the tooltip keeps the whole one.
    expect(label.detail).toContain(reason);
  });

  it("reports a failure with no reason at all", () => {
    const label = brokerConnectionLabel(
      account({ status: "failed", connected: false, message: "", at: "" }),
    );
    expect(label.tone).toBe("failed");
    expect(label.text).toContain("last login failed");
  });
});
