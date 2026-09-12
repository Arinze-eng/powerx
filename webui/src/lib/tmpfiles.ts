/** Direct browser -> tmpfiles.org uploads for WebUI file attachments.
 *
 * File attachments (pdf, zip, apk, xlsx, ...) are uploaded straight from the
 * browser to tmpfiles.org so the file bytes never transit the gateway host.
 * Images and videos keep the existing base64 data-URL path untouched.
 *
 * tmpfiles.org needs no account, no key, and no bucket: one fixed public
 * endpoint, one POST per file, and the response JSON carries the public URL.
 */

export const TMPFILES_UPLOAD_URL = "https://tmpfiles.org/api/v1/upload";

/** tmpfiles.org rejects anything larger than 50 MB. */
export const TMPFILES_MAX_BYTES = 50 * 1024 * 1024;

const UPLOAD_TIMEOUT_MS = 120_000;

export type TmpfilesUploadFailure = "too_large" | "io" | "rejected";

export type TmpfilesUploadResult =
  | { ok: true; url: string }
  | { ok: false; reason: TmpfilesUploadFailure };

/** tmpfiles.org legacy numeric-id links need the ``/dl/`` prefix to serve the
 * raw bytes; newer opaque-slug links redirect ``/dl`` back to the HTML page,
 * where the page URL is already the valid public link. */
export function tmpfilesDirectUrl(pageUrl: string): string {
  try {
    const parsed = new URL(pageUrl);
    if (parsed.hostname !== "tmpfiles.org") return pageUrl;
    const first = parsed.pathname.split("/").find(Boolean);
    return first && /^\d+$/.test(first)
      ? `https://tmpfiles.org/dl${parsed.pathname}`
      : pageUrl;
  } catch {
    return pageUrl;
  }
}

/** Upload one file straight from the browser to tmpfiles.org.
 *
 * Multiple files can be uploaded concurrently — each call is independent, so
 * the composer's existing per-attachment lifecycle already parallelizes them.
 */
export async function uploadFileToTmpfiles(
  file: File,
  timeoutMs: number = UPLOAD_TIMEOUT_MS,
): Promise<TmpfilesUploadResult> {
  if (file.size > TMPFILES_MAX_BYTES) return { ok: false, reason: "too_large" };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const form = new FormData();
    form.append("file", file, file.name || "upload.bin");
    const response = await fetch(TMPFILES_UPLOAD_URL, {
      method: "POST",
      body: form,
      signal: controller.signal,
    });
    if (!response.ok) return { ok: false, reason: "rejected" };
    const payload: unknown = await response.json();
    const url =
      typeof payload === "object" && payload !== null
        ? (payload as { data?: { url?: unknown } }).data?.url
        : undefined;
    if (typeof url !== "string" || !url) return { ok: false, reason: "rejected" };
    return { ok: true, url: tmpfilesDirectUrl(url) };
  } catch {
    return { ok: false, reason: "io" };
  } finally {
    clearTimeout(timer);
  }
}
