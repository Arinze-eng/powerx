import { useState } from 'react';
import { CheckCircle2, Clock, PlayCircle, AlertCircle, Layers, Cpu, Sliders } from 'lucide-react';

export interface PlanStep {
  id: string;
  title: string;
  status: 'pending' | 'running' | 'completed' | 'failed';
  detail?: string;
}

interface ManusTaskPlannerProps {
  steps: PlanStep[];
  activeStepId?: string;
  tokenUsage?: { estimated_tokens: number; max_tokens: number; usage_percent: number };
  onPreferenceChange?: (key: string, value: any) => void;
}

export function ManusTaskPlanner({
  steps,
  tokenUsage
}: ManusTaskPlannerProps) {
  const [preferences, setPreferences] = useState({
    autonomousMode: true,
    cacheEnabled: true,
    maxBudgetTokens: 128000
  });

  return (
    <div className="rounded-xl border bg-card p-5 text-card-shadow shadow-sm space-y-4">
      <div className="flex items-center justify-between border-b pb-3">
        <div className="flex items-center gap-2">
          <Layers className="h-5 w-5 text-primary animate-pulse" />
          <h3 className="font-semibold text-base">Manus-Style Autonomous Execution Plan</h3>
        </div>
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1 bg-muted px-2 py-1 rounded-md">
            <Cpu className="h-3.5 w-3.5" /> Tokens: {tokenUsage ? `${tokenUsage.estimated_tokens.toLocaleString()} / ${tokenUsage.max_tokens.toLocaleString()} (${tokenUsage.usage_percent}%)` : "—"}
          </span>
        </div>
      </div>

      {/* Steps List */}
      <div className="space-y-2.5">
        {steps.map((step, idx) => {
          let icon = <Clock className="h-4 w-4 text-muted-foreground" />;
          let statusClass = "border-border/50 bg-muted/20 text-muted-foreground";

          if (step.status === 'completed') {
            icon = <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
            statusClass = "border-emerald-500/30 bg-emerald-500/5 text-emerald-900 dark:text-emerald-200";
          } else if (step.status === 'running') {
            icon = <PlayCircle className="h-4 w-4 text-blue-500 animate-spin" />;
            statusClass = "border-blue-500/40 bg-blue-500/10 text-blue-900 dark:text-blue-200 font-medium";
          } else if (step.status === 'failed') {
            icon = <AlertCircle className="h-4 w-4 text-red-500" />;
            statusClass = "border-red-500/30 bg-red-500/5 text-red-900 dark:text-red-200";
          }

          return (
            <div key={step.id || idx} className={`flex items-start gap-3 p-3 rounded-lg border transition-all ${statusClass}`}>
              <div className="mt-0.5 shrink-0">{icon}</div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">
                    {idx + 1}. {step.title}
                  </span>
                  <span className="text-[10px] uppercase px-1.5 py-0.5 rounded font-mono bg-background/60 border">
                    {step.status}
                  </span>
                </div>
                {step.detail && <p className="text-xs opacity-80 mt-0.5">{step.detail}</p>}
              </div>
            </div>
          );
        })}
      </div>

      {/* Preferences & Controls */}
      <div className="pt-3 border-t flex items-center justify-between text-xs text-muted-foreground">
        <div className="flex items-center gap-2">
          <Sliders className="h-4 w-4" />
          <span>Preference Learning: Active</span>
        </div>
        <div className="flex items-center gap-3">
          <label className="flex items-center gap-1.5 cursor-pointer">
            <input
              type="checkbox"
              checked={preferences.cacheEnabled}
              onChange={(e) => setPreferences({ ...preferences, cacheEnabled: e.target.checked })}
              className="rounded border-input"
            />
            Response Caching
          </label>
          <label className="flex items-center gap-1.5 cursor-pointer">
            <input
              type="checkbox"
              checked={preferences.autonomousMode}
              onChange={(e) => setPreferences({ ...preferences, autonomousMode: e.target.checked })}
              className="rounded border-input"
            />
            Manus Multi-Step Loop
          </label>
        </div>
      </div>
    </div>
  );
}
