import { useCallback, useEffect, useRef, useState } from "react";

import { encodeImage, type EncodeFailure } from "@/lib/imageEncode";
import { TMPFILES_MAX_BYTES, uploadFileToTmpfiles } from "@/lib/tmpfiles";
import type { WebUIIngressLimits } from "@/lib/types";

/** Lifecycle stages of one attachment:
 *
 * - ``encoding``  — posted to the Worker / read from disk; chip shows a spinner
 * - ``ready``     — ``dataUrl`` (images/videos) or ``uploadUrl`` (files) available; safe to submit
 * - ``error``     — validation / decode failure; chip shows inline error
 */
export type AttachmentStatus = "encoding" | "ready" | "error";
export type AttachmentKind = "image" | "file";

export interface AttachedAttachment {
  id: string;
  kind: AttachmentKind;
  file: File;
  /** Optimistic ``blob:`` preview URL; revoked on ``remove`` / ``clear`` /
   * unmount. */
  previewUrl?: string;
  status: AttachmentStatus;
  /** Populated when ``status === "ready"`` (images/videos). */
  dataUrl?: string;
  /** Remote tmpfiles.org direct URL when ``status === "ready"`` and
   * ``kind === "file"``. The file bytes are uploaded browser-side and never
   * touch the gateway host. */
  uploadUrl?: string;
  /** Size of the final encoded payload (base64 bytes decoded). */
  encodedBytes?: number;
  /** Whether the Worker re-encoded the image to hit the size budget. */
  normalized?: boolean;
  /** Human-readable validation / encoding error when ``status === "error"``. */
  error?: AttachmentError;
}

export type AttachedImage = AttachedAttachment;

export interface RestoredReadyAttachment {
  dataUrl: string;
  name?: string;
  kind?: AttachmentKind;
}

export type RestoredReadyImage = RestoredReadyAttachment;

/** Machine-readable rejection reasons surfaced as inline chip errors.
 *
 * Callers localize these via the ``composer.imageRejected.*`` i18n table. */
export type AttachmentError =
  | "unsupported_type"   // server whitelist excludes this MIME
  | "empty_file"         // backend data-URL decoder rejects empty payloads
  | "too_many_attachments" // per-message cap (4) reached before enqueue
  | "total_too_large"    // decoded attachments exceed the business-policy total
  | "transport_too_large" // projected JSON frame exceeds the transport guard
  | "magic_mismatch"     // extension lies about the real content
  | "decode_failed"      // Worker couldn't decode / re-encode
  | "too_large"          // even after normalization we exceed the budget
  | "io";                // file read failed at the browser layer

export const MAX_ATTACHMENTS_PER_MESSAGE = 4;
export const MAX_ATTACHMENT_BYTES = 28 * 1024 * 1024;
export const MAX_TOTAL_ATTACHMENT_BYTES = 28 * 1024 * 1024;

/** MIME whitelist — mirrors the server's and the ``<input accept>`` attr. */
const ACCEPTED_IMAGE_MIMES: ReadonlySet<string> = new Set([
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
]);

const DOCUMENT_MIME_BY_EXTENSION: ReadonlyMap<string, string> = new Map([
  [".pdf", "application/pdf"],
  [".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
  [".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
  [".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"],
  [".txt", "text/plain"],
  [".md", "text/markdown"],
  [".csv", "text/csv"],
  [".json", "application/json"],
  [".xml", "application/xml"],
  [".html", "text/html"],
  [".htm", "text/html"],
  [".log", "text/plain"],
  [".yaml", "application/yaml"],
  [".yml", "application/yaml"],
  [".toml", "application/toml"],
  [".ini", "text/plain"],
  [".cfg", "text/plain"],
]);

const ACCEPTED_DOCUMENT_MIMES: ReadonlySet<string> = new Set(DOCUMENT_MIME_BY_EXTENSION.values());

/** Archives, installers, executables, and other binary blobs (zip, apk, …).
 * Generic-file support so the WebUI accepts ANY user file instead of showing
 * "Unsupported file type". Mirrors the server-side binary allow-set. */
const BINARY_MIME_BY_EXTENSION: ReadonlyMap<string, string> = new Map([
  [".zip", "application/zip"],
  [".gz", "application/gzip"],
  [".tgz", "application/gzip"],
  [".tar", "application/x-tar"],
  [".bz2", "application/x-bzip2"],
  [".7z", "application/x-7z-compressed"],
  [".rar", "application/vnd.rar"],
  [".xz", "application/x-xz"],
  [".apk", "application/vnd.android.package-archive"],
  [".deb", "application/vnd.debian.binary-package"],
  [".dmg", "application/x-apple-diskimage"],
  [".exe", "application/x-msdownload"],
  [".msi", "application/x-msi"],
  [".jar", "application/java-archive"],
  [".pyc", "application/x-python-code"],
]);

const ACCEPTED_BINARY_MIMES: ReadonlySet<string> = new Set([
  "application/octet-stream",
  ...BINARY_MIME_BY_EXTENSION.values(),
]);

export const ACCEPT_ATTR = [
  ...ACCEPTED_IMAGE_MIMES,
  ...ACCEPTED_DOCUMENT_MIMES,
  ...ACCEPTED_BINARY_MIMES,
  ...DOCUMENT_MIME_BY_EXTENSION.keys(),
  ...BINARY_MIME_BY_EXTENSION.keys(),
].join(",");

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot).toLowerCase();
}

