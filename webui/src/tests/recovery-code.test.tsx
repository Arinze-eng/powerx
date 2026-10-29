import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RecoveryCodeScreen, RECOVERY_ADMIN_CONTACT } from "@/components/RecoveryCodeScreen";
import { SupabaseAuthPage } from "@/components/SupabaseAuthPage";
import { issueRecoveryCode, resetPasswordWithCode } from "@/lib/supabase-auth";

const URL_BASE = "https://example.supabase.co";
const ANON = "anon-key";

describe("RecoveryCodeScreen", () => {
  afterEach(() => {
    cleanup();
  });

  it("keeps continue disabled until the code is copied", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const onContinue = vi.fn();
    render(
      <RecoveryCodeScreen
        code="ABCD-EFGH-JKMN-PQRS-TUVW"
        error={null}
        continueLabel="I have copied it, continue"
        onContinue={onContinue}
      />,
    );

    const continueBtn = screen.getByRole("button", { name: "Copy the code to continue" });
    expect((continueBtn as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(continueBtn);
    expect(onContinue).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("ABCD-EFGH-JKMN-PQRS-TUVW"));
    const enabled = await screen.findByRole("button", { name: "I have copied it, continue" });
    expect((enabled as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(enabled);
    expect(onContinue).toHaveBeenCalledTimes(1);
  });

  it("shows the admin contact and the lost-code warning", () => {
    render(
      <RecoveryCodeScreen code="ABCD-EFGH-JKMN-PQRS-TUVW" error={null} continueLabel="go" onContinue={() => {}} />,
    );
    expect(screen.getByText(/you lose access to your account/i)).toBeTruthy();
    expect(screen.getByRole("link", { name: RECOVERY_ADMIN_CONTACT })).toBeTruthy();
  });

  it("shows the error with retry and sign-out actions when no code was issued", () => {
    const onRetry = vi.fn();
    const onSignOut = vi.fn();
    render(
      <RecoveryCodeScreen
        code={null}
        error="Could not create your recovery code. Please try again."
        continueLabel="go"
        onContinue={() => {}}
        onRetry={onRetry}
        onSignOut={onSignOut}
      />,
    );
    expect(screen.getByRole("alert").textContent).toContain("Could not create your recovery code");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onSignOut).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /copy code/i })).toBeNull();
  });
});

describe("recovery-code API wrappers", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("issueRecoveryCode sends the user's token and returns the code", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true, recovery_code: "ABCD-EFGH-JKMN-PQRS-TUVW" }), { status: 200 }),
    );
    const res = await issueRecoveryCode(`${URL_BASE}/`, ANON, "user-token");
    expect(res).toEqual({ ok: true, recoveryCode: "ABCD-EFGH-JKMN-PQRS-TUVW" });
    const [calledUrl, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(calledUrl).toBe(`${URL_BASE}/functions/v1/recovery-code-issue`);
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer user-token");
  });

  it("issueRecoveryCode surfaces a 409 as an error", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: false, error: "This account already has a recovery code." }), { status: 409 }),
    );
    const res = await issueRecoveryCode(URL_BASE, ANON, "user-token");
    expect(res.ok).toBe(false);
    expect(res.error).toContain("already has a recovery code");
  });

  it("resetPasswordWithCode normalises the email and returns the new code", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: true, recovery_code: "NEWC-ODE2-3456-7899-ABCD" }), { status: 200 }),
    );
    const res = await resetPasswordWithCode(URL_BASE, ANON, "  Dana@Example.com ", "code", "secret1");
    expect(res.recoveryCode).toBe("NEWC-ODE2-3456-7899-ABCD");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      email: "dana@example.com",
      code: "code",
      password: "secret1",
    });
  });

  it("resetPasswordWithCode returns the server's message on failure", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ ok: false, error: "That email and recovery code do not match." }), { status: 400 }),
    );
    const res = await resetPasswordWithCode(URL_BASE, ANON, "a@b.co", "bad", "secret1");
    expect(res).toEqual({ ok: false, error: "That email and recovery code do not match." });
  });
});

describe("SupabaseAuthPage forgot-password mode", () => {
  afterEach(() => {
    cleanup();
  });

  it("opens from sign-in and submits email, code and new password", async () => {
    const onResetPassword = vi.fn(async () => ({}));
    render(
      <SupabaseAuthPage
        supabaseUrl={URL_BASE}
        anonKey={ANON}
        onSignIn={async () => ({})}
        onSignUp={async () => ({})}
        onResetPassword={onResetPassword}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Forgot password?" }));
    expect(screen.getByRole("heading", { name: "Reset your password" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "dana@example.com" } });
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "abcd-efgh" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "secret1" } });
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));

    await waitFor(() =>
      expect(onResetPassword).toHaveBeenCalledWith("dana@example.com", "abcd-efgh", "secret1"),
    );
  });

  it("rejects a short new password without calling the server", () => {
    const onResetPassword = vi.fn(async () => ({}));
    render(
      <SupabaseAuthPage
        supabaseUrl={URL_BASE}
        anonKey={ANON}
        onSignIn={async () => ({})}
        onSignUp={async () => ({})}
        onResetPassword={onResetPassword}
        initialMode="signin"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Forgot password?" }));
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "dana@example.com" } });
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "abcd" } });
    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "123" } });
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));
    expect(screen.getByRole("alert").textContent).toContain("at least 6 characters");
    expect(onResetPassword).not.toHaveBeenCalled();
  });

  it("shows the admin contact in forgot mode", () => {
    render(
      <SupabaseAuthPage
        supabaseUrl={URL_BASE}
        anonKey={ANON}
        onSignIn={async () => ({})}
        onSignUp={async () => ({})}
        onResetPassword={async () => ({})}
        initialMode="signin"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Forgot password?" }));
    expect(screen.getByRole("link", { name: RECOVERY_ADMIN_CONTACT })).toBeTruthy();
  });
});
