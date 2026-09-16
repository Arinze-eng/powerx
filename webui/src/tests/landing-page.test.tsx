import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { LandingPage } from "@/components/LandingPage";
import { CREDIT_PACKS, formatCredits, formatUsd } from "@/lib/plans";
import {
  APP_SUMMARY,
  FEATURES,
  QUICK_ACTIONS,
  SUGGESTED_TASKS,
  STEPS,
} from "@/lib/marketing";

/** Renders the landing page with spy callbacks. */
function renderLanding(props: Partial<Parameters<typeof LandingPage>[0]> = {}) {
  const onSignIn = vi.fn();
  const onSignUp = vi.fn();
  const onPrivacy = vi.fn();
  render(
    <LandingPage
      onSignIn={onSignIn}
      onSignUp={onSignUp}
      onPrivacy={onPrivacy}
      {...props}
    />,
  );
  return { onSignIn, onSignUp, onPrivacy };
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("LandingPage structure", () => {
  it("leads with the task composer, not a marketing block", () => {
    renderLanding();
    // The Manus-style entry point: a real input asking for the task.
    const composer = screen.getByLabelText(
      "Describe what you want CDNAI to work on",
    );
    expect(composer).toBeTruthy();
    expect(composer.tagName).toBe("TEXTAREA");
    expect(composer.getAttribute("placeholder")).toContain("Give CDNAI a task");
  });

  it("states that signing in is required before tasks run", () => {
    renderLanding();
    expect(screen.getByText(/Sign in to run tasks\./i)).toBeTruthy();
  });

  it("renders the about / how-it-works / pricing sections with anchors", () => {
    const { container } = render(
      <LandingPage onSignIn={vi.fn()} onSignUp={vi.fn()} onPrivacy={vi.fn()} />,
    );
    for (const id of ["product", "how", "about", "pricing"]) {
      expect(container.querySelector(`#${id}`), `#${id} section`).toBeTruthy();
    }
  });

  it("shows the product summary and every marketing feature", () => {
    renderLanding();
    expect(screen.getByText(APP_SUMMARY)).toBeTruthy();
    for (const f of FEATURES) {
      expect(screen.getByText(f.title), f.title).toBeTruthy();
    }
    for (const s of STEPS) {
      expect(screen.getByText(s.title), s.title).toBeTruthy();
    }
  });

  it("renders suggested starter tasks", () => {
    renderLanding();
    for (const s of SUGGESTED_TASKS) {
      expect(screen.getByText(s.title), s.title).toBeTruthy();
    }
  });

  it("renders the Manus-style quick-action chips", () => {
    renderLanding();
    for (const a of QUICK_ACTIONS) {
      expect(screen.getByText(a.label), a.label).toBeTruthy();
    }
  });

  it("fills the composer from a quick-action chip instead of sending it", () => {
    const { onSignUp } = renderLanding();
    const first = QUICK_ACTIONS[0];
    fireEvent.click(screen.getByText(first.label));
    const composer = screen.getByLabelText(
      "Describe what you want CDNAI to work on",
    ) as HTMLTextAreaElement;
    expect(composer.value).toBe(first.prompt);
    // Filling is not submitting — the visitor can still edit the prompt.
    expect(onSignUp).not.toHaveBeenCalled();
  });

  it("uses the monochrome surface rather than gradient accents", () => {
    // Regression guard for the "blue shiny" marketing treatment: the public
    // surface is intentionally monochrome, so any reintroduced purple/cyan
    // gradient should fail here rather than silently ship.
    const { container } = render(
      <LandingPage onSignIn={vi.fn()} onSignUp={vi.fn()} onPrivacy={vi.fn()} />,
    );
    const html = container.innerHTML;
    expect(html).not.toContain("7C5CFF");
    expect(html).not.toContain("22D3EE");
    expect(html).not.toMatch(/gradient-to-/);
  });

  it("does not mention institution-specific content", () => {
    // The landing page describes the product only; the copy modules must not
    // leak operational or customer-specific material.
    const { container } = render(
      <LandingPage onSignIn={vi.fn()} onSignUp={vi.fn()} onPrivacy={vi.fn()} />,
    );
    expect(container.textContent?.toLowerCase()).not.toContain("uniabuja");
    expect(container.textContent?.toLowerCase()).not.toContain("transcript");
  });
});

describe("LandingPage composer gating", () => {
  it("routes to signup when a task is submitted", () => {
    const { onSignUp, onSignIn } = renderLanding();
    const composer = screen.getByLabelText(
      "Describe what you want CDNAI to work on",
    );
    fireEvent.change(composer, { target: { value: "Write me a report" } });
    fireEvent.click(screen.getByLabelText("Start this task"));
    expect(onSignUp).toHaveBeenCalledTimes(1);
    expect(onSignIn).not.toHaveBeenCalled();
  });

  it("sends the task on Enter but keeps Shift+Enter for newlines", () => {
    const { onSignUp } = renderLanding();
    const composer = screen.getByLabelText(
      "Describe what you want CDNAI to work on",
    );
    fireEvent.change(composer, { target: { value: "Do a thing" } });

    fireEvent.keyDown(composer, { key: "Enter", shiftKey: true });
    expect(onSignUp).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, { key: "Enter" });
    expect(onSignUp).toHaveBeenCalledTimes(1);
  });

  it("will not submit an empty or whitespace-only task", () => {
    const { onSignUp } = renderLanding();
    const send = screen.getByLabelText("Start this task") as HTMLButtonElement;
    expect(send.disabled).toBe(true);

    const composer = screen.getByLabelText(
      "Describe what you want CDNAI to work on",
    );
    fireEvent.change(composer, { target: { value: "   " } });
    fireEvent.click(send);
    expect(onSignUp).not.toHaveBeenCalled();
  });

  it("fills the composer when a starter task is tapped", () => {
    renderLanding();
    const first = SUGGESTED_TASKS[0];
    fireEvent.click(screen.getByText(first.title));
    const composer = screen.getByLabelText(
      "Describe what you want CDNAI to work on",
    ) as HTMLTextAreaElement;
    expect(composer.value).toBe(first.prompt);
  });

  it("exposes sign in and sign up in the hero gate", () => {
    const { onSignIn, onSignUp } = renderLanding();
    // These two live inside the composer's gate paragraph.
    const gate = screen.getByText(/Sign in to run tasks\./i).closest("p")!;
    fireEvent.click(within(gate).getByText("Sign in"));
    expect(onSignIn).toHaveBeenCalledTimes(1);
    fireEvent.click(within(gate).getByText("Create an account"));
    expect(onSignUp).toHaveBeenCalledTimes(1);
  });
});

