import { describe, expect, it } from "vitest";

import {
  applyToolEvents,
  condenseLine,
  summarizeToolCall,
  tradeRows,
  type SandboxActivityRow,
} from "@/lib/sandbox-activity";
import type { ToolProgressEvent } from "@/lib/types";

const T0 = 1_700_000_000_000;

function event(overrides: Partial<ToolProgressEvent>): ToolProgressEvent {
  return { version: 1, phase: "start", ...overrides };
}

describe("summarizeToolCall", () => {
  it("renders an exec command as a plain terminal line", () => {
    const summary = summarizeToolCall("exec", { command: "ls -la /workspace" });
    expect(summary).toEqual({ kind: "command", line: "ls -la /workspace" });
  });

  it("accepts the cmd alias the exec tool also publishes", () => {
    const summary = summarizeToolCall("exec", { cmd: "pwd" });
    expect(summary?.line).toBe("pwd");
  });

  it("keeps a multi-line command on one row rather than stopping at the first line", () => {
    // The first line of an agent's shell command is routinely `cd ... &&`, so a
    // row that kept only that line would say something ran and not what.
    const summary = summarizeToolCall("exec", {
      command: "\n\n  cd /workspace &&\n  python3 train.py\n",
    });
    expect(summary?.line).toBe("cd /workspace && python3 train.py");
  });

  it("elides a command too long to render on one line", () => {
    const summary = summarizeToolCall("exec", { command: "x".repeat(600) });
    expect(summary).not.toBeNull();
    expect(summary!.line.length).toBeLessThanOrEqual(220);
    expect(summary!.line.endsWith("…")).toBe(true);
  });

  it("marks a market order as a trade, with side and symbol before the size", () => {
    const summary = summarizeToolCall("mt5_sandbox", {
      action: "order",
      side: "buy",
      symbol: "EURUSD",
      volume: 0.01,
    });
    expect(summary).toEqual({ kind: "trade", line: "mt5 order buy EURUSD 0.01" });
  });

  it("marks close, modify and guard as trades too", () => {
    for (const action of ["close", "close_all", "modify", "split", "cancel", "guard"]) {
      const summary = summarizeToolCall("mt5_sandbox", { action });
      expect(summary?.kind, action).toBe("trade");
    }
  });

  it("does not call a read-only MT5 action a trade", () => {
    for (const action of ["quote", "positions", "account", "candles", "watch", "history"]) {
      const summary = summarizeToolCall("mt5_sandbox", { action });
      expect(summary?.kind, action).toBe("sandbox");
    }
  });

  it("names the ticket on a modify so a guard change is identifiable", () => {
    const summary = summarizeToolCall("mt5_sandbox", {
      action: "modify",
      ticket: 4737451282,
      exit_at: 1.15,
    });
    expect(summary?.line).toBe("mt5 modify exit_at=1.15 #4737451282");
  });

  it("marks an order opened without a stop, because that is the risky case", () => {
    const summary = summarizeToolCall("mt5_sandbox", {
      action: "order",
      side: "sell",
      symbol: "XAUUSD",
      volume: 0.1,
      allow_no_stop: true,
    });
    expect(summary?.line).toBe("mt5 order sell XAUUSD 0.1 (no stop)");
  });

  it("renders a hosted-sandbox tool, which has no command argument", () => {
    const summary = summarizeToolCall("novita_sandbox", { action: "install" });
    expect(summary).toEqual({ kind: "sandbox", line: "novita_sandbox install" });
  });

  it("renders a python tool as one condensed source line", () => {
    const summary = summarizeToolCall("python_code", { code: "import pandas as pd\nprint(1)" });
    expect(summary?.line).toBe("python_code: import pandas as pd print(1)");
  });

  it("ignores a tool that never reaches a sandbox", () => {
    expect(summarizeToolCall("web_search", { query: "eurusd" })).toBeNull();
    expect(summarizeToolCall("filesystem", { path: "/tmp/x" })).toBeNull();
    expect(summarizeToolCall("message", { text: "hi" })).toBeNull();
  });

  it("ignores an empty tool name rather than inventing a row", () => {
    expect(summarizeToolCall("", { command: "ls" })).toBeNull();
  });

  it("renders a bare command on a tool whose name is not on the list", () => {
    const summary = summarizeToolCall("some_new_backend", { command: "whoami" });
    expect(summary).toEqual({ kind: "command", line: "whoami" });
  });
});

describe("condenseLine", () => {
  it("collapses runs of whitespace so one command is one row", () => {
    expect(condenseLine("a    b\t\tc")).toBe("a b c");
  });

  it("returns an empty string for whitespace-only input", () => {
    expect(condenseLine("   \n  ")).toBe("");
  });
});

