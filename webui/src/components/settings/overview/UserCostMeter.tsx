import { FileText, Globe2, LayoutGrid, Terminal, Zap, type LucideIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { SettingsPayload, UserCostCounters } from "@/lib/types";

type UsagePayload = NonNullable<SettingsPayload["usage"]>;

const EMPTY_COUNTERS: UserCostCounters = {
  turns: 0,
  api_calls: 0,
  commands: 0,
  pages: 0,
  files: 0,
  steps: 0,
};

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1000)}k`;
  if (value >= 1_000) return `${(value / 1000).toFixed(1)}k`;
  return value.toLocaleString();
}

interface MeterTile {
  key: string;
  icon: LucideIcon;
  label: string;
  hint: string;
  value: number;
  today: number;
}

/**
 * The signed-in user's own cost meter.
 *
 * The four figures are the ones a user can act on: `api_calls` is the expensive
 * half and everything else is local work done between those calls, so the ratio
 * between them is the headline. One model call driving many commands is the cost
 * discipline working, and it is not visible from either number alone.
 *
 * Nothing here is computed on the client. The counts are accumulated host-side
 * from the same classifier the live feed uses, so a command a user watched run
 * is a command in this panel and not a second, differently-derived figure that
 * disagrees with the feed beside it.
 */
export function UserCostMeter({
  usage,
  className,
}: {
  usage?: UsagePayload;
  className?: string;
}) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string, values?: Record<string, unknown>) =>
    t(key, { defaultValue: fallback, ...(values ?? {}) });

  const meter = usage?.meter;

  if (!meter || !meter.metered) {
    // Not "you have spent nothing" — "this deployment could not tell who you
    // are, so there is no meter to show". Reporting the first when the second is
    // true would be a claim the data does not support.
    return (
      <div
        className={cn(
          "flex items-center gap-2 text-[11px] font-normal text-muted-foreground/70",
          className,
        )}
      >
        <LayoutGrid className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>
          {tx("settings.costMeter.unmetered", "Cost metering starts when this account is signed in.")}
        </span>
      </div>
    );
  }

  const totals: UserCostCounters = meter.totals ?? EMPTY_COUNTERS;
  const today: UserCostCounters = meter.today ?? EMPTY_COUNTERS;
  const windowTotals: UserCostCounters = meter.window ?? EMPTY_COUNTERS;
  const windowDays = meter.window_days ?? 30;
  const commandsPerCall = meter.efficiency?.commands_per_api_call ?? null;

  const tiles: MeterTile[] = [
    {
      key: "api_calls",
      icon: Zap,
      label: tx("settings.costMeter.apiCalls", "API calls"),
      value: totals.api_calls,
      today: today.api_calls,
      hint: tx(
        "settings.costMeter.apiCallsHint",
        "Requests that reached the configured model. Everything else here is local work done between them.",
      ),
    },
    {
      key: "commands",
      icon: Terminal,
      label: tx("settings.costMeter.commands", "Commands"),
      value: totals.commands,
      today: today.commands,
      hint: tx(
        "settings.costMeter.commandsHint",
        "Shell, sandbox and hosted-runner steps the agent executed.",
      ),
    },
    {
      key: "pages",
      icon: Globe2,
      label: tx("settings.costMeter.pages", "Pages"),
      value: totals.pages,
      today: today.pages,
      hint: tx("settings.costMeter.pagesHint", "Pages the agent opened or fetched."),
    },
    {
      key: "files",
      icon: FileText,
      label: tx("settings.costMeter.files", "Files"),
      value: totals.files,
      today: today.files,
      hint: tx("settings.costMeter.filesHint", "Files created or modified, counted once each."),
    },
  ];

  return (
    <TooltipProvider delayDuration={120} skipDelayDuration={80}>
      <div className={cn("min-w-0", className)}>
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
          <span className="text-[11px] font-normal leading-none text-muted-foreground/64">
            {tx("settings.costMeter.title", "Your usage")}
          </span>
          <span className="text-[11px] font-normal leading-none text-muted-foreground/56">
            {tx("settings.costMeter.stepsCaption", "{{steps}} steps · {{turns}} turns", {
              steps: formatCount(totals.steps),
              turns: formatCount(totals.turns),
            })}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 sm:gap-3">
          {tiles.map((tile) => (
            <Tooltip key={tile.key}>
              <TooltipTrigger asChild>
                <div className="rounded-lg bg-black/[0.03] px-3 py-2.5 dark:bg-white/[0.04]">
                  <div className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground/70">
                    <tile.icon className="h-3 w-3 shrink-0" aria-hidden />
                    <span className="truncate">{tile.label}</span>
                  </div>
                  <div className="mt-1 text-[19px] font-semibold leading-none tabular-nums">
                    {formatCount(tile.value)}
                  </div>
                  <div className="mt-1 text-[10px] font-normal leading-none text-muted-foreground/60">
                    {tx("settings.costMeter.todayCount", "+{{count}} today", {
                      count: formatCount(tile.today),
                    })}
                  </div>
                </div>
              </TooltipTrigger>
              <TooltipContent
                side="top"
                align="center"
                className="max-w-60 px-2.5 py-1.5 text-[11px] font-normal"
              >
                {tile.hint}
              </TooltipContent>
            </Tooltip>
          ))}
        </div>

        <p className="mt-2.5 text-[11px] font-normal leading-relaxed text-muted-foreground/70">
          {commandsPerCall !== null
            ? tx(
                "settings.costMeter.efficiency",
                "{{ratio}} commands per API call — the higher this is, the less of your spend went to the model.",
                { ratio: commandsPerCall },
              )
            : tx("settings.costMeter.noCalls", "No model call has been made for this account yet.")}
        </p>

        <p className="mt-2 text-[11px] font-normal leading-relaxed text-muted-foreground/56">
          {tx(
            "settings.costMeter.window",
            "Last {{days}} days: {{calls}} API calls, {{commands}} commands, {{pages}} pages, {{files}} files.",
            {
              days: windowDays,
              calls: formatCount(windowTotals.api_calls),
              commands: formatCount(windowTotals.commands),
              pages: formatCount(windowTotals.pages),
              files: formatCount(windowTotals.files),
            },
          )}
        </p>
      </div>
    </TooltipProvider>
  );
}