function mimeForFile(file: File): string {
  const byName =
    DOCUMENT_MIME_BY_EXTENSION.get(extensionOf(file.name)) ??
    BINARY_MIME_BY_EXTENSION.get(extensionOf(file.name));
  if (byName) return byName;
  if (!file.type || file.type === "application/octet-stream") {
    return "application/octet-stream";
  }
  return file.type;
}

function projectedDataUrlBytes(
  file: File,
  kind: AttachmentKind,
  maxFileBytes: number,
): number {
  const prefixBytes = `data:${mimeForFile(file)};base64,`.length;
  const decodedBytes = kind === "image" ? Math.min(file.size, maxFileBytes) : file.size;
  return prefixBytes + 4 * Math.ceil(decodedBytes / 3);
}

function positiveLimit(value: number | null | undefined, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.floor(value)
    : fallback;
}

function attachmentPayloadBudget(limits: WebUIIngressLimits | null | undefined): number | null {
  const maxFrameBytes = limits?.transport.max_frame_bytes;
  if (typeof maxFrameBytes !== "number" || !Number.isFinite(maxFrameBytes)) {
    return null;
  }
  return Math.max(
    0,
    Math.floor(maxFrameBytes)
      - positiveLimit(limits?.message.max_text_bytes, 0)
      - positiveLimit(limits?.transport.envelope_reserve_bytes, 0),
  );
}

export function acceptedAttachmentKind(file: File): AttachmentKind | null {
  if (DOCUMENT_MIME_BY_EXTENSION.has(extensionOf(file.name))) return "file";
  if (BINARY_MIME_BY_EXTENSION.has(extensionOf(file.name))) return "file";
  if (ACCEPTED_IMAGE_MIMES.has(file.type)) return "image";
  const mime = mimeForFile(file);
  if (ACCEPTED_DOCUMENT_MIMES.has(mime)) return "file";
  if (ACCEPTED_BINARY_MIMES.has(mime)) return "file";
  // Fallback: any non-empty binary blob is accepted as a generic file so the
  // composer never blocks a legitimate user file with "Unsupported file type".
  if (mime === "application/octet-stream" || (!file.type && file.size > 0)) return "file";
  return null;
}

function dataUrlMime(dataUrl: string): string {
  const match = /^data:([^;,]+)[;,]/.exec(dataUrl);
  return match?.[1] || "image/png";
}

function kindFromDataUrl(dataUrl: string): AttachmentKind {
  return dataUrlMime(dataUrl).startsWith("image/") ? "image" : "file";
}

function dataUrlToFile(dataUrl: string, name?: string): File {
  const mime = dataUrlMime(dataUrl);
  const fallbackName = `image.${mime.split("/")[1] || "png"}`;
  try {
    const [, base64 = ""] = dataUrl.split(",", 2);
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return new File([bytes], name || fallbackName, { type: mime });
  } catch {
    return new File([], name || fallbackName, { type: mime });
  }
}

