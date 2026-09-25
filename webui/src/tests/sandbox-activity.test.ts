import { describe, expect, it } from "vitest";

import {
  applyToolEvents,
  asKind,
  condenseLine,
  destinationOf,
  statusFromEvent,
  summarizeToolCall,
  tradeRows,
  type SandboxActivityKind,
  type SandboxActivityRow,
} from "@/lib/sandbox-activity";
import type { ToolProgressEvent } from "@/lib/types";

const T0 = 1_700_000_000_000;

function event(overrides: Partial<ToolProgressEvent>): ToolProgressEvent {
  return { version: 1, phase: "start", ...overrides };
}

describe("asKind", () => {
  it("accepts the four kinds the host sends", () => {
    for (const kind of ["command", "trade", "nav", "sandbox"]) {
      expect(asKind(kind)).toBe(kind);
    }
  });

  it("rejects a kind this client has never heard of, and a missing one", () => {
    // A newer server naming a kind this build does not render must produce no
    // row. Casting instead would render `undefined` into the panel.
    expect(asKind("teleport")).toBeNull();
    expect(asKind(undefined)).toBeNull();
    expect(asKind(null)).toBeNull();
    expect(asKind(7)).toBeNull();
  });
});

describe("summarizeToolCall", () => {
  it("renders an exec command as a plain terminal line", () => {
    const summary = summarizeToolCall("exec", { command: "ls -la /workspace" }, "command");
    expect(summary).toEqual({ kind: "command", line: "ls -la /workspace" });
  });

  it("accepts the cmd alias the exec tool also publishes", () => {
    const summary = summarizeToolCall("exec", { cmd: "pwd" }, "command");
    expect(summary?.line).toBe("pwd");
  });

  it("keeps a multi-line command on one row rather than stopping at the first line", () => {
    // The first line of an agent's shell command is routinely `cd ... &&`, so a
    // row that kept only that line would say something ran and not what.
    const summary = summarizeToolCall(
      "exec",
      { command: "\n\n  cd /workspace &&\n  python3 train.py\n" },
      "command",
    );
    expect(summary?.line).toBe("cd /workspace && python3 train.py");
  });

  it("elides a command too long to render on one line", () => {
    const summary = summarizeToolCall("exec", { command: "x".repeat(600) }, "command");
    expect(summary).not.toBeNull();
    expect(summary.line.length).toBeLessThanOrEqual(220);
    expect(summary.line.endsWith("…")).toBe(true);
  });

  it("marks a market order as a trade, with side and symbol before the size", () => {
    const summary = summarizeToolCall(
      "mt5_sandbox",
      { action: "order", side: "buy", symbol: "EURUSD", volume: 0.01 },
      "trade",
    );
    expect(summary).toEqual({ kind: "trade", line: "mt5 order buy EURUSD 0.01" });
  });

  it("names the ticket on a modify so a guard change is identifiable", () => {
    const summary = summarizeToolCall(
      "mt5_sandbox",
      { action: "modify", ticket: 4737451282, exit_at: 1.15 },
      "trade",
    );
    expect(summary?.line).toBe("mt5 modify exit_at=1.15 #4737451282");
  });

  it("marks an order opened without a stop, because that is the risky case", () => {
    const summary = summarizeToolCall(
      "mt5_sandbox",
      { action: "order", side: "sell", symbol: "XAUUSD", volume: 0.1, allow_no_stop: true },
      "trade",
    );
    expect(summary?.line).toBe("mt5 order sell XAUUSD 0.1 (no stop)");
  });

  it("renders a read-only MT5 action as a sandbox row, not a trade", () => {
    const summary = summarizeToolCall("mt5_sandbox", { action: "quote", symbol: "XAUUSD" }, "sandbox");
    expect(summary).toEqual({ kind: "sandbox", line: "mt5 quote XAUUSD" });
  });

  it("renders a hosted-sandbox tool, which has no command argument", () => {
    const summary = summarizeToolCall("novita_sandbox", { action: "install" }, "sandbox");
    expect(summary).toEqual({ kind: "sandbox", line: "novita_sandbox install" });
  });

  it("renders a python tool as one condensed source line", () => {
    const summary = summarizeToolCall(
      "python_code",
      { code: "import pandas as pd\nprint(1)" },
      "sandbox",
    );
    expect(summary?.line).toBe("python_code: import pandas as pd print(1)");
  });

  it("names the tool and action when a sandbox call carries nothing renderable", () => {
    const summary = summarizeToolCall("long_task", { action: "status" }, "sandbox");
    expect(summary).toEqual({ kind: "sandbox", line: "long_task status" });
  });
});

