"use client";

export type WorkspaceStage = "planning" | "generating" | "validating" | "executing" | "complete";

export function WorkspaceStages({
  stage,
  elapsedSeconds,
  error,
}: {
  stage: WorkspaceStage;
  elapsedSeconds?: number;
  error?: string | null;
}) {
  const stages: WorkspaceStage[] = ["planning", "generating", "validating", "executing", "complete"];
  const active = stages.indexOf(stage);
  return (
    <section className="workflow-stages" aria-label="Query workflow stages">
      <div className="workflow-stepper">
        {stages.map((item, index) => (
          <div className={`workflow-step ${index < active ? "done" : index === active ? "active" : ""}`} key={item}>
            <span>{index < active ? "✓" : index + 1}</span>
            <strong>{item}</strong>
          </div>
        ))}
      </div>
      {elapsedSeconds !== undefined && <small>{elapsedSeconds.toFixed(1)}s elapsed</small>}
      {error && <p className="bad" role="alert">{error}</p>}
    </section>
  );
}