function uuid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return (crypto as Crypto).randomUUID();
  }
  return `img-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function mapEncodeFailure(reason: EncodeFailure["reason"]): AttachmentError {
  switch (reason) {
    case "invalid_mime":
    case "magic_mismatch":
      return "magic_mismatch";
    case "too_large_after_normalize":
      return "too_large";
    case "io":
      return "io";
    case "decode_failed":
    default:
      return "decode_failed";
  }
}

export interface UseAttachedImagesApi {
  images: AttachedAttachment[];
  /** Enqueue new files. Returns the list of rejected files so the caller can
   * surface inline errors. Files rejected client-side (wrong MIME, limit) are
   * *not* added to ``images`` — only recoverable read/encoding failures show
   * up as error chips. */
  enqueue: (files: Iterable<File>) => {
    rejected: Array<{ file: File; reason: AttachmentError }>;
  };
  remove: (id: string) => { nextFocusId: string | null };
  /** Revoke every staged blob URL and drop all attachments. Called after a
   * successful submit — the optimistic bubble holds onto an independent
   * ``data:`` URL so tearing down blob previews here is safe. */
  clear: () => void;
  /** Restore already-encoded attachments, e.g. a queued composer draft moving
   * back into the input. These entries are immediately sendable and use image
   * ``data:`` URLs as stable previews. */
  restoreReadyImages: (images: RestoredReadyAttachment[]) => void;
  /** ``true`` when at least one attachment is still encoding — Send should wait. */
  encoding: boolean;
  /** ``true`` when we've hit ``MAX_ATTACHMENTS_PER_MESSAGE``. */
  full: boolean;
}

interface UseAttachedImagesOptions {
  ingressLimits?: WebUIIngressLimits | null;
}

/** Manage the lifecycle of attachments in the Composer.
 *
 * Responsibilities in one place:
 *   - validation (MIME whitelist, count cap)
 *   - blob URL creation + revocation
 *   - Worker orchestration
 *   - focus bookkeeping so keyboard delete doesn't strand the user
 */
export function useAttachedImages({
  ingressLimits = null,
}: UseAttachedImagesOptions = {}): UseAttachedImagesApi {
  const [images, setImages] = useState<AttachedAttachment[]>([]);
  const maxAttachments = positiveLimit(
    ingressLimits?.attachments.max_count,
    MAX_ATTACHMENTS_PER_MESSAGE,
  );
  const maxFileBytes = positiveLimit(
    ingressLimits?.attachments.max_file_bytes,
    MAX_ATTACHMENT_BYTES,
  );
  const maxTotalBytes = positiveLimit(
    ingressLimits?.attachments.max_total_bytes,
    MAX_TOTAL_ATTACHMENT_BYTES,
  );
  // Ref mirror so ``enqueue`` can see the authoritative length when invoked
  // multiple times in a single tick (rapid file selection, drag of many
  // files, paste storms). ``state`` is stale for that second + call.
  const imagesRef = useRef<AttachedAttachment[]>([]);
  imagesRef.current = images;

  const setEntry = useCallback((id: string, patch: Partial<AttachedAttachment>) => {
    setImages((prev) => {
      const next = prev.map((img) => (img.id === id ? { ...img, ...patch } : img));
      imagesRef.current = next;
      return next;
    });
  }, []);

  const enqueue = useCallback(
    (files: Iterable<File>) => {
      const rejected: Array<{ file: File; reason: AttachmentError }> = [];
      const toAdd: AttachedAttachment[] = [];
      let slot = maxAttachments - imagesRef.current.length;
      const payloadBudget = attachmentPayloadBudget(ingressLimits);
      let projectedWireBytes = imagesRef.current.reduce(
        (total, image) => total + (
          image.kind === "file"
            ? 512
            : (image.dataUrl?.length ?? projectedDataUrlBytes(image.file, image.kind, maxFileBytes))
        ),
        0,
      );
      let projectedDecodedBytes = imagesRef.current.reduce(
        (total, image) => total + (
          image.kind === "file"
            ? 0
            : (image.encodedBytes ?? Math.min(image.file.size, maxFileBytes))
        ),
        0,
      );

      for (const file of files) {
        const kind = acceptedAttachmentKind(file);
        if (!kind) {
          rejected.push({ file, reason: "unsupported_type" });
          continue;
        }
        if (file.size === 0) {
          rejected.push({ file, reason: "empty_file" });
          continue;
        }
        if (kind === "file" && file.size > TMPFILES_MAX_BYTES) {
          rejected.push({ file, reason: "too_large" });
          continue;
        }
        if (slot <= 0) {
          rejected.push({ file, reason: "too_many_attachments" });
          continue;
        }
        const nextDecodedBytes = kind === "file" ? 0 : Math.min(file.size, maxFileBytes);
        if (projectedDecodedBytes + nextDecodedBytes > maxTotalBytes) {
          rejected.push({ file, reason: "total_too_large" });
          continue;
        }
        const nextWireBytes = kind === "file" ? 512 : projectedDataUrlBytes(file, kind, maxFileBytes);
        if (payloadBudget !== null && projectedWireBytes + nextWireBytes > payloadBudget) {
          rejected.push({ file, reason: "transport_too_large" });
          continue;
        }
        slot -= 1;
        projectedDecodedBytes += nextDecodedBytes;
        projectedWireBytes += nextWireBytes;
        toAdd.push({
          id: uuid(),
          kind,
          file,
          ...(kind === "image" ? { previewUrl: URL.createObjectURL(file) } : {}),
          status: "encoding",
        });
      }

      if (toAdd.length > 0) {
        const next = [...imagesRef.current, ...toAdd];
        imagesRef.current = next;
        setImages(next);
        // Fire the Worker after the commit so chips render first (good INP).
        for (const entry of toAdd) {
          queueMicrotask(() => {
            const work: Promise<
              | { ok: true; dataUrl: string; bytes: number; normalized?: boolean }
              | { ok: true; url: string; bytes: number }
              | { ok: false; reason: string }
            > = entry.kind === "image"
              ? encodeImage(entry.file)
              : uploadFileToTmpfiles(entry.file).then((result) =>
                  result.ok
                    ? { ok: true as const, url: result.url, bytes: entry.file.size }
                    : { ok: false as const, reason: result.reason },
                );
            work.then(
              (result) => {
                if (result.ok) {
                  if ("url" in result) {
                    setEntry(entry.id, {
                      status: "ready",
                      uploadUrl: result.url,
                      encodedBytes: result.bytes,
                    });
                  } else {
                    setEntry(entry.id, {
                      status: "ready",
                      dataUrl: result.dataUrl,
                      encodedBytes: result.bytes,
                      normalized: "normalized" in result ? result.normalized : false,
                    });
                  }
                } else {
                  setEntry(entry.id, {
                    status: "error",
                    error: entry.kind === "image"
                      ? mapEncodeFailure(result.reason as EncodeFailure["reason"])
                      : (result.reason as AttachmentError),
                  });
                }
              },
              () => {
                setEntry(entry.id, {
                  status: "error",
                  error: "decode_failed",
                });
              },
            );
          });
        }
      }
      return { rejected };
    },
    [ingressLimits, maxAttachments, maxFileBytes, maxTotalBytes, setEntry],
  );

  const remove = useCallback((id: string) => {
    let nextFocusId: string | null = null;
    setImages((prev) => {
      const idx = prev.findIndex((img) => img.id === id);
      if (idx === -1) return prev;
      const target = prev[idx];
      if (target.previewUrl) {
        try {
          URL.revokeObjectURL(target.previewUrl);
        } catch {
          // No-op: previewUrl revocation is best-effort.
        }
      }
      const next = [...prev.slice(0, idx), ...prev.slice(idx + 1)];
      imagesRef.current = next;
      // Prefer moving focus to the chip at the same index, else previous.
      const candidate = next[idx] ?? next[idx - 1];
      nextFocusId = candidate?.id ?? null;
      return next;
    });
    return { nextFocusId };
  }, []);

  const clear = useCallback(() => {
    setImages((prev) => {
      for (const img of prev) {
        if (img.previewUrl) {
          try {
            URL.revokeObjectURL(img.previewUrl);
          } catch {
            // revoke is best-effort
          }
        }
      }
      imagesRef.current = [];
      return [];
    });
  }, []);

  const restoreReadyImages = useCallback((restored: RestoredReadyAttachment[]) => {
    const toRestore = restored
      .filter((img) => acceptedAttachmentKind(dataUrlToFile(img.dataUrl, img.name)))
      .slice(0, maxAttachments)
      .map((img): AttachedAttachment => {
        const file = dataUrlToFile(img.dataUrl, img.name);
        const kind = img.kind ?? kindFromDataUrl(img.dataUrl);
        return {
          id: uuid(),
          kind,
          file,
          ...(kind === "image" ? { previewUrl: img.dataUrl } : {}),
          status: "ready",
          dataUrl: img.dataUrl,
          encodedBytes: file.size,
        };
      });
    setImages((prev) => {
      for (const img of prev) {
        if (img.previewUrl) {
          try {
            URL.revokeObjectURL(img.previewUrl);
          } catch {
            // revoke is best-effort
          }
        }
      }
      imagesRef.current = toRestore;
      return toRestore;
    });
  }, [maxAttachments]);

  // Final safety net: revoke any outstanding blob URLs on unmount. Safe
  // under StrictMode double-invoke because revoked blob URLs are only
  // referenced from in-hook chip state, which is rebuilt on remount.
  useEffect(() => {
    return () => {
      for (const img of imagesRef.current) {
        if (img.previewUrl) {
          try {
            URL.revokeObjectURL(img.previewUrl);
          } catch {
            // best-effort cleanup on unmount
          }
        }
      }
    };
  }, []);

  const encoding = images.some((img) => img.status === "encoding");
  const full = images.length >= maxAttachments;

  return { images, enqueue, remove, clear, restoreReadyImages, encoding, full };
}