describe("applyToolEvents", () => {
  it("opens a row on a start frame and closes it on the matching end frame", () => {
    const started = applyToolEvents(
      [],
      [event({ phase: "start", call_id: "a", name: "exec", arguments: { command: "ls" } })],
      T0,
      200,
    );
    expect(started).toHaveLength(1);
    expect(started[0].status).toBe("running");
    expect(started[0].endedAt).toBeNull();

    const ended = applyToolEvents(
      started,
      [event({ phase: "end", call_id: "a", name: "exec", arguments: { command: "ls" }, result: "ok" })],
      T0 + 500,
      200,
    );
    expect(ended).toHaveLength(1);
    expect(ended[0].status).toBe("ok");
    expect(ended[0].endedAt).toBe(T0 + 500);
    expect(ended[0].detail).toBe("ok");
  });

  it("keeps the agent's own line, not the result's, when a call finishes", () => {
    const rows = applyToolEvents(
      applyToolEvents(
        [],
        [event({ phase: "start", call_id: "a", name: "mt5_sandbox", arguments: { action: "order", side: "buy", symbol: "EURUSD", volume: 0.01 } })],
        T0,
        200,
      ),
      [event({ phase: "end", call_id: "a", name: "mt5_sandbox", arguments: { action: "order", side: "buy", symbol: "EURUSD", volume: 0.01 }, result: { retcode: 10009, fill_price: 1.13975, order: 4737451282 } })],
      T0 + 900,
      200,
    );
    expect(rows[0].line).toBe("mt5 order buy EURUSD 0.01");
    expect(rows[0].detail).toBe("retcode 10009 · @ 1.13975 · order 4737451282");
    expect(rows[0].kind).toBe("trade");
  });

  it("marks a failed call as an error and carries the reason", () => {
    const rows = applyToolEvents(
      applyToolEvents(
        [],
        [event({ phase: "start", call_id: "a", name: "exec", arguments: { command: "make" } })],
        T0,
        200,
      ),
      [event({ phase: "error", call_id: "a", name: "exec", arguments: { command: "make" }, error: "No such file: Makefile" })],
      T0 + 100,
      200,
    );
    expect(rows[0].status).toBe("error");
    expect(rows[0].detail).toBe("No such file: Makefile");
  });

  it("ignores a tool call that never touched a sandbox", () => {
    const rows = applyToolEvents(
      [],
      [
        event({ phase: "start", call_id: "w", name: "web_search", arguments: { query: "x" } }),
        event({ phase: "start", call_id: "e", name: "exec", arguments: { command: "ls" } }),
      ],
      T0,
      200,
    );
    expect(rows.map((row) => row.callId)).toEqual(["e"]);
  });

  it("does not open a second row for a replayed start frame", () => {
    const once = applyToolEvents(
      [],
      [event({ phase: "start", call_id: "a", name: "exec", arguments: { command: "ls" } })],
      T0,
      200,
    );
    const twice = applyToolEvents(
      once,
      [event({ phase: "start", call_id: "a", name: "exec", arguments: { command: "ls" } })],
      T0 + 10,
      200,
    );
    expect(twice).toHaveLength(1);
    expect(twice).toBe(once);
  });

  it("records an end frame whose start was never seen, rather than dropping it", () => {
    // A reconnect can deliver a terminal frame on its own. Dropping it would
    // hide a command that ran, which is the one thing the feed must not do.
    const rows = applyToolEvents(
      [],
      [event({ phase: "end", call_id: "ghost", name: "exec", arguments: { command: "reboot" }, result: "done" })],
      T0,
      200,
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].line).toBe("reboot");
    expect(rows[0].status).toBe("ok");
  });

  it("does not reopen a closed row when a late end frame arrives", () => {
    const closed = applyToolEvents(
      applyToolEvents(
        [],
        [event({ phase: "start", call_id: "a", name: "exec", arguments: { command: "ls" } })],
        T0,
        200,
      ),
      [event({ phase: "end", call_id: "a", name: "exec", arguments: { command: "ls" }, result: "ok" })],
      T0 + 10,
      200,
    );
    const again = applyToolEvents(
      closed,
      [event({ phase: "error", call_id: "a", name: "exec", arguments: { command: "ls" }, error: "late" })],
      T0 + 20,
      200,
    );
    expect(again[0].status).toBe("ok");
  });

  it("keeps only the newest rows once the limit is reached", () => {
    let rows: SandboxActivityRow[] = [];
    for (let i = 0; i < 12; i += 1) {
      rows = applyToolEvents(
        rows,
        [event({ phase: "start", call_id: `c${i}`, name: "exec", arguments: { command: `echo ${i}` } })],
        T0 + i,
        5,
      );
    }
    expect(rows).toHaveLength(5);
    expect(rows[0].line).toBe("echo 7");
    expect(rows[4].line).toBe("echo 11");
  });

  it("returns the same array for an empty batch, so React does not re-render", () => {
    const rows = applyToolEvents(
      [],
      [event({ phase: "start", call_id: "a", name: "exec", arguments: { command: "ls" } })],
      T0,
      200,
    );
    expect(applyToolEvents(rows, [], T0, 200)).toBe(rows);
    expect(applyToolEvents(rows, undefined, T0, 200)).toBe(rows);
  });

  it("never mutates the rows it was given", () => {
    const first = applyToolEvents(
      [],
      [event({ phase: "start", call_id: "a", name: "exec", arguments: { command: "ls" } })],
      T0,
      200,
    );
    const snapshot = JSON.parse(JSON.stringify(first));
    applyToolEvents(first, [event({ phase: "end", call_id: "a", name: "exec", result: "ok" })], T0 + 5, 200);
    expect(first).toEqual(snapshot);
  });
});

describe("tradeRows", () => {
  it("separates the lines that moved money from the ones that only looked", () => {
    const rows = applyToolEvents(
      [],
      [
        event({ phase: "start", call_id: "q", name: "mt5_sandbox", arguments: { action: "quote", symbol: "EURUSD" } }),
        event({ phase: "start", call_id: "o", name: "mt5_sandbox", arguments: { action: "order", side: "buy", symbol: "EURUSD", volume: 0.01 } }),
        event({ phase: "start", call_id: "c", name: "mt5_sandbox", arguments: { action: "close_all" } }),
      ],
      T0,
      200,
    );
    expect(tradeRows(rows).map((row) => row.callId)).toEqual(["o", "c"]);
  });
});