describe("LandingPage pricing", () => {
  it("lists every credit pack with its exact price and credit count", () => {
    renderLanding();
    for (const pack of CREDIT_PACKS) {
      expect(screen.getByText(pack.name), pack.name).toBeTruthy();
      expect(screen.getByText(formatUsd(pack.amountUsd)), pack.name).toBeTruthy();
      expect(
        screen.getByText(`${formatCredits(pack.credits)} credits`),
        pack.name,
      ).toBeTruthy();
    }
  });

  it("renders the free tier alongside the packs", () => {
    renderLanding();
    expect(screen.getByText("Free")).toBeTruthy();
    expect(screen.getByText("$0")).toBeTruthy();
    expect(screen.getByText("Credit packs")).toBeTruthy();
  });

  it("marks the featured pack", () => {
    renderLanding();
    expect(screen.getByText("Most popular")).toBeTruthy();
    expect(screen.getByText("Best value")).toBeTruthy();
  });

  it("routes to sign-in for every pack purchase", () => {
    const { onSignIn } = renderLanding();
    const popular = CREDIT_PACKS.find((p) => p.featured)!;
    fireEvent.click(screen.getByText(`Get ${popular.name}`));
    expect(onSignIn).toHaveBeenCalledTimes(1);
    // One hint per pack.
    expect(screen.getAllByText("Sign in to purchase")).toHaveLength(
      CREDIT_PACKS.length,
    );
  });

  it("never opens a payment page for an anonymous visitor", () => {
    // PAYWALL REGRESSION GUARD. The pack CTAs used to call window.open() with
    // the checkout URL straight from the public landing page, sending signed-out
    // visitors to the payment provider without an account — so the payment could
    // never be claimed by a user. Every CTA must now start the auth flow.
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const { onSignIn, onSignUp } = renderLanding();
    for (const pack of CREDIT_PACKS) {
      fireEvent.click(screen.getByText(`Get ${pack.name}`));
    }
    expect(open).not.toHaveBeenCalled();
    expect(onSignIn).toHaveBeenCalledTimes(CREDIT_PACKS.length);
    expect(onSignUp).not.toHaveBeenCalled();
  });

  it("labels every pack CTA as an auth-gated action", () => {
    renderLanding();
    // The public surface never renders a raw "Buy" that implies an immediate
    // payment, and it holds no checkout URL to leak.
    const packCtas = () =>
      screen
        .getAllByRole("button")
        .map((b) => (b.textContent ?? "").trim())
        .filter((t) => CREDIT_PACKS.some((p) => t === `Get ${p.name}` || t === `Buy ${p.name}`));
    expect(packCtas().filter((t) => t.startsWith("Buy "))).toHaveLength(0);
    expect(packCtas().filter((t) => t.startsWith("Get "))).toHaveLength(CREDIT_PACKS.length);
  });
});