import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ApiPlatformSettings } from "@/components/settings/api/ApiPlatformSettings";
import { ClientProvider } from "@/providers/ClientProvider";

const requestMutationMock = vi.fn();

function platformPayload(overrides: Record<string, unknown> = {}) {
  return {
    enabled: true,
    configured: true,
    signed_in: true,
    base_url: "https://powerx.example.com",
    endpoint: "https://powerx.example.com/v1",
    models: [{ id: "powerx-1", object: "model", created: 0, owned_by: "powerx" }],
    keys: [
      {
        id: "k1",
        name: "laptop",
        prefix: "px_abcdef12",
        active: true,
        requests: 12,
        last_used_at: "2026-09-01T10:00:00Z",
        created_at: "2026-08-01T10:00:00Z",
      },
    ],
    max_keys: 10,
    docs: {
      chat_completions: "https://powerx.example.com/v1/chat/completions",
      models: "https://powerx.example.com/v1/models",
      api_docs: "https://powerx.example.com/v1/api-docs",
    },
    ...overrides,
  };
}

function renderPane() {
  return render(
    <ClientProvider client={{ requestMutation: requestMutationMock } as never} token="tok">
      <ApiPlatformSettings />
    </ClientProvider>,
  );
}

beforeEach(() => {
  requestMutationMock.mockReset();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => platformPayload(),
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("shows the base URL, endpoint and models for a signed-in account", async () => {
  renderPane();

  await waitFor(() => {
    expect(screen.getByTestId("api-platform-base-url")).toHaveTextContent(
      "https://powerx.example.com",
    );
  });
  expect(screen.getByTestId("api-platform-endpoint")).toHaveTextContent(
    "https://powerx.example.com/v1/chat/completions",
  );
  expect(screen.getByText("powerx-1")).toBeTruthy();
  expect(screen.getByText("laptop")).toBeTruthy();
  expect(fetch).toHaveBeenCalledWith(
    "/api/settings/api-platform",
    expect.objectContaining({ headers: { Authorization: "Bearer tok" } }),
  );
});

it("reveals a generated key once and never asks the server for it again", async () => {
  renderPane();
  await waitFor(() => expect(screen.getByTestId("api-platform-generate")).toBeEnabled());

  requestMutationMock.mockResolvedValueOnce(
    platformPayload({
      created: { key: "px_livekeyvalue", name: "laptop", id: "k2", prefix: "px_livekey" },
    }),
  );

  await userEvent.click(screen.getByTestId("api-platform-generate"));

  await waitFor(() => {
    expect(screen.getByTestId("api-platform-fresh-key")).toHaveTextContent("px_livekeyvalue");
  });
  expect(requestMutationMock).toHaveBeenCalledWith(
    "settings.api_platform.create",
    {},
    20_000,
  );
  // The list refresh after a create comes from the mutation response, not a read.
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("confirms before revoking every key on the account", async () => {
  renderPane();
  await waitFor(() => expect(screen.getByTestId("api-platform-revoke")).toBeEnabled());

  requestMutationMock.mockResolvedValueOnce(platformPayload({ keys: [], revoked: 1 }));

  await userEvent.click(screen.getByTestId("api-platform-revoke"));
  await userEvent.click(await screen.findByTestId("api-platform-revoke-confirm"));

  await waitFor(() => {
    expect(requestMutationMock).toHaveBeenCalledWith("settings.api_platform.revoke", {}, 20_000);
  });
});

async function generateKey() {
  renderPane();
  await waitFor(() => expect(screen.getByTestId("api-platform-generate")).toBeEnabled());
  requestMutationMock.mockResolvedValueOnce(
    platformPayload({
      created: { key: "px_livekeyvalue", name: "laptop", id: "k2", prefix: "px_livekey" },
    }),
  );
  await userEvent.click(screen.getByTestId("api-platform-generate"));
  await waitFor(() =>
    expect(screen.getByTestId("api-platform-fresh-key")).toHaveTextContent("px_livekeyvalue"),
  );
}

it("copies the generated key on request", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });

  await generateKey();
  await userEvent.click(screen.getByTestId("api-platform-copy-key"));

  await waitFor(() => expect(writeText).toHaveBeenCalledWith("px_livekeyvalue"));
  expect(screen.getByText("Copied")).toBeTruthy();
});

it("hands the key over by hand when the clipboard is blocked", async () => {
  // The key is shown once, so a clipboard that refuses must not be the end of it:
  // the panel says so and leaves the key selected for a manual copy.
  vi.stubGlobal("navigator", {
    ...navigator,
    clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
  });

  await generateKey();
  await userEvent.click(screen.getByTestId("api-platform-copy-key"));

  await waitFor(() => expect(screen.getByTestId("api-platform-copy-hint")).toBeTruthy());
  expect(screen.getByTestId("api-platform-copy-hint")).toHaveTextContent("Ctrl/Cmd-C");
  // Still on screen, still whole.
  expect(screen.getByTestId("api-platform-fresh-key")).toHaveTextContent("px_livekeyvalue");
});

it("says why the pane is unusable when the server address is missing", async () => {
  vi.mocked(fetch).mockResolvedValueOnce({
    ok: true,
    status: 200,
    json: async () => platformPayload({
      configured: false,
      base_url: "",
      endpoint: "",
      docs: { chat_completions: "", models: "", api_docs: "" },
      notice: "The server address is not configured, so keys cannot be used yet.",
    }),
  } as Response);

  renderPane();

  await waitFor(() => {
    expect(
      screen.getByText("The server address is not configured, so keys cannot be used yet."),
    ).toBeTruthy();
  });
  expect(screen.getByTestId("api-platform-generate")).toBeDisabled();
  expect(screen.queryByTestId("api-platform-fresh-key")).toBeNull();
});
