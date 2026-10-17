import type { BrokerAccount } from "@/lib/api";

/**
 * What the last real login attempt proved, in the words the user needs.
 *
 * An account being saved is not the same as being connected: only the broker
 * terminal can answer that, and it can only run inside the execution sandbox.
 * It reports back through the agent's `broker_trade` tool, which writes the
 * outcome onto the row, and the pane reads it here. Until a login has been
 * tried this says so plainly rather than implying one worked.
 *
 * Kept out of the component so the three states can be asserted without
 * mounting a settings pane.
 */
export interface BrokerConnectionLabel {
  text: string;
  tone: "connected" | "failed" | "idle";
  /** The untruncated text, for the row's tooltip. */
  detail: string;
}

/** How much of a terminal's failure reason a single row may show. The stored
 * message comes from an MT5 log and can be a paragraph; one failed account
 * must not push the Remove button off the row. */
const MAX_REASON_CHARS = 120;

function formatWhen(value: string | undefined): string {
  if (!value) return "";
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return "";
  return at.toLocaleString();
}

export function brokerConnectionLabel(account: BrokerAccount): BrokerConnectionLabel {
  const connection = account.connection;
  const status = connection?.status ?? "";
  const when = formatWhen(connection?.at);

  if (status === "connected") {
    const text = when ? `Connected ${when}` : "Connected";
    return { text, tone: "connected", detail: text };
  }

  if (status === "failed") {
    const reason = (connection?.message ?? "").trim();
    const short =
      reason.length > MAX_REASON_CHARS
        ? `${reason.slice(0, MAX_REASON_CHARS - 1)}…`
        : reason;
    const lead = `Not connected — last login failed${when ? ` ${when}` : ""}`;
    return {
      text: short ? `${lead}: ${short}` : lead,
      tone: "failed",
      detail: reason ? `${lead}: ${reason}` : lead,
    };
  }

  const text = "Not connected yet — connect from a chat";
  return { text, tone: "idle", detail: text };
}