describe("summarizeToolCall — browsing", () => {
  it("shows the destination host for a navigation, not a raw URL", () => {
    const summary = summarizeToolCall(
      "browser",
      { action: "navigate", url: "https://docs.deriv.com/api/trading" },
      "nav",
    );
    expect(summary).toEqual({ kind: "nav", line: "browser navigate → docs.deriv.com/api/trading" });
  });

  it("drops the query string, because that is where a token lives", () => {
    // These rows are broadcast to every WebUI client and persisted in the
    // transcript. A URL's query routinely carries a credential.
    const summary = summarizeToolCall(
      "browser",
      { action: "navigate", url: "https://app.example.com/login?token=abc123#step2" },
      "nav",
    );
    expect(summary?.line).toBe("browser navigate → app.example.com/login");
    expect(summary?.line).not.toContain("abc123");
  });

  it("shows what a click aimed at when there is no address", () => {
    const summary = summarizeToolCall("browser", { action: "click", target: "#submit" }, "nav");
    expect(summary).toEqual({ kind: "nav", line: "browser click #submit" });
  });

  it("falls back to the tool and action when a browsing call carries neither", () => {
    const summary = summarizeToolCall("browser", { action: "screenshot" }, "nav");
    expect(summary).toEqual({ kind: "nav", line: "browser screenshot" });
  });

  it("renders a fetch as a destination, which is the whole point of the call", () => {
    const summary = summarizeToolCall("web_fetch", { url: "https://example.com/a" }, "nav");
    expect(summary?.line).toBe("web_fetch → example.com/a");
  });

  it("reads the first entry of a page_urls list rather than the whole list", () => {
    const summary = summarizeToolCall(
      "mt5_sandbox",
      { action: "install", page_urls: ["https://broker.example.com/setup", "https://b.example.com"] },
      "nav",
    );
    expect(summary?.line).toBe("mt5_sandbox install → broker.example.com/setup");
  });

  it("shows a media download's host, which was one of the unregistered cases", () => {
    const summary = summarizeToolCall(
      "media_sandbox",
      { action: "download", url: "https://cdn.example.com/clip.mp4" },
      "nav",
    );
    expect(summary?.line).toBe("media_sandbox download → cdn.example.com/clip.mp4");
  });
});

