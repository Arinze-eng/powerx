import { useEffect, useState } from "react";
import { Megaphone } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/** Shape returned by GET /webui/announcement. */
type Announcement = {
  id: string;
  title: string;
  message: string;
  created_at?: string | null;
};

const DISMISS_KEY_PREFIX = "nanobot-webui.dismissed-announcement:";

function readDismissedId(): string {
  try {
    return window.localStorage.getItem(DISMISS_KEY_PREFIX) ?? "";
  } catch {
    return "";
  }
}

function writeDismissedId(id: string): void {
  try {
    window.localStorage.setItem(DISMISS_KEY_PREFIX, id);
  } catch {
    // storage errors are non-fatal
  }
}

/**
 * Public admin announcement shown as a modal dialog on the landing page and the
 * authenticated AI surface. Fetches the newest active announcement from the
 * gateway (no auth required) and displays it once — dismissal is remembered per
 * announcement id so re-showing only happens when an admin posts something new.
 * Renders nothing when there is no announcement or after dismissal.
 */
export function AnnouncementDialog() {
  const [announcement, setAnnouncement] = useState<Announcement | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/webui/announcement", {
          method: "GET",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
        if (!res.ok) return;
        const data = (await res.json()) as { announcement?: Announcement | null };
        const a = data?.announcement ?? null;
        if (cancelled || !a || !a.message) return;
        // Only show if this exact announcement hasn't been dismissed already.
        if (readDismissedId() === a.id) return;
        setAnnouncement(a);
        setOpen(true);
      } catch {
        // Network/format error: silently skip — never block the app.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const dismiss = () => {
    if (announcement?.id) writeDismissedId(announcement.id);
    setOpen(false);
  };

  if (!announcement) return null;

  return (
    <Dialog open={open} onOpenChange={(v) => (v ? setOpen(true) : dismiss())}>
      <DialogContent className="max-w-md rounded-2xl border border-[#E7E4DF] bg-white text-[#0A0A0A]">
        <DialogHeader className="relative">
          <div className="mb-1 inline-flex h-11 w-11 items-center justify-center rounded-xl border border-[#E7E4DF] bg-[#F7F6F4] text-[#0A0A0A]">
            <Megaphone className="h-5 w-5" />
          </div>
          <DialogTitle className="font-serif text-lg font-medium tracking-[-0.01em]">
            {announcement.title}
          </DialogTitle>
          <DialogDescription className="whitespace-pre-wrap pt-1 text-sm leading-relaxed text-[#6B6862]">
            {announcement.message}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="relative">
          <button
            type="button"
            onClick={dismiss}
            className="inline-flex h-10 w-full items-center justify-center rounded-full bg-[#0A0A0A] px-5 text-sm font-medium text-white transition-colors hover:bg-[#1F1F1F] sm:w-auto"
          >
            Got it
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
