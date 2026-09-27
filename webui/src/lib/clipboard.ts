/**
 * Copy text, working on desktop browsers and on mobile WebViews where the
 * Clipboard API is missing or refuses.
 *
 * WHY THE LEGACY PATH GOES FIRST
 * ``navigator.clipboard.writeText`` is the modern, correct API, and it is used —
 * but it can only be *awaited*, and awaiting spends the transient user activation
 * that ``document.execCommand("copy")`` requires. So the old path ran after an
 * await and failed on exactly the browsers that need it: an Android WebView or an
 * insecure origin where ``navigator.clipboard`` exists but rejects, and the copy
 * then silently did nothing while the button looked like it had worked.
 *
 * Running the synchronous path first keeps the gesture intact, which is what
 * makes the copy land on a phone. If it cannot run at all — the API removed, or
 * refused — the async path is tried, which covers a browser that has dropped
 * ``execCommand``.
 */
export async function copyTextToClipboard(text: string): Promise<boolean> {
  if (copyTextWithTextarea(text)) {
    return true;
  }

  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Nothing left to try: the caller reports the failure and offers the
    // select-and-copy route instead of pretending it worked.
  }

  return false;
}

function copyTextWithTextarea(text: string): boolean {
  if (typeof document.execCommand !== "function" || !document.body) {
    return false;
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  // Deliberately NOT readonly and marked contenteditable: WebKit and the Android
  // WebView refuse to select a read-only field, which is the whole reason this
  // path can fail silently.
  textarea.setAttribute("contenteditable", "true");
  textarea.setAttribute("aria-hidden", "true");
  textarea.setAttribute("tabindex", "-1");
  textarea.style.position = "fixed";
  textarea.style.top = "0";
  textarea.style.left = "-9999px";
  textarea.style.width = "1px";
  textarea.style.height = "1px";
  textarea.style.opacity = "0";

  document.body.appendChild(textarea);
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    // Some engines only copy what setSelectionRange covers, and the trailing
    // character is dropped by others when the range ends exactly at the end.
    textarea.setSelectionRange(0, textarea.value.length + 1);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(textarea);
  }
}
