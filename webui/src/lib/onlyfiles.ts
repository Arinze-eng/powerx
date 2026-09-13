/** Direct browser -> onlyfiles.com uploads for WebUI file attachments.
 *
 * File attachments (pdf, zip, apk, xlsx, ...) are uploaded straight from the
 * browser to onlyfiles.com so the file bytes never transit the gateway host.
 * Images and videos keep the existing base64 data-URL path untouched.
 *
 * onlyfiles.com needs no account, no key, and no bucket: one fixed public
 * endpoint, one POST per file, and the response JSON carries the public URL.
 */

export const ONLYFILES_UPLOAD_URL = "https://onlyfiles.com/api/v1/upload";

/** onlyfiles.com rejects anything larger than 100 MB. */
export const ONLYFILES_MAX_BYTES = 100 * 1024 * 1024;

const UPLOAD_TIMEOUT_MS = 120_000;

export type OnlyFilesUploadFailure = "too_large" | "io" | "rejected";

export type OnlyFilesUploadResult =
  | { ok: true; url: string }
  | { ok: false; reason: OnlyFilesUploadFailure };

/** onlyfiles page URLs serve an HTML viewer; the raw bytes live under
 * ``/dl/<ts.nonce>/<id>/<name>`` which is embedded in the viewer page. The
 * backend resolves that at delivery time, so here we simply return the full
 * page URL (which already includes the filename) as the shareable link. */
export function onlyfilesPageUrl(full: string): string {
  try {
    const parsed = new URL(full);
    if (parsed.hostname !== "onlyfiles.com") return full;
    return full;
  } catch {
    return full;
  }
}

/** Upload one file straight from the browser to onlyfiles.com.
 *
 * Multiple files can be uploaded concurrently — each call is independent, so
 * the composer's existing per-attachment lifecycle already parallelizes them.
 */
export async function uploadFileToOnlyFiles(
  file: File,
  timeoutMs: number = UPLOAD_TIMEOUT_MS,
): Promise<OnlyFilesUploadResult> {
  if (file.size > ONLYFILES_MAX_BYTES) return { ok: false, reason: "too_large" };
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const form = new FormData();
    form.append("file", file, file.name || "upload.bin");
    const response = await fetch(ONLYFILES_UPLOAD_URL, {
      method: "POST",
      body: form,
      signal: controller.signal,
    });
    if (!response.ok) return { ok: false, reason: "rejected" };
    const payload: unknown = await response.json();
    // Shape: { status: true, data: { file: { url: { full, short } } } }
    const url =
      typeof payload === "object" && payload !== null
        ? (payload as { data?: { file?: { url?: { full?: unknown } } } }).data?.file?.url?.full
        : undefined;
    if (typeof url !== "string" || !url) return { ok: false, reason: "rejected" };
    return { ok: true, url: onlyfilesPageUrl(url) };
  } catch {
    return { ok: false, reason: "io" };
  } finally {
    clearTimeout(timer);
  }
}
