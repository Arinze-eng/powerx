import { describe, expect, it } from "vitest";

import { computeTaskStats } from "@/lib/task-stats";
import type { ToolProgressEvent, UIMessage } from "@/lib/types";

function traceMessage(events: ToolProgressEvent[]): UIMessage {
  return {
    id: crypto.randomUUID(),
    role: "tool",
    kind: "trace",
    content: "",
    traces: events.map(() => "x"),
    toolEvents: events,
    createdAt: Date.now(),
  };
}

describe("computeTaskStats", () => {
  it("counts sandbox run ops as commands and write/upload as files", () => {
    const events: ToolProgressEvent[] = [
      { phase: "start", call_id: "a", name: "novita_sandbox", arguments: { action: "run", command: "ls" } },
      { phase: "end", call_id: "a", name: "novita_sandbox", arguments: { action: "run", command: "ls" }, result: "ok" },
      { phase: "start", call_id: "b", name: "novita_sandbox", arguments: { action: "write", path: "x.py" } },
      { phase: "start", call_id: "c", name: "novita_sandbox", arguments: { action: "read", path: "y" } },
    ];
    const stats = computeTaskStats([traceMessage(events)]);
    // Deduped by call_id → 3 logical steps.
    expect(stats.steps).toBe(3);
    expect(stats.commandsRun).toBe(1);
    expect(stats.filesCreated).toBe(1);
  });

  it("expands sandbox_batch into per-op command counts", () => {
    const events: ToolProgressEvent[] = [
      {
        phase: "start",
        call_id: "batch1",
        name: "sandbox_batch",
        arguments: {
          ops: [
            { action: "write", path: "run.sh" },
            { action: "run", command: "./run.sh" },
            { action: "run", command: "echo done" },
            { action: "complete", summary: "all good" },
          ],
        },
      },
    ];
    const stats = computeTaskStats([traceMessage(events)]);
    expect(stats.steps).toBe(1); // one batch invocation
    expect(stats.commandsRun).toBe(3); // write + 2 runs (complete excluded)
    expect(stats.filesCreated).toBe(1); // the write op
  });

  it("reads api calls from turn usage only when authoritative", () => {
    const events: ToolProgressEvent[] = [
      { phase: "start", call_id: "e", name: "exec", arguments: { command: "pytest" } },
    ];
    const live = computeTaskStats([traceMessage(events)], { live: true });
    expect(live.apiCalls).toBeNull();
    expect(live.commandsRun).toBe(1);

    const done = computeTaskStats([traceMessage(events)], {
      live: false,
      turnUsage: { llm_calls: 2, prompt_tokens: 100 },
    });
    expect(done.apiCalls).toBe(2);
  });

  it("merges persisted file-edit rows without double counting", () => {
    const msg: UIMessage = {
      id: crypto.randomUUID(),
      role: "tool",
      kind: "trace",
      content: "",
      traces: ["write_file(src/a.ts)"],
      toolEvents: [{ phase: "start", call_id: "w", name: "write_file", arguments: { path: "src/a.ts" } }],
      fileEdits: [
        { call_id: "w", tool: "write_file", path: "src/a.ts", added: 5, deleted: 0 },
      ] as unknown as UIMessage["fileEdits"],
      createdAt: Date.now(),
    };
    const stats = computeTaskStats([msg]);
    expect(stats.filesCreated).toBe(1);
  });

  it("counts every registered execution tool as a command", () => {
    const events: ToolProgressEvent[] = [
      { phase: "end", call_id: "p1", name: "python_code", arguments: { code: "print(1)" } },
      { phase: "end", call_id: "c1", name: "run_cli_app", arguments: { app: "deploy" } },
      { phase: "end", call_id: "b1", name: "build_artifact", arguments: {} },
      { phase: "end", call_id: "w1", name: "web_dev", arguments: {} },
      { phase: "end", call_id: "br1", name: "browser", arguments: { url: "https://x" } },
    ];
    const stats = computeTaskStats([traceMessage(events)]);
    expect(stats.steps).toBe(5);
    expect(stats.commandsRun).toBe(4); // python_code + run_cli_app + build_artifact + web_dev
    expect(stats.pagesViewed).toBe(1); // browser navigated one page
  });

  it("expands a run_plan call into its deterministic per-step counts", () => {
    const plan = {
      steps: [
        { id: "a", tool: "exec", args: { command: "make build" } },
        {
          id: "p",
          parallel: [
            { id: "f", tool: "web_fetch", args: { url: "https://x" } },
            { id: "w", tool: "write_file", args: { path: "out.txt" } },
          ],
        },
        { id: "z", tool: "python_code", args: { code: "1+1" } },
      ],
      output: "$a",
    };
    const events: ToolProgressEvent[] = [
      { phase: "end", call_id: "rp1", name: "run_plan", arguments: { plan } },
    ];
    const stats = computeTaskStats([traceMessage(events)]);
    expect(stats.steps).toBe(4); // a + f + w + z (parallel children counted individually)
    expect(stats.commandsRun).toBe(2); // exec + python_code
    expect(stats.filesCreated).toBe(1); // write_file
    expect(stats.pagesViewed).toBe(1); // web_fetch
  });

  it("falls back to one deterministic batch for an unparseable run_plan", () => {
    const events: ToolProgressEvent[] = [
      { phase: "end", call_id: "rp2", name: "run_plan", arguments: {} },
    ];
    const stats = computeTaskStats([traceMessage(events)]);
    expect(stats.steps).toBe(1);
    expect(stats.commandsRun).toBe(1);
  });

  it("returns empty stats for no activity", () => {
    const stats = computeTaskStats([]);
    expect(stats.commandsRun).toBe(0);
    expect(stats.filesCreated).toBe(0);
    expect(stats.steps).toBe(0);
    expect(stats.apiCalls).toBeNull();
  });

  it("uses the live streamed llm_calls while streaming, before turn_end", () => {
    const events: ToolProgressEvent[] = [
      { phase: "start", call_id: "b1", name: "sandbox_batch", arguments: { ops: [{ action: "run" }] } },
    ];
    const msg = traceMessage(events);
    (msg as { liveLlmCalls?: number }).liveLlmCalls = 3;
    const live = computeTaskStats([msg], { live: true });
    expect(live.apiCalls).toBe(3);

    // Authoritative turn_end usage wins over the live estimate.
    const done = computeTaskStats([msg], { live: false, turnUsage: { llm_calls: 5 } });
    expect(done.apiCalls).toBe(5);
  });
});