describe("destinationOf", () => {
  it("keeps host and path and drops the scheme", () => {
    expect(destinationOf("https://a.example.com/x/y")).toBe("a.example.com/x/y");
  });

  it("treats a bare root path as just the host", () => {
    expect(destinationOf("https://a.example.com/")).toBe("a.example.com");
  });

  it("strips a query and a fragment it cannot parse as a URL", () => {
    expect(destinationOf("example.com/x?token=1")).toBe("example.com/x");
  });

  it("returns the input rather than an empty string for something unparseable", () => {
    expect(destinationOf("about:blank")).toBe("about:blank");
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

describe("statusFromEvent", () => {
  it("takes the host's verdict when it sent one", () => {
    expect(statusFromEvent(event({ phase: "end", outcome: "ok" }))).toBe("ok");
    expect(statusFromEvent(event({ phase: "error", outcome: "error" }))).toBe("error");
  });

  it("calls a refusal refused, not failed", () => {
    // The MT5 live-trading gate produces a result that reads like a failure and
    // is a working safety control.
    const refused = event({
      phase: "error",
      outcome: "refused",
      error: "MT5 live trading is switched off. Set MT5_ALLOW_TRADING=1",
    });
    expect(statusFromEvent(refused)).toBe("refused");
  });

  it("falls back to the phase for a frame from a server that sent no outcome", () => {
    expect(statusFromEvent(event({ phase: "end" }))).toBe("ok");
    expect(statusFromEvent(event({ phase: "error" }))).toBe("error");
  });
});

describe("applyToolEvents", () => {
  it("opens a row on a start frame and closes it on the matching end frame", () => {
    const started = applyToolEvents(
      [],
      [
        event({
          phase: "start",
          call_id: "a",
          name: "exec",
          kind: "command",
          arguments: { command: "ls" },
        }),
      ],
      T0,
      200,
    );
    expect(started).toHaveLength(1);
    expect(started[0].status).toBe("running");
    expect(started[0].endedAt).toBeNull();

    const ended = applyToolEvents(
      started,
      [
        event({
          phase: "end",
          call_id: "a",
          name: "exec",
          kind: "command",
          arguments: { command: "ls" },
          result: "ok",
          outcome: "ok",
        }),
      ],
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
        [
          event({
            phase: "start",
            call_id: "a",
            name: "mt5_sandbox",
            kind: "trade",
            arguments: { action: "order", side: "buy", symbol: "EURUSD", volume: 0.01 },
          }),
        ],
        T0,
        200,
      ),
      [
        event({
          phase: "end",
          call_id: "a",
          name: "mt5_sandbox",
          kind: "trade",
          arguments: { action: "order", side: "buy", symbol: "EURUSD", volume: 0.01 },
          result: { retcode: 10009, fill_price: 1.13975, order: 4737451282 },
          outcome: "ok",
        }),
      ],
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
        [
          event({
            phase: "start",
            call_id: "a",
            name: "exec",
            kind: "command",
            arguments: { command: "make" },
          }),
        ],
        T0,
        200,
      ),
      [
        event({
          phase: "error",
          call_id: "a",
          name: "exec",
          kind: "command",
          arguments: { command: "make" },
          error: "No such file: Makefile",
          outcome: "error",
        }),
      ],
      T0 + 100,
      200,
    );
    expect(rows[0].status).toBe("error");
    expect(rows[0].detail).toBe("No such file: Makefile");
  });

  it("marks a refused call as refused and keeps the guard's reason readable", () => {
    const rows = applyToolEvents(
      applyToolEvents(
        [],
        [
          event({
            phase: "start",
            call_id: "g",
            name: "mt5_sandbox",
            kind: "trade",
            arguments: { action: "order", symbol: "EURUSD", side: "buy", volume: 0.01 },
          }),
        ],
        T0,
        200,
      ),
      [
        event({
          phase: "error",
          call_id: "g",
          name: "mt5_sandbox",
          kind: "trade",
          arguments: { action: "order", symbol: "EURUSD", side: "buy", volume: 0.01 },
          error: "MT5 live trading is switched off. Set MT5_ALLOW_TRADING=1",
          outcome: "refused",
        }),
      ],
      T0 + 40,
      200,
    );
    expect(rows[0].status).toBe("refused");
    expect(rows[0].detail).toContain("MT5_ALLOW_TRADING=1");
  });

  it("renders a browsing step as a nav row", () => {
    const rows = applyToolEvents(
      [],
      [
        event({
          phase: "start",
          call_id: "n",
          name: "browser",
          kind: "nav",
          arguments: { action: "navigate", url: "https://news.example.com/gold" },
        }),
      ],
      T0,
      200,
    );
    expect(rows[0].kind).toBe("nav");
    expect(rows[0].line).toBe("browser navigate → news.example.com/gold");
  });

  it("ignores a call the host gave no kind, whatever tool it names", () => {
    // The host decides what belongs in the feed. A general task that never
    // touches a machine or a page must not open a row just because this client
    // happens to recognise the tool's name.
    const rows = applyToolEvents(
      [],
      [
        event({ phase: "start", call_id: "w", name: "web_search", kind: null, arguments: { query: "x" } }),
        event({ phase: "start", call_id: "e", name: "exec", kind: "command", arguments: { command: "ls" } }),
      ],
      T0,
      200,
    );
    expect(rows.map((row) => row.callId)).toEqual(["e"]);
  });

  it("does not open a second row for a replayed start frame", () => {
    const once = applyToolEvents(
      [],
      [
        event({
          phase: "start",
          call_id: "a",
          name: "exec",
          kind: "command",
          arguments: { command: "ls" },
        }),
      ],
      T0,
      200,
    );
    const twice = applyToolEvents(
      once,
      [
        event({
          phase: "start",
          call_id: "a",
          name: "exec",
          kind: "command",
          arguments: { command: "ls" },
        }),
      ],
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
      [
        event({
          phase: "end",
          call_id: "ghost",
          name: "exec",
          kind: "command",
          arguments: { command: "reboot" },
          result: "done",
          outcome: "ok",
        }),
      ],
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
        [
          event({
            phase: "start",
            call_id: "a",
            name: "exec",
            kind: "command",
            arguments: { command: "ls" },
          }),
        ],
        T0,
        200,
      ),
      [
        event({
          phase: "end",
          call_id: "a",
          name: "exec",
          kind: "command",
          arguments: { command: "ls" },
          result: "ok",
          outcome: "ok",
        }),
      ],
      T0 + 10,
      200,
    );
    const again = applyToolEvents(
      closed,
      [
        event({
          phase: "error",
          call_id: "a",
          name: "exec",
          kind: "command",
          arguments: { command: "ls" },
          error: "late",
          outcome: "error",
        }),
      ],
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
        [
          event({
            phase: "start",
            call_id: `c${i}`,
            name: "exec",
            kind: "command",
            arguments: { command: `echo ${i}` },
          }),
        ],
        T0 + i,
        5,
      );
    }
    expect(rows).toHaveLength(5);
    expect(rows[0].line).toBe("echo 7");
    expect(rows[4].line).toBe("echo 11");
  });
});

describe("tradeRows", () => {
  it("keeps trades and drops everything else", () => {
    const rows: SandboxActivityRow[] = [
      {
        callId: "1",
        tool: "exec",
        kind: "command" as SandboxActivityKind,
        line: "ls",
        detail: null,
        status: "ok",
        startedAt: T0,
        endedAt: T0 + 1,
      },
      {
        callId: "2",
        tool: "browser",
        kind: "nav",
        line: "browser navigate → x.com",
        detail: null,
        status: "ok",
        startedAt: T0,
        endedAt: T0 + 1,
      },
      {
        callId: "3",
        tool: "mt5_sandbox",
        kind: "trade",
        line: "mt5 order buy EURUSD 0.01",
        detail: "retcode 10009",
        status: "ok",
        startedAt: T0,
        endedAt: T0 + 1,
      },
    ];
    expect(tradeRows(rows).map((row) => row.callId)).toEqual(["3"]);
  });
});
