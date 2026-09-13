import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";

import { AnnouncementDialog } from "@/components/AnnouncementDialog";
import i18n from "@/i18n";

const ANNOUNCEMENT = {
  id: "ann-123",
  title: "Scheduled maintenance",
  message: "CDNAI will be briefly unavailable tonight at 9pm UTC.",
  created_at: "2026-09-13T00:00:00Z",
};

function mockFetch(payload: unknown) {
  const fn = vi.fn().mockResolvedValue({
    ok: true,
    json: async () => payload,
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

describe("AnnouncementDialog", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the active announcement in a dialog", async () => {
    mockFetch({ announcement: ANNOUNCEMENT });
    render(<AnnouncementDialog />);

    await waitFor(() => {
      expect(screen.getByText(ANNOUNCEMENT.title)).toBeInTheDocument();
    });
    expect(screen.getByText(ANNOUNCEMENT.message)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /got it/i })).toBeInTheDocument();
  });

  it("renders nothing when there is no announcement", async () => {
    mockFetch({ announcement: null });
    const { container } = render(<AnnouncementDialog />);
    // allow the async fetch effect to settle
    await waitFor(() => expect(screen.queryByText(ANNOUNCEMENT.title)).toBeNull());
    expect(container).toBeEmptyDOMElement();
  });

  it("does not re-show after dismissal for the same id", async () => {
    mockFetch({ announcement: ANNOUNCEMENT });
    const first = render(<AnnouncementDialog />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /got it/i })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /got it/i }));
    await waitFor(() => expect(first.container).toBeEmptyDOMElement());
    first.unmount();

    // Re-mount: dismissal persisted, so it should stay hidden.
    render(<AnnouncementDialog />);
    await waitFor(() => expect(screen.queryByText(ANNOUNCEMENT.title)).toBeNull());
  });

  it("survives a failed fetch without throwing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("network down")));
    const { container } = render(<AnnouncementDialog />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });
});
