"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import Link from "next/link";
import dynamic from "next/dynamic";
import { useRouter } from "next/navigation";
import {
  Activity, AlertTriangle, ArrowDownToLine, ArrowRight, Check, ChevronDown, ChevronRight,
  CircleHelp, Clock3, Copy, Database, FileClock, FileUp, KeyRound, LayoutDashboard, LoaderCircle,
  Maximize2, Minimize2, Moon, Play, Search, Settings2, ShieldCheck, Sparkles, Sun, Table2, Trash2, WandSparkles, X,
} from "lucide-react";
import { api, ApiError, jsonBody } from "@/lib/api";
import { WorkspaceStages, type WorkspaceStage } from "@/components/WorkspaceStages";
import type {
  ActiveDataset, Column, Comparison, DashboardResponse, Dataset, DatasetRowsResponse, HistoryRecord, HistoryResponse,
  PreprocessRule, ProcessedVersion, QualityReport, QueryResult, ReliabilityReport, ReliabilitySummary,
  ReliabilityTest, ReliabilityTestRun, ResultVerification, Schema, SettingsResponse,
  ValidationResult, WorkflowDataset
} from "@/lib/types";
const ActivityChart = dynamic(
  () => import("@/components/ActivityChart").then((module) => module.ActivityChart),
  { ssr: false, loading: () => <div className="skeleton-chart" aria-label="Loading activity chart" /> },
);
const SqlCodeEditor = dynamic(
  () => import("@/components/SqlCodeEditor").then((module) => module.SqlCodeEditor),
  { ssr: false, loading: () => <div className="sql-editor-loading" role="status">Loading SQL editor…</div> },
);
type Page = "dashboard" | "workspace" | "schema" | "history" | "guardrails" | "settings";
type Notice = { kind: "success" | "error" | "info"; text: string } | null;
type UploadInspection = {
  row_count: number;
  col_count: number;
  total_nulls: number;
  columns: Array<{ name: string; dtype: string; null_count: number; null_pct: number; sample_values: unknown[] }>;
};
type UploadResponse = {
  dataset: WorkflowDataset;
  filename: string;
  inspection: UploadInspection;
  preview: { columns: string[]; rows: Array<Record<string, unknown>>; total_rows: number; truncated: boolean };
};
type UploadInfo = Pick<UploadResponse, "filename" | "inspection" | "preview">;
type UploadJob = {
  job_id: string;
  filename: string;
  total_bytes: number;
  status: "queued" | "running" | "completed" | "failed";
  stage: "queued" | "parsing" | "analyzing" | "storing" | "publishing" | "finalizing" | "completed" | "failed";
  processed_rows: number;
  total_rows: number | null;
  elapsed_seconds: number;
  error: string | null;
  result: UploadResponse | null;
};

const workflowStepOrder: Array<{ states: string[]; label: string }> = [
  { states: ["RAW_STORED", "ANALYZING", "RAW_ANALYZED", "PREPROCESSING", "PREPROCESSED", "PROCESSED_STORED"], label: "Raw stored" },
  { states: ["RAW_ANALYZED", "PREPROCESSING", "PREPROCESSED", "PROCESSED_STORED"], label: "Quality analyzed" },
  { states: ["PREPROCESSED", "PROCESSED_STORED"], label: "Preprocessed" },
  { states: ["PROCESSED_STORED"], label: "Processed stored" },
];

function workflowStepIndex(status: string) {
  let index = 0;
  workflowStepOrder.forEach((step, stepIndex) => {
    if (step.states.includes(status)) index = stepIndex + 1;
  });
  return index;
}

const navItems: Array<{ id: Page; label: string; icon: typeof LayoutDashboard }> = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "workspace", label: "SQL Workspace", icon: Activity },
  { id: "schema", label: "Schema Explorer", icon: Database },
  { id: "history", label: "Query History", icon: FileClock },
  { id: "guardrails", label: "Guardrails", icon: ShieldCheck },
  { id: "settings", label: "Settings", icon: Settings2 },
];

const pageTitle: Record<Page, string> = {
  dashboard: "Dashboard",
  workspace: "SQL workspace",
  schema: "Schema explorer",
  history: "Query history",
  guardrails: "Guardrails & validation",
  settings: "Workspace settings",
};

const examples = [
  "Show 10 records",
  "Count all rows",
  "Find missing values in each column",
];

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "An unexpected error occurred.";
}

function traceIdsFromError(error: unknown) {
  if (!(error instanceof ApiError) || !error.payload || typeof error.payload !== "object") return { trace: null as string | null, reliability: null as string | null };
  const detail = "detail" in error.payload ? (error.payload as { detail?: unknown }).detail : error.payload;
  if (!detail || typeof detail !== "object") return { trace: null as string | null, reliability: null as string | null };
  const record = detail as Record<string, unknown>;
  return {
    trace: typeof record.trace_query_id === "string" ? record.trace_query_id : null,
    reliability: typeof record.reliability_id === "string" ? record.reliability_id : null,
  };
}

function normalizedSqlType(type?: string) {
  return type?.trim().toUpperCase() || "UNKNOWN";
}

function prettyTime(value?: number) {
  if (value === undefined || value === null || !Number.isFinite(value)) return "—";
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(2)} s`;
}

function shortDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function relativeTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  if (Math.abs(seconds) < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (Math.abs(seconds) < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  return formatter.format(Math.round(seconds / 86400), "day");
}

function statusClass(value: string) {
  const status = value.toLowerCase();
  return status.includes("success") || status === "pass" || status === "ok"
    ? "good"
    : status.includes("block") || status.includes("fail") || status.includes("error")
      ? "bad"
      : status.includes("warn") || status.includes("confirm")
        ? "warn"
        : "neutral";
}

function Badge({ children, tone }: { children: React.ReactNode; tone?: string }) {
  return <span className={`badge ${tone ?? statusClass(String(children))}`}>{children}</span>;
}

function Panel({
  title, subtitle, actions, children, className = "",
}: {
  title: string; subtitle?: string; actions?: React.ReactNode; children: React.ReactNode; className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {actions && <div className="panel-actions">{actions}</div>}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

function Empty({ title, detail, icon: Icon = Database }: { title: string; detail: string; icon?: typeof Database }) {
  return <div className="empty-state"><Icon size={22} /><strong>{title}</strong><span>{detail}</span></div>;
}

function Metric({ label, value, note, icon: Icon }: { label: string; value: string | number; note: string; icon: typeof Activity }) {
  return (
    <article className="metric panel">
      <span className="metric-icon"><Icon size={16} /></span>
      <span className="eyebrow">{label}</span>
      <strong>{value}</strong>
      <span className="metric-note">{note}</span>
    </article>
  );
}

const dimensionLabels: Record<string, string> = {
  schema_grounding: "Schema grounding",
  question_understanding: "Question understanding",
  sql_requirement_coverage: "SQL requirement coverage",
  result_grounding: "Result grounding",
  unsupported_assumptions: "Unsupported assumptions",
};

const dimensionWeights: Record<string, number> = {
  schema_grounding: 25,
  question_understanding: 20,
  sql_requirement_coverage: 25,
  result_grounding: 20,
  unsupported_assumptions: 10,
};

function formatCompactList(value: unknown) {
  if (Array.isArray(value)) {
    return value.map((item) => typeof item === "string" ? item : JSON.stringify(item)).join(", ") || "None";
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([, left], [, right]) => Number(right) - Number(left))
      .map(([key, count]) => typeof count === "number" ? `${key}: ${count}` : key);
    return entries.join(", ") || "None";
  }
  return "Not provided";
}

function normalizedCheck(value: unknown) {
  if (typeof value === "boolean") return value ? "Passed" : "Failed";
  if (typeof value === "string" && value.trim()) return value.replaceAll("_", " ");
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    if (typeof record.valid === "boolean") return record.valid ? "Passed" : "Failed";
    if (typeof record.status === "string") return record.status.replaceAll("_", " ");
  }
  return "Not evaluated";
}

function ReliabilityPanel({ report, verification }: { report: ReliabilityReport; verification?: ResultVerification | null }) {
  const accuracy = report.answer_accuracy_score;
  const risk = report.hallucination_risk_score;
  const verified = verification ?? report.result_verification ?? null;
  const issueTone = report.critical_failure ? "bad" : "warn";

  return (
    <Panel title="Answer reliability" subtitle="Engineering indicators from deterministic evidence; not calibrated accuracy probabilities" className="reliability-panel">
      {report.critical_failure && <div className="reliability-blocked"><AlertTriangle size={16} /><strong>Rejected / critical reliability failure</strong><span>Do not treat this result as a verified answer.</span></div>}
      <div className="reliability-score-grid">
        <article><span>Answer accuracy</span><strong>{accuracy == null ? "—" : accuracy}</strong><small>/ 100</small></article>
        <article><span>Hallucination risk</span><strong>{risk == null ? "—" : risk}</strong><small>{report.hallucination_risk_level || "Not evaluated"}</small></article>
        <article><span>Validation</span><Badge tone={issueTone}>{report.validation_status || "Not evaluated"}</Badge></article>
        <article><span>Execution</span><Badge tone={statusClass(report.execution_status)}>{report.execution_status || "Not evaluated"}</Badge></article>
      </div>
      <div className="reliability-coverage">
        <span>Evaluated coverage</span>
        <strong>{report.evaluated_weight == null ? "Not evaluated" : `${report.evaluated_weight}%`}</strong>
      </div>
      <div className="reliability-dimensions" aria-label="Reliability dimensions">
        {Object.keys(dimensionWeights).map((key) => {
          const dimension = report.dimensions?.[key];
          const score = dimension?.score;
          const label = dimensionLabels[key] ?? key.replaceAll("_", " ");
          const weight = dimension?.weight ?? dimensionWeights[key];
          return (
            <div className="reliability-dimension" key={key}>
              <div><span>{label}</span><strong>{score == null ? "Not evaluated" : `${score} / ${weight}`}</strong></div>
              {score == null
                ? <div className="dimension-unevaluated">No score returned</div>
                : <div className="dimension-track" aria-label={`${label}: ${score} out of ${weight}`}><span style={{ width: `${Math.max(0, Math.min(100, (score / weight) * 100))}%` }} /></div>}
            </div>
          );
        })}
      </div>
      {report.issues?.length ? (
        <div className="reliability-issues">
          <h3>Detected issues</h3>
          {report.issues.map((issue, index) => (
            <article className={issue.type?.toLowerCase().includes("critical") || report.critical_failure ? "critical" : ""} key={`${issue.type ?? "issue"}-${index}`}>
              <Badge tone={issueTone}>{String(issue.type || "Issue")}</Badge>
              {issue.message && <strong>{issue.message}</strong>}
              {issue.missing_object != null && <p><span>Missing object</span>{String(issue.missing_object)}</p>}
              {issue.original_requirement != null && <p><span>Original requirement</span>{String(issue.original_requirement)}</p>}
              {issue.responsible_sql_section != null && <p><span>Responsible SQL section</span>{String(issue.responsible_sql_section)}</p>}
              {issue.recommended_correction != null && <p><span>Recommended correction</span>{String(issue.recommended_correction)}</p>}
            </article>
          ))}
        </div>
      ) : <p className="muted reliability-empty">No reliability issues were reported by the backend.</p>}
      {verified && (
        <div className="result-verification">
          <h3>Result verification</h3>
          <dl>
            <div><dt>Dataset/table source</dt><dd>{String(verified.database_source ?? verified.source ?? "Not verified")}</dd></div>
            <div><dt>Actual SQL output columns</dt><dd>{Array.isArray(verified.actual_columns) ? verified.actual_columns.join(", ") || "None" : Array.isArray(verified.columns) ? verified.columns.join(", ") || "None" : "Not verified"}</dd></div>
            <div><dt>Output column count</dt><dd>{verified.actual_column_count == null ? (Array.isArray(verified.columns) ? String(verified.columns.length) : "Not verified") : String(verified.actual_column_count)}</dd></div>
            <div><dt>Returned row count</dt><dd>{verified.actual_row_count == null ? (verified.rows_returned == null ? "Not verified" : String(verified.rows_returned)) : String(verified.actual_row_count)}</dd></div>
            <div><dt>Total result count</dt><dd>{verified.total_result_count == null ? "Not verified" : String(verified.total_result_count)}</dd></div>
            <div><dt>Preview truncated</dt><dd>{verified.truncated == null ? "Not verified" : verified.truncated ? "Yes" : "No"}</dd></div>
            <div><dt>Execution duration</dt><dd>{verified.duration_ms == null ? "Not verified" : `${Number(verified.duration_ms).toFixed(2)} ms`}</dd></div>
          </dl>
          {verified.null_counts && typeof verified.null_counts === "object" && (
            <p className="null-counts"><span>Null counts</span>{Object.entries(verified.null_counts).map(([column, count]) => `${column}: ${count}`).join(" · ") || "None"}</p>
          )}
          <p className="muted">Execution verification confirms returned data; it does not prove the SQL answers every part of the question.</p>
        </div>
      )}
    </Panel>
  );
}

function WorkflowSteps({ status }: { status: string }) {
  const activeIndex = workflowStepIndex(status);
  return (
    <ol className="workflow-steps" aria-label="Dataset workflow progress">
      {workflowStepOrder.map((step, index) => (
        <li key={step.label} className={index < activeIndex ? "done" : index === activeIndex ? "current" : ""}>
          <span>{index < activeIndex ? <Check size={12} /> : index + 1}</span>
          <b>{step.label}</b>
          {index < workflowStepOrder.length - 1 && <i />}
        </li>
      ))}
    </ol>
  );
}

function formatMetricValue(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : typeof value === "number" ? value.toLocaleString() : String(value);
}

function QualityReportView({ report }: { report: QualityReport }) {
  const m = report.metrics;
  const cards: Array<{ label: string; value: string; tone?: "good" | "bad" | "warn" }> = [
    { label: "Total rows", value: m.rows.toLocaleString() },
    { label: "Total columns", value: m.columns.toLocaleString() },
    { label: "Total cells", value: m.total_cells.toLocaleString() },
    { label: "Missing cells", value: `${m.missing_cells.toLocaleString()} (${m.missing_pct}%)`, tone: m.missing_cells ? "warn" : "good" },
    { label: "Rows with missing", value: m.rows_with_missing.toLocaleString(), tone: m.rows_with_missing ? "warn" : "good" },
    { label: "Duplicate rows", value: `${m.duplicate_rows.toLocaleString()} (${m.duplicate_pct}%)`, tone: m.duplicate_rows ? "warn" : "good" },
    { label: "Invalid values", value: m.invalid_values.toLocaleString(), tone: m.invalid_values ? "bad" : "good" },
    { label: "Outliers", value: m.outliers.total.toLocaleString() },
    { label: "Total issues", value: m.total_issues.toLocaleString(), tone: m.total_issues ? "warn" : "good" },
  ];
  return (
    <div className="quality-report">
      <div className="quality-cards">
        {cards.map((card) => (
          <div className={`quality-card ${card.tone ?? ""}`} key={card.label}>
            <span className="eyebrow">{card.label}</span>
            <strong>{card.value}</strong>
          </div>
        ))}
      </div>
      <div className="quality-details">
        <div className="table-wrap compact">
          <table>
            <thead><tr><th scope="col">Column</th><th scope="col">Missing</th><th scope="col">Missing %</th></tr></thead>
            <tbody>{Object.entries(m.missing_by_column).map(([column, info]) => (
              <tr key={column}><td>{column}</td><td>{info.missing_count.toLocaleString()}</td><td>{info.missing_pct}%</td></tr>
            ))}</tbody>
          </table>
        </div>
        <div className="quality-notes">
          {Object.keys(m.type_mismatches.by_column).length > 0 && (
            <p><AlertTriangle size={13} /> Type mismatches: {Object.entries(m.type_mismatches.by_column).map(([column, info]) => `${column} (${info.mismatch_count} not ${info.dominant_type})`).join(", ")}</p>
          )}
          {m.key_duplicates && <p><AlertTriangle size={13} /> {m.key_duplicates.criteria}: {m.key_duplicates.extra_rows} extra row(s).</p>}
          {m.constant_columns.length > 0 && <p>Constant columns: {m.constant_columns.join(", ")}.</p>}
          {m.empty_rows > 0 && <p>Completely empty rows: {m.empty_rows.toLocaleString()}.</p>}
          {m.empty_columns.length > 0 && <p>Completely empty columns: {m.empty_columns.join(", ")}.</p>}
          <p className="muted">{m.duplicate_criteria}</p>
          <p className="muted">{m.invalid_definition}</p>
          <p className="muted">{m.issue_counting_rules}</p>
        </div>
      </div>
    </div>
  );
}

const comparisonLabels: Record<string, string> = {
  total_rows: "Total rows",
  total_columns: "Total columns",
  total_cells: "Total cells",
  missing_cells: "Missing cells",
  missing_pct: "Missing %",
  rows_with_missing: "Rows with missing values",
  duplicate_rows: "Duplicate rows",
  duplicate_pct: "Duplicate %",
  invalid_values: "Invalid values",
  type_mismatches: "Type mismatches",
  empty_rows: "Empty rows",
  empty_columns: "Empty columns",
  outliers: "Outliers",
  constant_columns: "Constant columns",
  total_issues: "Total issues",
};

function ComparisonView({ comparison }: { comparison: Comparison }) {
  return (
    <div className="comparison-view">
      <p className={`comparison-banner ${comparison.improved ? "improved" : "not-improved"}`}>
        {comparison.improved
          ? `Total issues decreased from ${formatMetricValue(comparison.total_issues_before)} to ${formatMetricValue(comparison.total_issues_after)}.`
          : `Total issues did not decrease (${formatMetricValue(comparison.total_issues_before)} before, ${formatMetricValue(comparison.total_issues_after)} after). Review the remaining issues below; they are reported as they are.`}
      </p>
      <div className="table-wrap compact">
        <table>
          <thead><tr><th scope="col">Quality metric</th><th scope="col">Before</th><th scope="col">After</th><th scope="col">Change</th></tr></thead>
          <tbody>{comparison.rows.map((row) => {
            const isTotal = row.metric === "total_issues";
            const improved = isTotal && row.delta !== null && row.delta < 0;
            const worsened = isTotal && row.delta !== null && row.delta > 0;
            const change = row.delta === null ? "—" : row.delta > 0 ? `+${row.delta}` : String(row.delta);
            return (
              <tr key={row.metric}>
                <td>{comparisonLabels[row.metric] ?? row.metric}</td>
                <td>{formatMetricValue(row.before)}</td>
                <td>{formatMetricValue(row.after)}</td>
                <td className={improved ? "delta-good" : worsened ? "delta-bad" : "muted"}>{change}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
      {comparison.counting_rules && <p className="muted helper-note">{comparison.counting_rules}</p>}
    </div>
  );
}

function Toast({ notice, onClose }: { notice: Notice; onClose: () => void }) {
  if (!notice) return null;
  return (
    <div className={`toast ${notice.kind}`} role={notice.kind === "error" ? "alert" : "status"}>
      {notice.kind === "success" ? <Check size={16} /> : notice.kind === "error" ? <AlertTriangle size={16} /> : <CircleHelp size={16} />}
      <span>{notice.text}</span>
      <button className="icon-button" onClick={onClose} aria-label="Dismiss notification"><X size={15} /></button>
    </div>
  );
}

function TableView({
  rows, query, compact = false,
}: {
  rows: Array<Record<string, unknown>>; query?: string; compact?: boolean;
}) {
  const [sort, setSort] = useState<{ column: string; direction: "asc" | "desc" } | null>(null);
  const columns = rows.length ? Object.keys(rows[0]) : [];
  const filtered = useMemo(() => {
    const needle = (query ?? "").trim().toLowerCase();
    const matching = !needle ? rows : rows.filter((row) => Object.values(row).some((value) => String(value ?? "").toLowerCase().includes(needle)));
    if (!sort) return matching;
    return [...matching].sort((a, b) => String(a[sort.column] ?? "").localeCompare(String(b[sort.column] ?? ""), undefined, { numeric: true }) * (sort.direction === "asc" ? 1 : -1));
  }, [rows, query, sort]);
  if (!rows.length) return <Empty title="No result rows" detail="The API returned no rows for this query." />;
  return (
    <div className={`table-wrap ${compact ? "compact" : ""}`}>
      <table>
        <thead><tr>{columns.map((column) => (
          <th key={column} scope="col">
            <button onClick={() => { const direction = sort?.column === column && sort.direction === "asc" ? "desc" : "asc"; setSort({ column, direction }); }} aria-label={`Sort by ${column}`}>
              {column}{sort?.column === column ? (sort.direction === "asc" ? " ↑" : " ↓") : ""}
            </button>
          </th>
        ))}</tr></thead>
        <tbody>{filtered.map((row, index) => (
          <tr key={`${index}-${String(row[columns[0]])}`}>{columns.map((column) => (
            <td key={column} title={String(row[column] ?? "")}>{row[column] === null ? <span className="muted">NULL</span> : String(row[column] ?? "")}</td>
          ))}</tr>
        ))}</tbody>
      </table>
      <div className="table-foot">{filtered.length} row{filtered.length === 1 ? "" : "s"} shown</div>
    </div>
  );
}

function DatasetRowsExplorer({ table }: { table: string }) {
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<DatasetRowsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const pageSize = 25;

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    const timer = window.setTimeout(() => {
      const params = new URLSearchParams({
        offset: String(offset),
        limit: String(pageSize),
        search,
      });
      api<DatasetRowsResponse>(`/api/datasets/${encodeURIComponent(table)}/rows?${params}`)
        .then((response) => { if (active) setPage(response); })
        .catch((reason: unknown) => { if (active) { setPage(null); setError(messageOf(reason)); } })
        .finally(() => { if (active) setLoading(false); });
    }, search ? 250 : 0);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [offset, pageSize, search, table]);

  return (
    <section className="dataset-browser" aria-label={`Rows from ${table}`}>
      <div className="dataset-browser-tools">
        <div><strong>Data preview</strong><span>Read-only · 25 rows per page</span></div>
        <label className="searchbox">
          <Search size={14} />
          <input
            aria-label={`Search rows in ${table}`}
            value={search}
            onChange={(event) => { setSearch(event.target.value); setOffset(0); }}
            placeholder="Filter saved rows…"
          />
        </label>
      </div>
      {error ? <div className="connection-warning inline-error" role="alert">{error}</div> :
        loading ? <div className="rows-loading" role="status"><LoaderCircle className="spin" size={15} /> Loading saved rows…</div> :
          page?.rows.length ? <TableView rows={page.rows} /> :
            <Empty
              title={search ? "No matching rows" : "No saved rows"}
              detail={search ? "Try a different search term." : "This table does not contain any records yet."}
            />}
      {!loading && !error && page && <div className="pagination dataset-pagination">
        <span>{page.total_rows ? `${page.offset + 1}–${page.offset + page.rows.length} of ${page.total_rows.toLocaleString()} rows` : "0 rows"}</span>
        <div>
          <button className="button small" disabled={offset === 0} onClick={() => setOffset((current) => Math.max(0, current - pageSize))}>Previous</button>
          <button className="button small" disabled={!page.has_more} onClick={() => setOffset((current) => current + pageSize)}>Next</button>
        </div>
      </div>}
    </section>
  );
}

export function AppView({ page }: { page: Page }) {
  const router = useRouter();
  const [theme, setTheme] = useState<"dark" | "light">("light");
  const [preferencesLoaded, setPreferencesLoaded] = useState(false);
  const [reduceMotion, setReduceMotion] = useState(false);
  const [compact, setCompact] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [dashboard, setDashboard] = useState<DashboardResponse | null>(null);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [activeDataset, setActiveDataset] = useState<ActiveDataset | null>(null);
  const [runtime, setRuntime] = useState<SettingsResponse | null>(null);
  const [health, setHealth] = useState<{ status: string; provider_configured: boolean; model: string } | null>(null);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [schema, setSchema] = useState<Schema | null>(null);
  const [selectedTable, setSelectedTable] = useState("");
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [schemaError, setSchemaError] = useState("");
  const [schemaSearch, setSchemaSearch] = useState("");
  const [schemaTypeFilter, setSchemaTypeFilter] = useState("all");
  const [schemaSort, setSchemaSort] = useState<"name" | "type">("name");
  const [schemaPage, setSchemaPage] = useState(0);
  const [schemaDrawerOpen, setSchemaDrawerOpen] = useState(false);
  const [selectedColumn, setSelectedColumn] = useState<Column | null>(null);
  const schemaDrawerCloseRef = useRef<HTMLButtonElement>(null);
  const schemaBrowseRef = useRef<HTMLButtonElement>(null);
  const schemaPreviousFocusRef = useRef<HTMLElement | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState<string | null>(null);
  const deleteRequestInProgressRef = useRef(false);
  const deleteCancelButtonRef = useRef<HTMLButtonElement>(null);
  const deleteTriggerRef = useRef<HTMLButtonElement | null>(null);

  const [question, setQuestion] = useState("");
  const [sqlText, setSqlText] = useState("");
  const [generatedSql, setGeneratedSql] = useState("");
  const [generatedQuestion, setGeneratedQuestion] = useState("");
  const [busy, setBusy] = useState("");
  const [workspaceStage, setWorkspaceStage] = useState<WorkspaceStage>("planning");
  const [workspaceStageError, setWorkspaceStageError] = useState("");
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [validatedSql, setValidatedSql] = useState("");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [reliability, setReliability] = useState<ReliabilityReport | null>(null);
  const [reliabilityId, setReliabilityId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [pageNumber, setPageNumber] = useState(0);
  const [detail, setDetail] = useState<HistoryRecord | null>(null);
  const [openTables, setOpenTables] = useState<string[]>([]);
  const [guardSql, setGuardSql] = useState("");
  const [guardValidation, setGuardValidation] = useState<ValidationResult | null>(null);
  const [guardValidationError, setGuardValidationError] = useState("");
  const [guardBusy, setGuardBusy] = useState(false);
  const [reliabilitySummary, setReliabilitySummary] = useState<ReliabilitySummary | null>(null);
  const [reliabilityTests, setReliabilityTests] = useState<ReliabilityTest[]>([]);
  const [reliabilityTestResults, setReliabilityTestResults] = useState<ReliabilityTestRun | null>(null);
  const [reliabilityLoading, setReliabilityLoading] = useState(false);
  const [reliabilityError, setReliabilityError] = useState("");
  const [runningTestId, setRunningTestId] = useState<string | null>(null);
  const [maximizedPanel, setMaximizedPanel] = useState<"editor" | "results" | null>(null);
  const [healthCheckComplete, setHealthCheckComplete] = useState(false);
  const [uploadInfo, setUploadInfo] = useState<UploadInfo | null>(null);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadJob, setUploadJob] = useState<UploadJob | null>(null);
  const noticeTimerRef = useRef<number | null>(null);
  const workflowPollTimerRef = useRef<number | null>(null);
  const uploadPollActiveRef = useRef(false);
  const [activeDatasetId, setActiveDatasetId] = useState(() => {
    try {
      return localStorage.getItem("queryglass.workflowDataset") ?? "";
    } catch {
      return "";
    }
  });
  const activeDatasetRef = useRef(activeDatasetId);
  const [activeDatasetIdentity, setActiveDatasetIdentity] = useState(() => {
    try {
      return localStorage.getItem("queryglass.workflowIdentity") ?? "";
    } catch {
      return "";
    }
  });
  const [activeDatasetFilename, setActiveDatasetFilename] = useState(() => {
    try {
      return localStorage.getItem("queryglass.workflowFilename") ?? "";
    } catch {
      return "";
    }
  });
  const [workflow, setWorkflow] = useState<WorkflowDataset | null>(null);
  const [workflowLoading, setWorkflowLoading] = useState(false);
  const [rules, setRules] = useState<PreprocessRule[]>([]);
  const [preprocessOptions, setPreprocessOptions] = useState<Record<string, boolean | string>>({});
  const [preprocessBusy, setPreprocessBusy] = useState(false);
  const [storeBusy, setStoreBusy] = useState(false);
  const [deletingTable, setDeletingTable] = useState("");

  const loadData = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    setHealthCheckComplete(false);
    const [dash, data, hist, settings, status] = await Promise.allSettled([
      api<DashboardResponse>("/api/dashboard"),
      api<{ datasets: Dataset[]; active_dataset?: ActiveDataset | null }>("/api/datasets"),
      api<HistoryResponse>("/api/history?limit=500"),
      api<SettingsResponse>("/api/settings"),
      api<{ status: string; provider_configured: boolean; model: string }>("/api/health"),
    ]);
    let failed = false;
    if (dash.status === "fulfilled") setDashboard(dash.value); else failed = true;
    if (data.status === "fulfilled") {
      setDatasets(data.value.datasets);
      const currentActive = data.value.active_dataset ?? null;
      const nextActiveId = currentActive?.dataset_id ?? "";
      const activeChanged = activeDatasetRef.current !== nextActiveId;
      setActiveDataset(currentActive);
      if (activeChanged) {
        activeDatasetRef.current = nextActiveId;
        setActiveDatasetId(nextActiveId);
        setActiveDatasetIdentity(currentActive?.dataset_identity ?? "");
        setActiveDatasetFilename(currentActive?.filename ?? "");
        setWorkflow(null);
        setUploadInfo(null);
        setSchema(null);
        setSchemaError("");
        setSchemaDrawerOpen(false);
        setSchemaSearch("");
        setSchemaTypeFilter("all");
        setSchemaPage(0);
        setSelectedColumn(null);
        setOpenTables([]);
        setResult(null);
        setValidation(null);
        setValidatedSql("");
        setReliability(null);
        setReliabilityId(null);
        setGeneratedSql("");
        setGeneratedQuestion("");
        setQuestion("");
        setSqlText("");
        setSearch("");
        setStatusFilter("all");
        setPageNumber(0);
        setDetail(null);
        setGuardValidation(null);
        setGuardValidationError("");
        setGuardSql("");
      }
      let savedTable = "";
      try {
        savedTable = localStorage.getItem("queryglass.selectedTable") ?? "";
      } catch {
        savedTable = "";
      }
      setSelectedTable((current) => {
        const preferred = savedTable || current;
        if (activeChanged && currentActive?.raw_table) return currentActive.raw_table;
        return data.value.datasets.some((item) => item.name === preferred)
          ? preferred
          : data.value.datasets[0]?.name ?? "";
      });
    } else failed = true;
    if (hist.status === "fulfilled") setHistory(hist.value); else failed = true;
    if (settings.status === "fulfilled") setRuntime(settings.value);
    if (status.status === "fulfilled") setHealth(status.value);
    setHealthCheckComplete(true);
    if (failed) setLoadError("Some live data could not be loaded. Check the API connection and retry; unavailable metrics are not replaced with demo values.");
    setLoading(false);
  }, []);

  useEffect(() => {
    try {
      const preferences = JSON.parse(localStorage.getItem("queryglass.preferences") ?? "{}") as { theme?: "dark" | "light"; reduceMotion?: boolean; compact?: boolean };
      setTheme(preferences.theme ?? "light");
      setReduceMotion(preferences.reduceMotion ?? window.matchMedia("(prefers-reduced-motion: reduce)").matches);
      setCompact(Boolean(preferences.compact));
      const reused = sessionStorage.getItem("queryglass.reopen");
      if (reused) {
        const item = JSON.parse(reused) as { question?: string; sql?: string };
        setQuestion(item.question ?? "");
        setSqlText(item.sql ?? "");
        sessionStorage.removeItem("queryglass.reopen");
      }
    } catch {
      setReduceMotion(window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } finally {
      setPreferencesLoaded(true);
    }
    void loadData();
  }, [loadData]);

  useEffect(() => {
    if (page !== "guardrails") return;
    let active = true;
    setReliabilityLoading(true);
    setReliabilityError("");
    Promise.allSettled([
      api<ReliabilitySummary>("/api/reliability/summary"),
      api<{ items: ReliabilityTest[] }>("/api/reliability/tests"),
    ]).then(([summary, tests]) => {
      if (!active) return;
      if (summary.status === "fulfilled") setReliabilitySummary(summary.value);
      else setReliabilitySummary(null);
      if (tests.status === "fulfilled") setReliabilityTests(tests.value.items ?? []);
      else setReliabilityTests([]);
      if (summary.status === "rejected" || tests.status === "rejected") {
        setReliabilityError("Reliability monitoring data could not be loaded. The backend endpoints may be unavailable.");
      }
      setReliabilityLoading(false);
    });
    return () => { active = false; };
  }, [page]);

  const loadWorkflowSnapshot = useCallback(async (datasetId: string) => {
    if (!datasetId) { setWorkflow(null); setActiveDatasetIdentity(""); setActiveDatasetFilename(""); return; }
    setWorkflowLoading(true);
    try {
      const snapshot = await api<WorkflowDataset>(`/api/datasets/workflow/${encodeURIComponent(datasetId)}`);
      setWorkflow(snapshot);
      setActiveDatasetIdentity(snapshot.dataset_identity);
      setActiveDatasetFilename(snapshot.filename);
      // Interrupted analyses recover in the background; poll while a
      // long-running state is active so the view resolves on its own.
      if (snapshot.status === "ANALYZING" || snapshot.status === "PREPROCESSING") {
        if (workflowPollTimerRef.current !== null) window.clearTimeout(workflowPollTimerRef.current);
        workflowPollTimerRef.current = window.setTimeout(() => { void loadWorkflowSnapshot(datasetId); }, 2500);
      }
    } catch (error) {
      setWorkflow(null);
      setNotice({ kind: "error", text: messageOf(error) });
    } finally {
      setWorkflowLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadWorkflowSnapshot(activeDatasetId);
    return () => {
      if (workflowPollTimerRef.current !== null) {
        window.clearTimeout(workflowPollTimerRef.current);
        workflowPollTimerRef.current = null;
      }
    };
  }, [activeDatasetId, loadWorkflowSnapshot]);


  useEffect(() => {
    try {
      if (activeDatasetId) localStorage.setItem("queryglass.workflowDataset", activeDatasetId);
      else localStorage.removeItem("queryglass.workflowDataset");
      if (activeDatasetIdentity) localStorage.setItem("queryglass.workflowIdentity", activeDatasetIdentity);
      else localStorage.removeItem("queryglass.workflowIdentity");
      if (activeDatasetFilename) localStorage.setItem("queryglass.workflowFilename", activeDatasetFilename);
      else localStorage.removeItem("queryglass.workflowFilename");
    } catch {
      // Persistence of the active dataset is best-effort.
    }
  }, [activeDatasetFilename, activeDatasetId, activeDatasetIdentity]);

  useEffect(() => {
    let active = true;
    api<{ rules: PreprocessRule[] }>("/api/datasets/preprocessing-rules")
      .then((response) => {
        if (!active) return;
        setRules(response.rules);
        const defaults: Record<string, boolean | string> = {};
        response.rules.forEach((rule) => { defaults[rule.option_key] = rule.default; });
        setPreprocessOptions(defaults);
      })
      .catch(() => { /* the configuration panel falls back to backend defaults */ });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!preferencesLoaded) return;
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.motion = reduceMotion ? "reduce" : "full";
    localStorage.setItem("queryglass.preferences", JSON.stringify({ theme, reduceMotion, compact }));
  }, [theme, reduceMotion, compact, preferencesLoaded]);

  useEffect(() => {
    if (selectedTable) localStorage.setItem("queryglass.selectedTable", selectedTable);
  }, [selectedTable]);

  useEffect(() => {
    if (page !== "schema" || !datasets.length) return;
    const requestedTable = new URLSearchParams(window.location.search).get("table");
    if (requestedTable && datasets.some((item) => item.name === requestedTable)) {
      setSelectedTable(requestedTable);
      setOpenTables((items) => items.includes(requestedTable) ? items : [...items, requestedTable]);
      window.setTimeout(() => {
        document.getElementById(`schema-card-${encodeURIComponent(requestedTable)}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
      }, 0);
    }
  }, [datasets, page]);

  useEffect(() => {
    if (page !== "schema" || !selectedTable || !openTables.includes(selectedTable)) return;
    window.setTimeout(() => {
      document.getElementById(`schema-card-${encodeURIComponent(selectedTable)}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  }, [openTables, page, selectedTable]);

  useEffect(() => {
    if (!selectedTable) {
      setSchema(null);
      setSchemaLoading(false);
      setSchemaError("");
      return;
    }
    let active = true;
    setSchema(null);
    setSchemaLoading(true);
    setSchemaError("");
    api<Schema>(`/api/datasets/${encodeURIComponent(selectedTable)}/schema`)
      .then((value) => { if (active) setSchema(value); })
      .catch((error: unknown) => {
        if (active) {
          const message = messageOf(error);
          setSchema(null);
          setSchemaError(message);
          setNotice({ kind: "error", text: message });
        }
      })
      .finally(() => { if (active) setSchemaLoading(false); });
    return () => { active = false; };
  }, [selectedTable]);

  useEffect(() => {
    setSchemaPage(0);
    setSchemaTypeFilter("all");
    setSelectedColumn(null);
  }, [selectedTable]);

  useEffect(() => {
    if (!schemaDrawerOpen) return;
    schemaPreviousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => schemaDrawerCloseRef.current?.focus(), 0);
    const handleDrawerKeys = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setSchemaDrawerOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const drawer = document.getElementById("schema-drawer");
      const focusable = drawer?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleDrawerKeys);
    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleDrawerKeys);
      schemaPreviousFocusRef.current?.focus();
    };
  }, [schemaDrawerOpen]);

  useEffect(() => {
    if (!deleteConfirmation) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const focusTimer = window.setTimeout(() => deleteCancelButtonRef.current?.focus(), 0);
    const handleDeleteDialogKeys = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (!deleteRequestInProgressRef.current) {
          event.preventDefault();
          setDeleteConfirmation(null);
        }
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = document.getElementById("delete-dataset-dialog");
      const focusable = dialog?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!dialog?.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleDeleteDialogKeys);
    return () => {
      window.clearTimeout(focusTimer);
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", handleDeleteDialogKeys);
      const trigger = deleteTriggerRef.current;
      if (trigger?.isConnected) trigger.focus();
      else document.querySelector<HTMLElement>(".page-bar h1")?.focus();
      deleteTriggerRef.current = null;
    };
  }, [deleteConfirmation]);

  useEffect(() => {
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (deleteConfirmation) return;
      if (schemaDrawerOpen) setSchemaDrawerOpen(false);
      else if (detail) setDetail(null);
      else if (maximizedPanel) setMaximizedPanel(null);
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [deleteConfirmation, detail, maximizedPanel, schemaDrawerOpen]);

  useEffect(() => {
    const moveAmbientLight = (event: PointerEvent) => {
      document.documentElement.style.setProperty("--pointer-x", `${event.clientX}px`);
      document.documentElement.style.setProperty("--pointer-y", `${event.clientY}px`);
    };
    window.addEventListener("pointermove", moveAmbientLight, { passive: true });
    return () => window.removeEventListener("pointermove", moveAmbientLight);
  }, []);

  const updateNotice = (kind: "success" | "error" | "info", text: string) => {
    if (noticeTimerRef.current !== null) window.clearTimeout(noticeTimerRef.current);
    setNotice({ kind, text });
    noticeTimerRef.current = window.setTimeout(() => {
      setNotice((current) => current?.text === text ? null : current);
      noticeTimerRef.current = null;
    }, 4500);
  };

  useEffect(() => () => {
    if (noticeTimerRef.current !== null) window.clearTimeout(noticeTimerRef.current);
    if (workflowPollTimerRef.current !== null) window.clearTimeout(workflowPollTimerRef.current);
    uploadPollActiveRef.current = false;
  }, []);

  const queryTable = (() => {
  const availableTables = new Set(datasets.map((item) => item.name));

  if (selectedTable && availableTables.has(selectedTable)) {
    return selectedTable;
  }

  if (activeDataset?.raw_table && availableTables.has(activeDataset.raw_table)) {
    return activeDataset.raw_table;
  }

  // Use the first available table if no valid selection exists.
  return datasets[0]?.name ?? "";
})();

  const selectedPage = pageTitle[page];

  async function generateSql(event?: FormEvent) {
    event?.preventDefault();
    if (!question.trim() || !queryTable) return;
    if (question.trim() !== generatedQuestion.trim()) {
      setReliability(null);
      setReliabilityId(null);
      }
    setBusy("generate");
    setWorkspaceStage("planning");
    setWorkspaceStageError("");
    setValidation(null);
    setValidatedSql("");
    setResult(null);
    try {
      setWorkspaceStage("generating");
      const response = await api<{
        sql: string;
        explanation: string;
        table_name?: string;
        validation?: ValidationResult;
        reliability?: ReliabilityReport | null;
        reliability_id?: string | null;
      }>("/api/sql/generate", jsonBody({ question, table_name: queryTable || undefined }));
      setSqlText(response.sql);
      setGeneratedSql(response.sql);
      setGeneratedQuestion(question);
      if (response.table_name) setSelectedTable(response.table_name);
      setReliability(response.reliability ?? response.validation?.reliability ?? null);
      setReliabilityId(response.reliability_id ?? null);
      if (response.sql.trim()) {
        setWorkspaceStage("validating");
        updateNotice("success", "SQL was generated. Review it, validate it, and execute it to verify returned data.");
      } else {
        setValidation(response.validation ?? null);
        updateNotice("error", response.explanation || "SQL was not generated. Review the reported schema or request limitation.");
      }
    } catch (error) {
      setWorkspaceStageError(messageOf(error));
      const ids = traceIdsFromError(error);
      if (ids.trace) if (ids.reliability) setReliabilityId(ids.reliability);
      updateNotice("error", messageOf(error));
    } finally {
      setBusy("");
    }
  }

  async function inspectUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!uploadFile) return;
    setUploadBusy(true);
    setUploadInfo(null);
    setUploadJob(null);
    uploadPollActiveRef.current = true;
    try {
      const form = new FormData();
      form.append("file", uploadFile);
      let job = await api<UploadJob>("/api/datasets/upload-jobs", { method: "POST", body: form });
      setUploadJob(job);
      while (uploadPollActiveRef.current && (job.status === "queued" || job.status === "running")) {
        await new Promise((resolve) => window.setTimeout(resolve, 750));
        if (!uploadPollActiveRef.current) return;
        job = await api<UploadJob>(`/api/datasets/upload-jobs/${encodeURIComponent(job.job_id)}`);
        setUploadJob(job);
      }
      if (job.status === "failed") throw new Error(job.error || "Dataset ingestion failed.");
      if (!job.result) throw new Error("Dataset ingestion completed without a result.");
      const response = job.result;
      setActiveDatasetId(response.dataset.dataset_id);
      setActiveDatasetIdentity(response.dataset.dataset_identity);
      setActiveDatasetFilename(response.dataset.filename);
      await loadData();
      setUploadInfo({ filename: response.filename, inspection: response.inspection, preview: response.preview });
      updateNotice(
        response.dataset.status === "FAILED"
          ? "error"
          : "success",
        response.dataset.status === "FAILED"
          ? "Upload failed to persist. Preprocessing stays disabled until a valid raw dataset is stored."
          : "Raw data stored in the database. Review the quality report; preprocessing is optional.",
      );
    } catch (error) {
      updateNotice("error", messageOf(error));
    } finally {
      uploadPollActiveRef.current = false;
      setUploadBusy(false);
    }
  }

  async function runPreprocess() {
    if (!workflow || !workflow.allowed_actions.preprocess || preprocessBusy) return;
    setPreprocessBusy(true);
    try {
      const snapshot = await api<WorkflowDataset>(
        `/api/datasets/workflow/${encodeURIComponent(workflow.dataset_id)}/preprocess`,
        jsonBody({ options: preprocessOptions }),
      );
      setWorkflow(snapshot);
      updateNotice(
        snapshot.comparison?.improved ? "success" : "info",
        snapshot.comparison?.improved
          ? "Preprocessing complete. Compare the before/after quality report below."
          : "Preprocessing finished. The total issue count did not decrease; review the comparison before storing.",
      );
    } catch (error) {
      updateNotice("error", messageOf(error));
      void loadWorkflowSnapshot(workflow.dataset_id);
    } finally {
      setPreprocessBusy(false);
    }
  }

  async function storeProcessed() {
    const run = workflow?.latest_run;
    if (!workflow || !run || !workflow.allowed_actions.store_processed || storeBusy) return;
    setStoreBusy(true);
    try {
      const response = await api<{ stored: boolean; already_stored: boolean; replaced_previous: boolean; version: ProcessedVersion }>(
        `/api/datasets/workflow/${encodeURIComponent(workflow.dataset_id)}/runs/${encodeURIComponent(run.run_id)}/store`,
        { method: "POST" },
      );
      if (!response.stored && !response.already_stored) throw new Error("The cleaned dataset was not stored.");
      setSelectedTable(response.version.table_name);
      localStorage.setItem("queryglass.selectedTable", response.version.table_name);
      updateNotice(
        "success",
        response.already_stored
          ? `This cleaned dataset is already saved as "${response.version.table_name}".`
          : `Cleaned dataset saved as "${response.version.table_name}" (${response.version.row_count.toLocaleString()} rows)${response.replaced_previous ? "; the previous version was replaced." : "."}`,
      );
      await loadWorkflowSnapshot(workflow.dataset_id);
      void loadData();
    } catch (error) {
      updateNotice("error", messageOf(error));
    } finally {
      setStoreBusy(false);
    }

  }

  function closeDeleteConfirmation() {
    if (!deleteRequestInProgressRef.current) setDeleteConfirmation(null);
  }

  async function deleteDataset() {
    const tableName = deleteConfirmation;
    if (!tableName || deleteRequestInProgressRef.current) return;
    deleteRequestInProgressRef.current = true;
    setDeletingTable(tableName);
    try {
      const response = await api<{ deleted: boolean }>(`/api/datasets/${encodeURIComponent(tableName)}`, { method: "DELETE" });
      if (!response.deleted) throw new Error("The dataset was not deleted.");
      setDatasets((items) => items.filter((item) => item.name !== tableName));
      setOpenTables((items) => items.filter((item) => item !== tableName));
      if (selectedTable === tableName) {
        setSelectedTable("");
        setSchema(null);
      }
      if (workflow?.raw_table === tableName) {
        setActiveDatasetId("");
        setActiveDatasetIdentity("");
        setActiveDatasetFilename("");
        setWorkflow(null);
      }
      updateNotice("success", `"${tableName}" was deleted.`);
      void loadData();
      setDeleteConfirmation(null);
    } catch (error) {
      updateNotice("error", messageOf(error));
    } finally {
      deleteRequestInProgressRef.current = false;
      setDeletingTable("");
    }
  }

  async function validateSql(source = sqlText) {
    if (!source.trim() || !queryTable) return null;
    setBusy("validate");
    setWorkspaceStage("validating");
    setWorkspaceStageError("");
    setValidation(null);
    setValidatedSql("");
    try {
      const response = await api<ValidationResult>(
        "/api/sql/validate",
        jsonBody({
          question,
          sql: source,
          table_name: queryTable,
          reliability_id: reliabilityId,
        }),
      );
      setValidation(response);
      setValidatedSql(source);
      setReliability(response.reliability ?? reliability);
      setReliabilityId(response.reliability_id ?? reliabilityId);
      if (!response.valid) {
        const reason = response.confidence_reasons?.find((item) =>
          /changes stored data|destructive|blocked|prohibited|schema|column/i.test(item),
        );
        updateNotice(
          "error",
          reason
            ? `SQL rejected (${response.operation}): ${reason}`
            : `SQL rejected (${response.operation}). Review server-side validation details below.`,
        );
      }
      return response;
    } catch (error) {
      setWorkspaceStageError(messageOf(error));
      const ids = traceIdsFromError(error);
      if (ids.trace) if (ids.reliability) setReliabilityId(ids.reliability);
      updateNotice("error", messageOf(error));
      return null;
    } finally {
      setBusy("");
    }
  }

  async function runSql() {
    if (!queryTable || !sqlText.trim()) return;
    let currentValidation = validation;
    if (!currentValidation || validatedSql !== sqlText) currentValidation = await validateSql();
    if (!currentValidation?.valid) return;
    setBusy("execute");
    setWorkspaceStage("executing");
    setWorkspaceStageError("");
    setResult(null);
    try {
      const response = await api<QueryResult>("/api/sql/execute", jsonBody({
        question,
        sql: sqlText,
        table_name: queryTable,
        reliability_id: currentValidation.reliability_id ?? reliabilityId,
      }));
      if (response.status !== "SUCCESS") throw new Error(`Query did not complete successfully: ${response.status}`);
      setResult(response);
      setValidatedSql(sqlText);
      setReliability(response.reliability ?? reliability);
      setReliabilityId(response.reliability_id ?? reliabilityId);
      setWorkspaceStage("complete");
      updateNotice("success", `Query completed: ${response.row_count ?? 0} row(s).`);
      void loadData();
    } catch (error) {
      setWorkspaceStageError(messageOf(error));
      const ids = traceIdsFromError(error);
      if (ids.trace) if (ids.reliability) setReliabilityId(ids.reliability);
      updateNotice("error", messageOf(error));
    } finally {
      setBusy("");
    }
  }

  async function runReliabilityTests(testId?: string) {
    if (!queryTable) return;
    setReliabilityError("");
    setRunningTestId(testId ?? "all");
    try {
      const response = await api<ReliabilityTestRun>(
        "/api/reliability/tests/run",
        jsonBody({ table_name: queryTable, ...(testId ? { test_id: testId } : {}) }),
      );
      setReliabilityTestResults(response);
      void api<ReliabilitySummary>("/api/reliability/summary")
        .then((summary) => setReliabilitySummary(summary))
        .catch(() => { /* Keep the most recently loaded summary when refresh is unavailable. */ });
      updateNotice("success", `Reliability tests completed: ${response.passed} passed, ${response.failed} failed.`);
    } catch (error) {
      setReliabilityError(messageOf(error));
      updateNotice("error", messageOf(error));
    } finally {
      setRunningTestId(null);
    }
  }

  const historyItems = useMemo(() => history?.items ?? [], [history]);
  const filteredHistory = useMemo(() => historyItems.filter((item) => {
    const matchesSearch = `${item.question} ${item.sql_query} ${item.status}`.toLowerCase().includes(search.toLowerCase());
    return matchesSearch && (statusFilter === "all" || item.status.toLowerCase() === statusFilter);
  }), [historyItems, search, statusFilter]);
  const historyPages = Math.max(1, Math.ceil(filteredHistory.length / 10));
  const visibleHistory = filteredHistory.slice(pageNumber * 10, pageNumber * 10 + 10);
  const filteredDatasets = useMemo(() => datasets.filter((item) => {
    const needle = search.toLowerCase();
    return item.name.toLowerCase().includes(needle) || (item.schema?.columns ?? []).some((column) => `${column.name} ${column.type ?? ""}`.toLowerCase().includes(needle));
  }), [datasets, search]);
  const activeSchema = schema?.table === selectedTable ? schema : null;
  const schemaTypeGroups = useMemo(() => {
    const groups = new Map<string, number>();
    (activeSchema?.columns ?? []).forEach((column) => {
      const type = normalizedSqlType(column.type);
      groups.set(type, (groups.get(type) ?? 0) + 1);
    });
    return Array.from(groups, ([type, count]) => ({ type, count }));
  }, [activeSchema]);
  const sidebarSchemaColumns = useMemo(() => {
    const needle = schemaSearch.trim().toLowerCase();
    return (activeSchema?.columns ?? []).filter((column) =>
      !needle || `${column.name} ${column.type ?? ""}`.toLowerCase().includes(needle),
    );
  }, [activeSchema, schemaSearch]);
  const filteredSchemaColumns = useMemo(() => {
    const needle = schemaSearch.trim().toLowerCase();
    return (activeSchema?.columns ?? [])
      .filter((column) => {
        const type = normalizedSqlType(column.type);
        return (schemaTypeFilter === "all" || type === schemaTypeFilter)
          && (!needle || `${column.name} ${column.type ?? ""}`.toLowerCase().includes(needle));
      })
      .sort((left, right) => {
        const primary = schemaSort === "type"
          ? normalizedSqlType(left.type).localeCompare(normalizedSqlType(right.type), undefined, { numeric: true, sensitivity: "base" })
          : left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: "base" });
        return primary || left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: "base" });
      });
  }, [activeSchema, schemaSearch, schemaSort, schemaTypeFilter]);
  const schemaPageSize = 25;
  const schemaPageCount = Math.max(1, Math.ceil(filteredSchemaColumns.length / schemaPageSize));
  const visibleSchemaColumns = filteredSchemaColumns.slice(schemaPage * schemaPageSize, (schemaPage + 1) * schemaPageSize);

  useEffect(() => {
    setSchemaPage(0);
  }, [schemaSearch, schemaSort, schemaTypeFilter]);

  useEffect(() => {
    setSchemaPage((current) => Math.min(current, schemaPageCount - 1));
  }, [schemaPageCount]);

  const days = useMemo(() => {
    const items = historyItems;
    return Array.from({ length: 14 }, (_, offset) => {
      const date = new Date();
      date.setDate(date.getDate() - (13 - offset));
      const key = date.toLocaleDateString();
      const values = { day: date.toLocaleDateString(undefined, { month: "short", day: "numeric" }), success: 0, blocked: 0, failed: 0 };
      items.forEach((item) => {
        if (new Date(item.timestamp).toLocaleDateString() !== key) return;
        const status = item.status.toLowerCase();
        if (status.includes("success")) values.success += 1;
        else if (status.includes("block")) values.blocked += 1;
        else if (status.includes("fail")) values.failed += 1;
      });
      return values;
    });
  }, [historyItems]);

  function copy(text: string) {
    void navigator.clipboard?.writeText(text).then(() => updateNotice("success", "Copied to clipboard.")).catch(() => updateNotice("error", "Clipboard access is not available."));
  }

  function downloadCsv(rows: Array<Record<string, unknown>>) {
    if (!rows.length) return;
    const columns = Object.keys(rows[0]);
    const quote = (value: unknown) => `"${String(value ?? "").replaceAll('"', '""')}"`;
    const csv = [columns.map(quote).join(","), ...rows.map((row) => columns.map((column) => quote(row[column])).join(","))].join("\r\n");
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "queryglass-results.csv";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  function reopen(item: HistoryRecord) {
    setQuestion("");
    setGeneratedQuestion("");
    setSqlText("");
    setGeneratedSql("");
    setReliability(null);
    setReliabilityId(null);
    setValidation(null);
    setValidatedSql("");
    setResult(null);
    sessionStorage.setItem("queryglass.reopen", JSON.stringify({ question: item.question, sql: item.sql_query }));
    router.push("/workspace");
  }

  function validateDisabledSql(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      void generateSql();
    }
  }

  return (
    <div className="app-frame">
      <nav className="navigation" aria-label="Main navigation">
        <Link className="brand" href="/dashboard" aria-label="QueryGlass AI dashboard">
          <span className="brand-mark"><Database size={17} /></span><span>QueryGlass <i>AI</i></span>
        </Link>
        <div className="nav-links">
          {navItems.map(({ id, label, icon: Icon }) => (
            <Link key={id} href={`/${id}`} className={`nav-link ${page === id ? "active" : ""}`}>
              <Icon size={15} strokeWidth={1.8} /><span>{label}</span>
            </Link>
          ))}
        </div>
        <div className="nav-actions">
          <span className={`connection-pill ${health?.status === "ok" ? "online" : ""}`} title={healthCheckComplete ? "API connection status" : "Checking API connection"}>
            <span className="status-dot" />{!healthCheckComplete ? "Checking API" : health?.status === "ok" ? "API connected" : "API unavailable"}
          </span>
          <button className="icon-button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}>
            {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>
          <Link className="icon-button" href="/settings" aria-label="Settings"><Settings2 size={17} /></Link>
        </div>
      </nav>

      <main className="main-area">
        <header className="page-bar">
          <div><span className="eyebrow">QUERYGLASS / WORKSPACE</span><h1 tabIndex={-1}>{selectedPage}</h1></div>
          <div className="page-actions">
            {page === "dashboard" && <button className="button subtle" onClick={() => router.push("/workspace")}><WandSparkles size={15} /> Start a query</button>}
            <button className="icon-button refresh-button" aria-label="Refresh live data" onClick={() => void loadData()}><Activity size={16} /></button>
          </div>
        </header>

        {loadError && <div className="connection-warning" role="status"><AlertTriangle size={16} />{loadError}<button className="button small" onClick={() => void loadData()}>Retry</button></div>}

        {page === "dashboard" && (
          <div className="page-content dashboard-page">
            <section className="welcome-row">
              <div>
                <p className="eyebrow">DATA / INTELLIGENCE</p>
                <h2>Ask your data in plain English</h2>
                <p className="lead">Generate SQL, check it against guardrails, and review results in one calm flow.</p>
                <button className="button primary" onClick={() => router.push("/workspace")}><Sparkles size={15} /> Start a query <ArrowRight size={15} /></button>
              </div>
              <div className="welcome-stats">
                <div className="ring" style={{ "--rate": `${dashboard?.history.success_rate ?? 0}%` } as React.CSSProperties}>
                  <div><strong>{dashboard ? `${dashboard.history.success_rate.toFixed(1)}%` : "—"}</strong><span>success rate</span></div>
                </div>
                <span className="tiny-label">FROM RECORDED QUERIES</span>
              </div>
            </section>

            <section className="metric-grid" aria-label="Query metrics">
              <Metric label="TOTAL QUERIES" value={dashboard?.history.total_queries ?? "—"} note="Stored in query history" icon={Activity} />
              <Metric label="SUCCESSFUL" value={dashboard?.history.success_count ?? "—"} note="Completed executions" icon={Check} />
              <Metric label="BLOCKED" value={dashboard?.history.blocked_count ?? "—"} note="Rejected by server checks" icon={ShieldCheck} />
              <Metric label="AVG EXECUTION" value={dashboard ? prettyTime(dashboard.history.avg_ms) : "—"} note="Successful runs only" icon={Clock3} />
              <Metric label="SUCCESS RATE" value={dashboard ? `${dashboard.history.success_rate.toFixed(1)}%` : "—"} note="Based on recorded queries" icon={Check} />
            </section>

            <div className="content-grid dashboard-grid">
              <Panel title="Query activity" subtitle="Recorded outcomes · last 14 days" className="activity-panel">
                {loading ? <div className="skeleton-chart" aria-label="Loading activity chart" /> :
                  historyItems.length ? <div className="chart-wrap">
                    <ActivityChart data={days} />
                    <div className="chart-legend"><span><i className="legend success" />Successful</span><span><i className="legend accent" />Blocked</span><span><i className="legend error" />Failed</span></div>
                  </div> : <Empty title="No activity recorded" detail="Real query activity appears here after the first validation or execution." icon={Activity} />}
              </Panel>

              <Panel title="Database connection" subtitle="Live backend diagnostics" className="connection-panel">
                <div className="connection-status"><span className={`status-orb ${dashboard?.database.exists ? "live" : ""}`} /><div><strong>{dashboard?.database.exists ? "Database available" : dashboard ? "Database unavailable" : "Checking connection"}</strong><span>{dashboard?.database.sqlite_version ? `SQLite ${dashboard.database.sqlite_version}` : "Waiting for API response"}</span></div></div>
                <div className="detail-list">
                  <div><span>Saved tables</span><strong>{dashboard?.tables.length ?? "—"}</strong></div>
                  <div><span>Database size</span><strong>{dashboard ? `${dashboard.database.size_mb.toFixed(2)} MB` : "—"}</strong></div>
                  <div><span>SQL provider</span><Badge tone={health?.provider_configured ? "good" : "warn"}>{health?.provider_configured ? "Configured" : "Not configured"}</Badge></div>
                </div>
                <div className="quick-links"><Link href="/schema"><Table2 size={15} /> Explore schema <ArrowRight size={14} /></Link><Link href="/history"><FileClock size={15} /> Query history <ArrowRight size={14} /></Link></div>
              </Panel>

              <Panel title="Connected datasets" subtitle={`${datasets.length} live dataset${datasets.length === 1 ? "" : "s"}`} className="dataset-panel">
                {datasets.length ? <div className="dataset-list">{datasets.map((item) => {
                  const live = dashboard?.tables.find((table) => table.name === item.name);
                  const columnCount = item.schema?.columns.length ?? live?.columns ?? 0;
                  const rowCount = live?.rows ?? item.rows;
                  return <Link key={item.name} className="dataset-row" href={`/schema?table=${encodeURIComponent(item.name)}`} onClick={() => setSelectedTable(item.name)} aria-label={`Explore ${item.name}, ${rowCount.toLocaleString()} rows and ${columnCount} columns`}>
                    <span className="dataset-glyph"><Database size={15} /></span>
                    <span className="truncate"><strong>{item.name}</strong><small>{columnCount.toLocaleString()} columns</small></span>
                    <span className="dataset-count">{rowCount.toLocaleString()} rows</span>
                    <ChevronRight size={14} />
                  </Link>;
                })}</div> : <><Empty title="No datasets available" detail="Import a CSV or Excel file from Schema Explorer to begin exploring your data." /><Link className="button subtle onboarding-link" href="/schema"><FileUp size={14} /> Add a dataset <ArrowRight size={13} /></Link></>}
              </Panel>

              <Panel title="Recent query activity" subtitle="Latest persisted requests" className="recent-panel" actions={<Link className="text-link" href="/history">View all <ArrowRight size={13} /></Link>}>
                {dashboard?.recent_queries.length ? <div className="recent-list">{dashboard.recent_queries.slice(0, 5).map((item) => <button key={item.id} className="recent-row" onClick={() => setDetail(item)}><span className="truncate"><strong>{item.question || item.sql_query}</strong><small title={shortDate(item.timestamp)}>{relativeTime(item.timestamp)}</small></span><Badge>{item.status}</Badge><span className="duration">{prettyTime(item.execution_time_ms)}</span></button>)}</div> : <Empty title="No recent queries" detail="Validations and executions from the API will appear here." icon={FileClock} />}
              </Panel>

              <Panel title="Recent blocks & failures" subtitle="Rejected or unsuccessful API events" className="blocks-panel">
                {dashboard?.recent_queries.filter((item) => ["blocked", "failed"].includes(item.status.toLowerCase())).length ? <div className="recent-list">{dashboard.recent_queries.filter((item) => ["blocked", "failed"].includes(item.status.toLowerCase())).slice(0, 5).map((item) => <button key={item.id} className="recent-row" onClick={() => setDetail(item)}><span className="truncate"><strong>{item.question || item.sql_query}</strong><small>{item.error_message || relativeTime(item.timestamp)}</small></span><Badge>{item.status}</Badge></button>)}</div> : <Empty title="No blocked or failed records" detail="Real rejected and failed events are shown here when present." icon={ShieldCheck} />}
              </Panel>
            </div>
          </div>
        )}

        {page === "workspace" && (
          <div className="page-content workspace-page">
            <div className="workspace-layout">
              <div className="workspace-main">
                <WorkspaceStages
                  stage={workspaceStage}
                  error={workspaceStageError || null}
                />

                <Panel title="Ask in plain language" subtitle="Generation uses the configured provider on the server." actions={<Badge tone={health?.provider_configured ? "good" : "warn"}>{health?.provider_configured ? "Provider ready" : "Provider unavailable"}</Badge>}>
                  <form onSubmit={generateSql}>
                    <label className="field-label" htmlFor="question">What would you like to know?</label>
                    <textarea id="question" className="question-input" value={question} onChange={(event) => {
                      const nextQuestion = event.target.value;
                      setQuestion(nextQuestion);
                      if (nextQuestion.trim() !== generatedQuestion.trim()) {
                        setReliability(null);
                        setReliabilityId(null);
                        }
                    }} onKeyDown={validateDisabledSql} placeholder="For example: Show 10 records from the selected table." rows={3} />
                    <div className="example-chips">{examples.map((example) => <button type="button" className="chip" key={example} onClick={() => { setQuestion(example); if (example.trim() !== generatedQuestion.trim()) { setReliability(null); setReliabilityId(null); } }}>{example}</button>)}</div>
                    <div className="form-footer"><span className="muted">{queryTable ? `Using live table ${queryTable}.` : "Choose a live table before generating SQL."}</span><div className="button-row"><button type="button" className="button subtle" onClick={() => { setQuestion(""); setGeneratedQuestion(""); setSqlText(""); setGeneratedSql(""); setValidation(null); setValidatedSql(""); setResult(null); setReliability(null); setReliabilityId(null); }}>Clear</button><button type="submit" className="button primary" disabled={!question.trim() || !queryTable || Boolean(busy)}>{busy === "generate" ? <LoaderCircle className="spin" size={15} /> : <WandSparkles size={15} />} Generate SQL <span className="shortcut">⌘ ↵</span></button></div></div>
                  </form>
                </Panel>

                <Panel className={maximizedPanel === "editor" ? "maximized-panel" : ""} title="SQL editor" subtitle={sqlText && sqlText === generatedSql ? "Generated SQL · edit before running" : sqlText ? "Edited SQL · validate before running" : "Editable SQL · server validated"} actions={<><button className="icon-button" title="Copy SQL" aria-label="Copy SQL" onClick={() => copy(sqlText)} disabled={!sqlText}><Copy size={15} /></button><button className="icon-button" title="Reset to generated SQL" aria-label="Reset to generated SQL" onClick={() => { setSqlText(generatedSql); setValidation(null); setResult(null); }} disabled={!generatedSql}><ArrowDownToLine size={15} /></button><button className="icon-button" title={maximizedPanel === "editor" ? "Restore editor panel" : "Maximize editor panel"} aria-label={maximizedPanel === "editor" ? "Restore editor panel" : "Maximize editor panel"} onClick={() => setMaximizedPanel(maximizedPanel === "editor" ? null : "editor")}>{maximizedPanel === "editor" ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button></>}>
                  <div className="editor-frame">
                    <SqlCodeEditor value={sqlText} onChange={(value) => { setSqlText(value); if (validatedSql !== value) { setValidation(null); setResult(null); } }} />
                  </div>
                  <div className="editor-toolbar"><span className="muted">{sqlText.split("\n").length} line{sqlText.split("\n").length === 1 ? "" : "s"} · Ctrl/Cmd + Enter to generate</span><div className="button-row"><button className="button" disabled={!sqlText.trim() || !queryTable || Boolean(busy)} onClick={() => void validateSql()}>{busy === "validate" ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />} Validate</button><button className="button primary" disabled={!sqlText.trim() || !queryTable || Boolean(busy) || Boolean(validation && validatedSql === sqlText && !validation.valid)} onClick={() => void runSql()}>{busy === "execute" ? <LoaderCircle className="spin" size={14} /> : <Play size={14} />} Run query</button></div></div>
                </Panel>

                {validation && (
                  <Panel title="Server validation" subtitle="Authoritative response from the API">
                    <div className="validation-summary"><Badge tone={validation.valid ? "good" : "bad"}>{validation.valid ? "Passed" : "Rejected"}</Badge><span>Operation: <b>{validation.operation}</b></span><span>Safety: <b>{validation.safety.replaceAll("_", " ")}</b></span><span>Syntax: <b>{validation.syntax_valid ? "Valid" : "Invalid"}</b></span><span>Schema: <b>{validation.schema_valid ? "Valid" : "Invalid"}</b></span>{validation.error_category && <span>Issue: <b>{validation.error_category.replaceAll("_", " ")}</b></span>}</div>
                    {validation.validation_checks && (
                      <div className="guard-validation-checks">
                        {(["syntax", "read_only_safety", "schema", "semantic", "result"] as const).map((key) => {
                          const check = validation.validation_checks?.[key];
                          const label = key === "read_only_safety" ? "Read-only safety" : key[0].toUpperCase() + key.slice(1);
                          const normalized = normalizedCheck(check);
                          const tone = normalized.toLowerCase();
                          const passed = /pass|valid|ok/.test(tone);
                          const failed = /fail|reject|block|invalid/.test(tone);
                          return <span key={key}><i className={passed ? "check" : failed ? "fail" : ""}>{passed ? <Check size={12} /> : failed ? <X size={12} /> : <CircleHelp size={12} />}</i>{label}: <b>{normalized}</b></span>;
                        })}
                      </div>
                    )}
                    {validation.confidence_reasons?.length ? <ul className="reason-list">{validation.confidence_reasons.map((reason, index) => <li key={`${index}-${reason}`}>{reason}</li>)}</ul> : null}
                    {validation.hallucination?.errors?.length ? <div className="error-notes">{validation.hallucination.errors.map((error) => <p key={error}><AlertTriangle size={14} />{error}</p>)}</div> : null}
                    {validation.semantic_validation && (
                      <div className="semantic-validation-details">
                        <strong>Semantic: {validation.semantic_validation.status?.replaceAll("_", " ") ?? "Not evaluated"}</strong>
                        {Array.isArray(validation.semantic_validation.requirements) && validation.semantic_validation.requirements.length > 0 && (
                          <p>{validation.semantic_validation.requirements.map((requirement) => typeof requirement === "string" ? requirement : JSON.stringify(requirement)).join(" · ")}</p>
                        )}
                        {validation.semantic_validation.issues?.map((issue, index) => <p key={`${issue.type ?? "semantic"}-${index}`}>{issue.message ? `${issue.type ?? "Issue"}: ${issue.message}` : String(issue.type ?? "Semantic issue")}</p>)}
                      </div>
                    )}
                    <p className="muted helper-note">The service limits result previews to {runtime?.limits.result_preview_rows ?? 500} rows; client-side checks are not used to authorize SQL.</p>
                  </Panel>
                )}

                {reliability ? (
                  <ReliabilityPanel
                    report={reliability}
                    verification={result?.result_verification ?? result?.reliability?.result_verification ?? validation?.reliability?.result_verification}
                  />
                ) : (
                  <Panel title="Answer reliability" subtitle="Evaluation follows SQL generation and server validation" className="reliability-panel">
                    <div className="reliability-empty" role="status">
                      <strong>{sqlText.trim() ? "Awaiting SQL validation" : "Awaiting SQL generation"}</strong>
                      <p className="muted">Accuracy and hallucination risk are not scored until the system has SQL and evaluation evidence.</p>
                    </div>
                  </Panel>
                )}

                {result && (
                  <Panel className={maximizedPanel === "results" ? "maximized-panel" : ""} title="Query results" subtitle={`${result.row_count ?? 0} row(s) · ${prettyTime(result.duration_ms)}${result.truncated ? " · preview truncated" : ""}`} actions={<><button className="icon-button" aria-label="Download results as CSV" title="Download CSV" disabled={!result.rows?.length} onClick={() => downloadCsv(result.rows ?? [])}><ArrowDownToLine size={15} /></button><button className="button subtle" onClick={() => copy(sqlText)}><Copy size={14} /> Copy SQL</button><button className="icon-button" title={maximizedPanel === "results" ? "Restore results panel" : "Maximize results panel"} aria-label={maximizedPanel === "results" ? "Restore results panel" : "Maximize results panel"} onClick={() => setMaximizedPanel(maximizedPanel === "results" ? null : "results")}>{maximizedPanel === "results" ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</button></>}>
                    {result.rows?.length ? <><div className="results-tools"><label className="searchbox"><Search size={14} /><input aria-label="Search result rows" placeholder="Filter rows…" value="" onChange={() => {}} /></label><span className="muted">Results returned by the API</span></div><TableView rows={result.rows} query="" compact={compact} /></> :
                      <div className="success-result"><Check size={20} /><div><strong>Statement completed</strong><span>0 rows returned. This result came from the backend.</span></div></div>}
                  </Panel>
                )}
                {!datasets.length && <div className="connection-warning"><Database size={16} />No dataset tables are available. <Link href="/schema">Import a CSV or Excel dataset</Link> before generating or validating SQL.</div>}
              </div>

              <aside className="workspace-sidebar">
                <Panel title="Live schema" subtitle="From the connected SQLite database">
                  {datasets.length ? <label className="field-label" htmlFor="table-select">Query context</label> : null}
                  {datasets.length ? <select id="table-select" className="select-input" value={selectedTable} onChange={(event) => { setSelectedTable(event.target.value); setReliability(null); setReliabilityId(null); setValidation(null); setValidatedSql(""); setResult(null); }}>{datasets.map((dataset) => <option key={dataset.name} value={dataset.name}>{dataset.name}</option>)}</select> : <Empty title="No tables found" detail="Connect or upload a dataset to inspect its schema." />}
                  {selectedTable && <Link className="text-link schema-explorer-link" href={`/schema?table=${encodeURIComponent(selectedTable)}`}><Table2 size={14} /> Explore selected table data <ArrowRight size={13} /></Link>}
                  {selectedTable && <div className="schema-sidebar">
                    <div className="schema-sidebar-title">
                      <strong title={selectedTable}>{selectedTable}</strong>
                      <span className={`schema-status-badge ${schemaError ? "is-error" : activeSchema ? "is-ready" : ""}`} role="status">
                        {schemaLoading ? <LoaderCircle size={12} className="spin" /> : <i />}
                        {schemaLoading ? "Loading" : schemaError ? "Error" : activeSchema ? "Ready" : "Waiting"}
                      </span>
                    </div>
                    {schemaError ? <p className="schema-inline-state error-notes" role="alert">{schemaError}</p> :
                      schemaLoading ? <p className="schema-inline-state muted">Refreshing live column metadata…</p> :
                        activeSchema ? <>
                          <div className="schema-summary" aria-label={`${activeSchema.columns.length} columns and ${schemaTypeGroups.length} SQL types`}>
                            <span><b>{activeSchema.columns.length.toLocaleString()}</b><small>COLUMNS</small></span>
                            <span><b>{schemaTypeGroups.length.toLocaleString()}</b><small>SQL TYPES</small></span>
                          </div>
                          {activeSchema.columns.length > 0 && <>
                            <div className="schema-type-bar" role="img" aria-label={schemaTypeGroups.map(({ type, count }) => `${type}: ${count}`).join(", ")}>
                              {schemaTypeGroups.map(({ type, count }, index) => <span key={type} style={{ width: `${(count / activeSchema.columns.length) * 100}%`, backgroundColor: `hsl(${188 + (index * 31) % 150} 48% 47%)` }} />)}
                            </div>
                            <div className="schema-type-legend" aria-label="Column type distribution">
                              {schemaTypeGroups.map(({ type, count }, index) => <span key={type} title={`${type}: ${count} columns`}><i style={{ backgroundColor: `hsl(${188 + (index * 31) % 150} 48% 47%)` }} />{type}<b>{count}</b></span>)}
                            </div>
                          </>}
                          <div className="searchbox schema-sidebar-search">
                            <Search size={13} />
                            <input aria-label="Search schema columns by name or type" placeholder="Find a column or type…" value={schemaSearch} onChange={(event) => setSchemaSearch(event.target.value)} />
                            {schemaSearch && <button className="icon-button mini" type="button" aria-label="Clear column search" onClick={() => setSchemaSearch("")}><X size={12} /></button>}
                          </div>
                          {activeSchema.columns.length === 0 ? <p className="schema-inline-state muted">This table has no columns in its live schema.</p> :
                            sidebarSchemaColumns.length === 0 ? <p className="schema-inline-state muted">No columns match “{schemaSearch}”.</p> :
                              <div className="schema-preview-list" aria-label="Column preview">
                                {sidebarSchemaColumns.slice(0, 4).map((column) => <button className="schema-preview-row" key={column.name} onClick={() => copy(`"${column.name}"`)} title={`${column.name} · ${normalizedSqlType(column.type)} — copy column name`}>
                                  <span className="schema-preview-name">{column.is_pk && <KeyRound size={11} className="key-icon" />}{column.name}</span>
                                  <span className="schema-type-badge">{normalizedSqlType(column.type)}</span>
                                </button>)}
                              </div>}
                          <div className="schema-browse-row">
                            <span>{sidebarSchemaColumns.length > 4 ? `Showing 4 of ${sidebarSchemaColumns.length.toLocaleString()}` : `${sidebarSchemaColumns.length.toLocaleString()} matching`}</span>
                            <button ref={schemaBrowseRef} className="button small schema-browse-button" type="button" onClick={() => setSchemaDrawerOpen(true)}><Table2 size={13} /> Browse all columns <ArrowRight size={12} /></button>
                          </div>
                        </> : null}
                  </div>}
                  {activeSchema?.foreign_keys?.length ? <div className="relationship-note"><span className="eyebrow">FOREIGN KEYS</span>{activeSchema.foreign_keys.map((item) => <p key={`${item.from}-${item.table}`}>{item.from} <ArrowRight size={12} /> {item.table}.{item.to}</p>)}</div> : null}
                </Panel>
                <Panel title="Query explanation" subtitle="Available when supported by server output">
                  {validation?.valid ? <div className="explanation"><p>Query parsed as <b>{validation.operation}</b> against selected context <b>{selectedTable}</b>.</p><p>The API permits execution only through a read-only SQLite connection.</p><p className="muted">Semantic correctness is not guaranteed by deterministic validation. Review the SQL and results.</p></div> : <Empty title="No analysis yet" detail="Generate SQL and validate it to review available server explanations." icon={CircleHelp} />}
                </Panel>
              </aside>
            </div>
          </div>
        )}

        {page === "schema" && (
          <div className="page-content schema-page">
            <div className="page-intro"><div><p className="eyebrow">DATABASE / METADATA</p><h2>Know the shape of your data.</h2><p>Schema details are read live from SQLite. The highlighted upload is the single active dataset used across the workspace.</p></div><label className="searchbox schema-search"><Search size={15} /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search tables or columns" aria-label="Search tables and columns" /></label></div>
            {activeDataset ? <div className="schema-active-banner" role="status">
              <span className="status-orb live" />
              <span><strong>Active dataset</strong> · {activeDataset.filename} → <code>{activeDataset.raw_table}</code></span>
              <span>{activeDataset.row_count.toLocaleString()} rows · {activeDataset.column_count} columns · {activeDataset.status.replaceAll("_", " ")}</span>
              <span className="muted">Uploaded {shortDate(activeDataset.created_at)}</span>
            </div> : <div className="schema-active-banner no-active" role="status"><Database size={15} /> No active uploaded dataset. Existing SQLite tables remain available below.</div>}
            <div className="schema-overview" aria-label="Live schema overview">
              <Metric label="LIVE TABLES" value={datasets.length} note="Available in the database" icon={Database} />
              <Metric label="ACTIVE ROWS" value={activeDataset?.row_count.toLocaleString() ?? "—"} note={activeDataset?.raw_table ?? "No active upload"} icon={Table2} />
              <Metric label="SELECTED COLUMNS" value={schema?.columns.length ?? "—"} note="Columns in the selected table" icon={LayoutDashboard} />
              <Metric label="LAST UPLOAD" value={activeDataset ? relativeTime(activeDataset.created_at) : "—"} note={workflow?.status.replaceAll("_", " ") ?? "Awaiting a dataset"} icon={Clock3} />
            </div>
            <Panel title="Replace active dataset" subtitle="A successful upload atomically replaces prior app-managed upload tables. Failed uploads leave the current dataset and its data intact." className="upload-panel">
              <form onSubmit={inspectUpload} className="upload-form">
                <label className="field-label" htmlFor="dataset-file">Dataset file (.csv, .xlsx, .xls)</label>
                <div className="upload-controls">
                  <input id="dataset-file" type="file" accept=".csv,.xlsx,.xls" disabled={uploadBusy} onChange={(event) => { setUploadFile(event.target.files?.[0] ?? null); setUploadInfo(null); setUploadJob(null); }} />
                  <button className="button primary" type="submit" disabled={!uploadFile || uploadBusy}>{uploadBusy ? <LoaderCircle size={14} className="spin" /> : <FileUp size={14} />} Upload file</button>
                </div>
              </form>
              {uploadJob && <div className={`upload-job-status ${uploadJob.status === "failed" ? "failed" : ""}`} role={uploadJob.status === "failed" ? "alert" : "status"}>
                <div className="upload-job-heading">
                  <strong>{uploadJob.stage === "parsing" ? "Parsing file" : uploadJob.stage === "analyzing" ? "Analyzing data quality" : uploadJob.stage === "storing" ? "Storing rows" : uploadJob.stage === "publishing" ? "Publishing dataset" : uploadJob.stage === "finalizing" ? "Preparing dataset preview" : uploadJob.stage === "completed" ? "Upload complete" : uploadJob.stage === "failed" ? "Upload failed" : "Upload queued"}</strong>
                  <span>{uploadJob.elapsed_seconds.toFixed(1)}s elapsed</span>
                </div>
                <p>{uploadJob.stage === "parsing"
                  ? "Reading the uploaded file; row totals will be available after parsing."
                  : uploadJob.stage === "analyzing" && uploadJob.total_rows !== null
                    ? `Analyzing ${uploadJob.total_rows.toLocaleString()} parsed rows.`
                    : uploadJob.stage === "storing" && uploadJob.total_rows !== null
                      ? `${uploadJob.processed_rows.toLocaleString()} of ${uploadJob.total_rows.toLocaleString()} rows written to SQLite.`
                      : uploadJob.stage === "publishing"
                        ? "Committing the validated dataset and updating the active dataset."
                        : uploadJob.stage === "finalizing"
                          ? "Building the upload summary and sample preview."
                        : uploadJob.status === "completed" && uploadJob.total_rows !== null
                          ? `${uploadJob.total_rows.toLocaleString()} rows processed successfully.`
                          : uploadJob.error || "Waiting for an ingestion worker."}</p>
                {uploadJob.error && <p className="upload-job-error">{uploadJob.error}</p>}
              </div>}
              {uploadInfo && <div className="upload-review">
                <div className="upload-summary">
                  <Badge tone={workflow?.status === "FAILED" ? "bad" : "good"}>{workflowLoading ? "Loading…" : workflow?.status.replace(/_/g, " ") ?? "Inspecting…"}</Badge>
                  <span>{uploadInfo.filename}</span>
                  <span>{uploadInfo.inspection.row_count.toLocaleString()} rows</span>
                  <span>{uploadInfo.inspection.col_count} columns</span>
                </div>
                {workflow && <p className="muted helper-note">Dataset ID <code>{workflow.dataset_id}</code> · raw table <strong>{workflow.raw_table}</strong> (preserved as uploaded).</p>}
                {uploadInfo.preview.truncated && <p className="muted helper-note">Preview shows the first {uploadInfo.preview.rows.length} of {uploadInfo.preview.total_rows.toLocaleString()} rows.</p>}
                <div className="table-wrap compact">
                  <table><thead><tr><th>Column</th><th>Type</th><th>Missing</th><th>Sample values</th></tr></thead><tbody>{uploadInfo.inspection.columns.map((column) => <tr key={column.name}><td>{column.name}</td><td>{column.dtype}</td><td>{column.null_count} ({column.null_pct}%)</td><td>{column.sample_values.map(String).join(", ") || "—"}</td></tr>)}</tbody></table>
                </div>
              </div>}
            </Panel>

            {workflow?.error_message && <div className="connection-warning danger-note" role="alert"><AlertTriangle size={16} />{workflow.error_message}</div>}

            {(workflow?.processed_report ?? workflow?.raw_report) && (
              <Panel
                title={workflow?.processed_report ? "Processed data-quality report" : "Initial data-quality report"}
                subtitle={`${workflow?.processed_report ? "Calculated from the stored processed dataset" : "Calculated from the stored raw dataset"} · ${(workflow?.processed_report ?? workflow?.raw_report)?.created_at}`}
                className="quality-panel"
              >
                <WorkflowSteps status={workflow.status} />
                {workflow.status === "RAW_ANALYZED" && <p className="status-note good">Raw Data Stored — Quality Analysis Complete</p>}
                {workflow.processed_report && <p className="status-note good">Processed metrics are live and reflect the latest preprocessing run.</p>}
                <QualityReportView report={(workflow.processed_report ?? workflow.raw_report)!} />
              </Panel>
            )}

            {workflow && workflow.allowed_actions.raw_available && (
              <Panel title="Preprocessing configuration" subtitle="Optional. Nothing runs until you click Preprocess Data; the uploaded raw dataset is preserved until the cleaned replacement is ready." className="preprocess-panel">
                {rules.length ? <div className="rule-options">
                  {rules.map((rule) => rule.type === "toggle" ? (
                    <label className="rule-option" key={rule.id}>
                      <input
                        type="checkbox"
                        checked={preprocessOptions[rule.option_key] === true}
                        onChange={(event) => setPreprocessOptions((options) => ({ ...options, [rule.option_key]: event.target.checked }))}
                      />
                      <span><strong>{rule.title}</strong><small>{rule.description}</small></span>
                    </label>
                  ) : (
                    <label className="rule-option select" key={rule.id}>
                      <span><strong>{rule.title}</strong><small>{rule.description}</small></span>
                      <select
                        className="select-input"
                        value={String(preprocessOptions[rule.option_key] ?? rule.default)}
                        onChange={(event) => setPreprocessOptions((options) => ({ ...options, [rule.option_key]: event.target.value }))}
                      >
                        {(rule.choices ?? []).map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}
                      </select>
                    </label>
                  ))}
                </div> : <p className="muted helper-note">Optional operations could not be loaded; backend defaults will apply.</p>}
                <div className="button-row align-right">
                  <button className="button primary" type="button" onClick={() => void runPreprocess()} disabled={!workflow.allowed_actions.preprocess || preprocessBusy}>
                    {preprocessBusy ? <LoaderCircle size={14} className="spin" /> : <WandSparkles size={14} />} Preprocess Data
                  </button>
                </div>
                {!workflow.allowed_actions.preprocess && <p className="muted helper-note">Preprocessing unlocks after the raw dataset is stored and analyzed. Current status: {workflow.status.replace(/_/g, " ")}.</p>}
                {preprocessBusy && <p className="status-note" role="status"><LoaderCircle size={13} className="spin" /> Preprocessing is running; the previous saved dataset remains available until the replacement is committed.</p>}
              </Panel>
            )}

            {workflow?.comparison && workflow.processed_report && (
              <Panel title="Before and after preprocessing" subtitle={`Processed report recalculated from the processed dataset · ${workflow.processed_report.created_at}`} className="comparison-panel">
                <ComparisonView comparison={workflow.comparison} />
                {workflow.latest_run?.transformations?.length ? (
                  <div className="transform-list">
                    <span className="eyebrow">APPLIED OPERATIONS</span>
                    {workflow.latest_run.transformations.map((transformation, index) => <p key={`${index}-${transformation.description}`}>{transformation.description}{transformation.count !== undefined ? ` — ${transformation.count.toLocaleString()}` : ""}</p>)}
                  </div>
                ) : null}
              </Panel>
            )}

            {workflow && (
              <Panel title="Processed data storage" subtitle="Save the validated cleaned dataset as the current version. Any prior stored data for this dataset is replaced only after this commit succeeds." className="store-panel">
                <div className="button-row align-right">
                  <button className="button primary" type="button" onClick={() => void storeProcessed()} disabled={!workflow.allowed_actions.store_processed || storeBusy}>
                    {storeBusy ? <LoaderCircle size={14} className="spin" /> : <Database size={14} />} Save Cleaned Dataset
                  </button>
                </div>
                {!workflow.allowed_actions.store_processed && <p className="muted helper-note">Storing unlocks after a successful preprocessing run. Current status: {workflow.status.replace(/_/g, " ")}.</p>}
                {workflow.status === "PROCESSED_STORED" && workflow.versions[0] && <div className="status-note good">
                  <span>Cleaned dataset saved successfully as <strong>{workflow.versions[0].table_name}</strong> — {workflow.versions[0].row_count.toLocaleString()} rows, {workflow.versions[0].column_count} columns.</span>
                  <Link className="text-link" href={`/schema?table=${encodeURIComponent(workflow.versions[0].table_name)}`} onClick={() => { setSelectedTable(workflow.versions[0].table_name); setOpenTables((items) => items.includes(workflow.versions[0].table_name) ? items : [...items, workflow.versions[0].table_name]); }}>Explore current dataset</Link>
                </div>}
              </Panel>
            )}

            {loading ? <div className="skeleton-grid">{[1, 2, 3].map((item) => <div className="skeleton-card" key={item} />)}</div> : filteredDatasets.length ? <div className="schema-grid">{filteredDatasets.map((dataset) => {
              const actualSchema = dataset.name === selectedTable ? schema : dataset.schema ? { ...dataset.schema, table: dataset.name, rows: dataset.rows } : null;
              const columns = actualSchema?.columns ?? [];
              const matchingColumns = columns.filter((column) => !search || `${dataset.name} ${column.name} ${column.type ?? ""}`.toLowerCase().includes(search.toLowerCase()));
              const expanded = openTables.includes(dataset.name);
              const panelId = `schema-extra-${encodeURIComponent(dataset.name)}`;
              return <section className="panel schema-card" id={`schema-card-${encodeURIComponent(dataset.name)}`} key={dataset.name}>
                <div className="schema-card-head" role="button" tabIndex={0} aria-expanded={expanded} aria-controls={panelId} onClick={() => { setSelectedTable(dataset.name); setOpenTables((items) => expanded ? items.filter((item) => item !== dataset.name) : [...items, dataset.name]); }} onKeyDown={(event) => { if (event.target !== event.currentTarget && event.target instanceof Element && event.target.closest("button, a, input, select, textarea")) return; if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelectedTable(dataset.name); setOpenTables((items) => expanded ? items.filter((item) => item !== dataset.name) : [...items, dataset.name]); } }}>
                  <span className="schema-card-icon"><Database size={17} /></span>
                    <span className="schema-card-title truncate"><strong>{dataset.name}</strong><small><span className={`schema-status ${dataset.name === activeDataset?.raw_table ? "active" : ""}`}><i /> {dataset.name === activeDataset?.raw_table ? "ACTIVE UPLOAD" : "AVAILABLE"}</span><span className="schema-action-hint">{expanded ? "Hide details" : "Browse schema and rows"}</span></small></span>
                  <span className="schema-card-meta" aria-label={`${dataset.rows.toLocaleString()} rows and ${columns.length} columns`}><span><b>{dataset.rows.toLocaleString()}</b><small>ROWS</small></span><span><b>{columns.length}</b><small>COLUMNS</small></span></span>
                  <span className="schema-card-actions"><button className="icon-button delete-table-button" type="button" aria-label={`Delete ${dataset.name}`} title="Delete dataset" disabled={deletingTable === dataset.name} onClick={(event) => { event.stopPropagation(); deleteTriggerRef.current = event.currentTarget; setDeleteConfirmation(dataset.name); }}><Trash2 size={15} /></button><ChevronDown className={expanded ? "chevron-open" : ""} size={16} /></span>
                </div>
                <div className="schema-column-labels" aria-hidden="true"><span>Column</span><span>Type</span><span>Constraints</span><span /></div>
                <div className="schema-columns">
                  {matchingColumns.slice(0, 5).map((column) => <div className="schema-column" key={column.name}><span className="schema-column-name">{column.is_pk && <KeyRound size={12} className="key-icon" />}{column.name}</span><span className="schema-column-type">{column.type || "unknown"}</span><span className="schema-column-flags">{column.is_pk && <em>PK</em>}{column.not_null === false && <em>NULL</em>}{column.not_null === true && <em>REQ</em>}</span><button className="icon-button mini" aria-label={`Copy ${column.name}`} onClick={() => copy(`"${column.name}"`)}><Copy size={12} /></button></div>)}
                  {!matchingColumns.length && search && <p className="muted schema-no-columns">No matching columns in this table.</p>}
                </div>
                <div className="schema-extra" id={panelId} hidden={!expanded}>
                  {matchingColumns.length > 5 && <div className="schema-columns schema-columns-extra">{matchingColumns.slice(5).map((column) => <div className="schema-column" key={column.name}><span className="schema-column-name">{column.is_pk && <KeyRound size={12} className="key-icon" />}{column.name}</span><span className="schema-column-type">{column.type || "unknown"}</span><span className="schema-column-flags">{column.is_pk && <em>PK</em>}{column.not_null === false && <em>NULL</em>}{column.not_null === true && <em>REQ</em>}</span><button className="icon-button mini" aria-label={`Copy ${column.name}`} onClick={() => copy(`"${column.name}"`)}><Copy size={12} /></button></div>)}</div>}
                  {expanded && actualSchema?.foreign_keys?.length ? <div className="relationship-note"><span className="eyebrow">FOREIGN KEYS</span>{actualSchema.foreign_keys.map((key) => <p key={`${key.from}-${key.table}`}>{key.from} <ArrowRight size={12} /> {key.table}.{key.to}</p>)}</div> : null}
                  {expanded && <DatasetRowsExplorer table={dataset.name} />}
                </div>
              </section>;
            })}</div> : <Empty title="No matching schema" detail={datasets.length ? "Try another table, column, or data type." : "No datasets are registered in the live database."} />}
          </div>
        )}

        {page === "history" && (
          <div className="page-content history-page">
            <Panel title="History log" subtitle={`${filteredHistory.length} matching stored record${filteredHistory.length === 1 ? "" : "s"}`}>
              <div className="history-controls"><label className="searchbox"><Search size={14} /><input aria-label="Search history" value={search} onChange={(event) => { setSearch(event.target.value); setPageNumber(0); }} placeholder="Search question or SQL" /></label><select className="select-input filter-select" aria-label="Filter history by status" value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); setPageNumber(0); }}><option value="all">All statuses</option><option value="success">Success</option><option value="blocked">Blocked</option><option value="failed">Failed</option><option value="previewed">Previewed</option></select></div>
              {visibleHistory.length ? <div className="table-wrap"><table><thead><tr><th>Question / SQL</th><th>Status</th><th>Rows</th><th>Duration</th><th>Time</th><th aria-label="Actions" /></tr></thead><tbody>{visibleHistory.map((item) => {
                return <tr key={item.id}><td className="history-query"><button onClick={() => setDetail(item)}>{item.question || item.sql_query}</button><small>{item.sql_query}</small></td><td><Badge>{item.status}</Badge></td><td>{item.row_count.toLocaleString()}</td><td>{prettyTime(item.execution_time_ms)}</td><td title={shortDate(item.timestamp)}>{relativeTime(item.timestamp)}</td><td><div className="button-row"><button className="button small" onClick={() => reopen(item)}>Open workspace</button></div></td></tr>;
              })}</tbody></table><div className="pagination"><span>{filteredHistory.length ? `${pageNumber * 10 + 1}–${Math.min((pageNumber + 1) * 10, filteredHistory.length)} of ${filteredHistory.length}` : "0 records"}</span><div><button className="button small" disabled={pageNumber === 0} onClick={() => setPageNumber((value) => value - 1)}>Previous</button><button className="button small" disabled={pageNumber + 1 >= historyPages} onClick={() => setPageNumber((value) => value + 1)}>Next</button></div></div></div> : <Empty title="No matching history" detail={historyItems.length ? "Adjust your search or status filter." : "Query events will be stored here after API validations and executions."} icon={FileClock} />}
            </Panel>
            <Panel title="Duration distribution" subtitle="Derived from stored history entries">
              {historyItems.length ? <div className="duration-bars">{[
                ["< 100 ms", historyItems.filter((item) => item.execution_time_ms < 100).length],
                ["100–500 ms", historyItems.filter((item) => item.execution_time_ms >= 100 && item.execution_time_ms < 500).length],
                ["500–1000 ms", historyItems.filter((item) => item.execution_time_ms >= 500 && item.execution_time_ms < 1000).length],
                ["≥ 1 s", historyItems.filter((item) => item.execution_time_ms >= 1000).length],
              ].map(([label, count]) => <div key={String(label)}><span>{label}</span><div className="bar-track"><i style={{ width: `${Math.max(Number(count) ? 4 : 0, Number(count) / historyItems.length * 100)}%` }} /></div><b>{count}</b></div>)}</div> : <Empty title="No duration data" detail="A distribution is shown once real query history exists." icon={Clock3} />}
            </Panel>
          </div>
        )}

        {page === "guardrails" && (
          <div className="page-content guardrails-page">
            <section className="guardrails-intro">
              <div className="guardrails-intro-copy">
                <p className="eyebrow">SERVER-ENFORCED SQL CONTROLS</p>
                <h2>Inspect the statement before it runs.</h2>
                <p>Validate SQL against the selected dataset, then review the safeguards applied during execution.</p>
              </div>
              <div className="guardrails-authority">
                <ShieldCheck size={17} />
                <p><strong>Validation runs on the backend.</strong> This page displays its response; the frontend is not a security boundary. Validation does not execute SQL.</p>
              </div>
            </section>
            <div className="metric-grid guardrail-metrics">
              <Metric label="RECORDED REQUESTS" value={history?.stats.total_queries ?? "—"} note="Persisted query history" icon={Activity} />
              <Metric label="SUCCESSFUL" value={history?.stats.success_count ?? "—"} note="Recorded successful requests" icon={Check} />
              <Metric label="BLOCKED / FAILED" value={history ? history.stats.blocked_count + history.stats.failed_count : "—"} note="Recorded unsuccessful requests" icon={ShieldCheck} />
            </div>
            <div className="guardrails-layout">
              <Panel title="Validate SQL" subtitle="Check a statement against the selected table" className="guardrail-editor">
                <div className="guardrail-table-context">
                  <Table2 size={15} />
                  <span>{selectedTable ? <>Selected dataset <strong>{selectedTable}</strong></> : "No dataset selected"}</span>
                  {!selectedTable && <Link className="text-link" href="/workspace">Choose in SQL Workspace <ArrowRight size={13} /></Link>}
                </div>
                <label className="field-label" htmlFor="guard-sql">SQL statement</label>
                <textarea id="guard-sql" className="question-input mono-input guardrail-sql-input" value={guardSql} onChange={(event) => { setGuardSql(event.target.value); setGuardValidation(null); setGuardValidationError(""); }} rows={8} placeholder={"SELECT *\nFROM your_table\nLIMIT 20;"} />
                <p className="guardrail-form-note">Validation checks the request and returns a server assessment. It does not run this statement.</p>
                <div className="button-row align-right">
                  <button
                    className="button primary"
                    disabled={!guardSql.trim() || !selectedTable || guardBusy}
                    onClick={async () => {
                      setGuardBusy(true);
                      setGuardValidation(null);
                      setGuardValidationError("");
                      try {
                        const response = await api<ValidationResult>("/api/sql/validate", jsonBody({ question: "", sql: guardSql, table_name: selectedTable }));
                        setGuardValidation(response);
                      } catch (error) {
                        const message = messageOf(error);
                        setGuardValidationError(message);
                        updateNotice("error", message);
                      } finally {
                        setGuardBusy(false);
                      }
                    }}
                  >
                    {guardBusy ? <LoaderCircle className="spin" size={14} /> : <ShieldCheck size={14} />}
                    {guardBusy ? "Checking SQL…" : "Validate statement"}
                  </button>
                </div>
                {guardValidationError && <p className="guardrail-validation-error" role="alert">{guardValidationError}</p>}
                {guardBusy && <p className="guardrail-pending" role="status">Waiting for the backend validation response…</p>}
                {guardValidation && (() => {
                  const outcome = guardValidation.valid ? "Valid" : "Rejected";
                  const tone = guardValidation.valid ? "good" : "bad";
                  return (
                    <div className="guard-result" aria-live="polite">
                      <div className="guard-result-heading"><span>Backend validation result</span><Badge tone={tone}>{outcome}</Badge></div>
                      <p>Operation: <b>{guardValidation.operation}</b><span className="guard-result-separator">·</span>Safety: <b>{guardValidation.safety.replaceAll("_", " ")}</b></p>
                      <div className="guard-validation-checks">
                        <span><i className={guardValidation.syntax_valid ? "check" : "fail"}>{guardValidation.syntax_valid ? <Check size={12} /> : <X size={12} />}</i>SQL syntax</span>
                        <span><i className={guardValidation.schema_valid ? "check" : "fail"}>{guardValidation.schema_valid ? <Check size={12} /> : <X size={12} />}</i>Schema check</span>
                      </div>
                      {[...(guardValidation.confidence_reasons ?? []), ...(guardValidation.hallucination?.errors ?? []), ...(guardValidation.hallucination?.warnings ?? [])].map((reason, index) => <p className="muted guardrail-result-reason" key={`${index}-${reason}`}>{reason}</p>)}
                    </div>
                  );
                })()}
              </Panel>
              <div className="guardrails-aside">
                <Panel title="Validation pipeline" subtitle="Checks applied by the validation endpoint">
                  <div className="rule-list">
                    {[
                      ["Statement count", "Rejects requests containing multiple SQL statements."],
                      ["SQL audit", "Audits the statement before it can proceed."],
                      ["Grounding", "Checks table and column references against the selected dataset."],
                      ["Schema conditions", "Checks schema consistency and reports hallucination conditions."],
                    ].map(([name, description]) => <div className="rule-row" key={name}><span className="rule-mark"><Check size={13} /></span><div><strong>{name}</strong><p>{description}</p></div><span className="rule-category">Validate</span></div>)}
                  </div>
                </Panel>
                <Panel title="Execution safeguards" subtitle="Separate from validation; applied when SQL is run">
                  <div className="rule-list">
                    {[
                      ["Read queries", "SELECT and WITH … SELECT queries run through a read-only SQLite connection."],
                      ["Query limits", "Execution times out after 5 seconds; results are capped at 500 rows."],
                      ["Write statements", "INSERT, UPDATE, DELETE, DDL, PRAGMA, and other non-SELECT statements are rejected."],
                      ["Table authorization", "SQLite authorizers permit reads only from the selected dataset table."],
                    ].map(([name, description]) => <div className="rule-row" key={name}><span className="rule-mark"><ShieldCheck size={13} /></span><div><strong>{name}</strong><p>{description}</p></div><span className="rule-category">Execute</span></div>)}
                  </div>
                </Panel>
              </div>
            </div>
            <div className="guardrails-secondary">
              <Panel title="Recent blocked or failed requests" subtitle="From persisted query history" className="guardrail-recent" actions={<Link className="text-link" href="/history">View history <ArrowRight size={13} /></Link>}>
                {loading ? <div className="guardrail-loading" role="status"><LoaderCircle className="spin" size={15} />Loading query history…</div>
                  : !history ? <Empty title="History unavailable" detail="Persisted request records could not be loaded. Retry the live data request to check again." icon={FileClock} />
                    : historyItems.filter((item) => ["blocked", "failed"].includes(item.status.toLowerCase())).length ? <div className="recent-list">{historyItems.filter((item) => ["blocked", "failed"].includes(item.status.toLowerCase())).slice(0, 8).map((item) => <button className="recent-row" key={item.id} onClick={() => setDetail(item)}><span className="truncate"><strong>{item.question || item.sql_query}</strong><small>{item.error_message || shortDate(item.timestamp)}</small></span><Badge>{item.status}</Badge></button>)}</div>
                      : <Empty title="No blocked or failed requests" detail="Recorded rejections and failures will appear here when present." icon={ShieldCheck} />}
              </Panel>
              <Panel title="Not configured / unavailable" subtitle="Capabilities not represented by a production endpoint">
                <div className="rule-list guardrail-unavailable-list">
                  <div className="rule-row"><span className="rule-mark unavailable"><X size={13} /></span><div><strong>Authentication and table roles</strong><p>User authentication and per-user table-role authorization are not configured.</p></div><Badge tone="warn">Not configured</Badge></div>
                  <div className="rule-row"><span className="rule-mark unavailable"><Activity size={13} /></span><div><strong>Risk analytics</strong><p>No production endpoint provides risk-trend or incident aggregates; no chart is inferred from query history.</p></div><Badge tone="neutral">Unavailable</Badge></div>
                </div>
              </Panel>
            </div>
            <section className="reliability-testing">
              <Panel
                title="AI Reliability & Hallucination Testing"
                subtitle="Server-backed test prompts and monitoring aggregates"
                actions={<button className="button primary" disabled={!selectedTable || reliabilityLoading || runningTestId !== null} onClick={() => void runReliabilityTests()}>
                  {runningTestId === "all" ? <LoaderCircle className="spin" size={14} /> : <Play size={14} />}
                  {runningTestId === "all" ? "Running all tests…" : "Run all tests"}
                </button>}
              >
                {reliabilityError && <p className="reliability-error" role="alert">{reliabilityError}</p>}
                {reliabilityLoading ? <div className="guardrail-loading" role="status"><LoaderCircle className="spin" size={15} />Loading reliability data…</div> : (
                  <>
                    {reliabilitySummary ? (
                      <div className="reliability-monitor-grid">
                        <Metric label="QUERIES EVALUATED" value={reliabilitySummary.queries_evaluated} note="Reliability evaluations" icon={Activity} />
                        <Metric label="PASSED / REJECTED" value={`${reliabilitySummary.queries_passed} / ${reliabilitySummary.queries_rejected}`} note="Server classifications" icon={ShieldCheck} />
                        <Metric label="MEAN ACCURACY" value={reliabilitySummary.average_accuracy_score == null ? "—" : reliabilitySummary.average_accuracy_score} note="/ 100" icon={Check} />
                        <Metric label="DETECTION RATE" value={reliabilitySummary.hallucination_detection_rate == null ? "—" : `${reliabilitySummary.hallucination_detection_rate}%`} note="Hallucination test detection" icon={AlertTriangle} />
                      </div>
                    ) : <p className="muted reliability-empty">Monitoring summary is unavailable.</p>}
                    {reliabilitySummary && (
                      <div className="reliability-frequency">
                        <span><b>Test cases run</b>{reliabilitySummary.test_cases_run}</span>
                        <span><b>Frequent failure categories</b>{formatCompactList(reliabilitySummary.most_frequent_failure_categories)}</span>
                        <span><b>Score trend</b>{Array.isArray(reliabilitySummary.score_trends) && reliabilitySummary.score_trends.length ? `${reliabilitySummary.score_trends.length} recorded points` : "No trend data"}</span>
                      </div>
                    )}
                    {!selectedTable && <p className="reliability-error">Choose a live table in SQL Workspace before running tests.</p>}
                    {reliabilityTests.length ? (
                      <div className="reliability-test-groups">
                        {Array.from(new Set(reliabilityTests.map((test) => test.category))).map((category) => (
                          <section className="reliability-test-category" key={category}>
                            <h3>{category}</h3>
                            {reliabilityTests.filter((test) => test.category === category).map((test) => (
                              <article className="reliability-test-row" key={test.id}>
                                <div className="reliability-test-copy">
                                  <span className="eyebrow">TEST {test.id}</span>
                                  <strong>{test.input}</strong>
                                  <p>{test.expected_behavior}</p>
                                  <Badge tone={test.must_reject ? "bad" : "warn"}>{test.must_reject ? "Must reject" : "Expected behavior"}</Badge>
                                </div>
                                <button className="button small" disabled={!selectedTable || runningTestId !== null} onClick={() => void runReliabilityTests(test.id)}>
                                  {runningTestId === test.id ? <LoaderCircle className="spin" size={13} /> : <Play size={13} />}
                                  {runningTestId === test.id ? "Running…" : "Run test"}
                                </button>
                              </article>
                            ))}
                          </section>
                        ))}
                      </div>
                    ) : <Empty title="No test prompts" detail="The backend returned no reliability test cases." icon={ShieldCheck} />}
                  </>
                )}
              </Panel>
              {reliabilityTestResults && (
                <Panel title="Test run results" subtitle={`${reliabilityTestResults.passed} passed · ${reliabilityTestResults.failed} failed · ${reliabilityTestResults.total} total`} className="reliability-results">
                  <div className="reliability-result-list">
                    {reliabilityTestResults.results.map((result, index) => (
                      <article className="reliability-result-row" key={`${result.test_id}-${index}`}>
                        <div className="reliability-result-heading">
                          <div><span className="eyebrow">{result.category} / {result.test_id}</span><strong>{result.test_input}</strong></div>
                          <Badge tone={statusClass(result.status)}>{result.status}</Badge>
                        </div>
                        <dl>
                          <div><dt>Expected</dt><dd>{result.expected_behavior}</dd></div>
                          <div><dt>Actual response</dt><dd>{result.actual_response || "No response returned"}</dd></div>
                          <div><dt>Generated SQL</dt><dd><code>{result.generated_sql || "No SQL generated"}</code></dd></div>
                          <div><dt>Schema / semantic</dt><dd>{normalizedCheck(result.schema_validation_result)} / {normalizedCheck(result.semantic_validation_result)}</dd></div>
                          <div><dt>Execution</dt><dd>{result.execution_status || "Not executed"}</dd></div>
                          {result.result_verification && (
                            <div><dt>Result verification</dt><dd>
                              {result.result_verification.database_source ? `Source: ${result.result_verification.database_source} · ` : ""}
                              {result.result_verification.actual_row_count == null ? "Row count: not provided" : `${result.result_verification.actual_row_count} row(s)`}
                              {result.result_verification.actual_column_count == null ? "" : ` · ${result.result_verification.actual_column_count} column(s)`}
                              {Array.isArray(result.result_verification.actual_columns) && result.result_verification.actual_columns.length ? ` · Columns: ${result.result_verification.actual_columns.join(", ")}` : ""}
                              {result.result_verification.truncated ? " · Preview truncated" : result.result_verification.truncated === false ? " · Preview complete" : ""}
                              {result.result_verification.null_counts && Object.keys(result.result_verification.null_counts).length ? ` · Null counts: ${Object.entries(result.result_verification.null_counts).map(([column, count]) => `${column}=${count}`).join(", ")}` : ""}
                            </dd></div>
                          )}
                          <div><dt>Accuracy / risk</dt><dd>{result.accuracy_score == null ? "—" : result.accuracy_score} / {result.hallucination_risk_score == null ? "—" : result.hallucination_risk_score} ({result.hallucination_risk_level || "Not evaluated"})</dd></div>
                        </dl>
                      </article>
                    ))}
                  </div>
                </Panel>
              )}
            </section>
          </div>
        )}

        {page === "settings" && (
          <div className="page-content settings-page">
            <div className="page-intro"><div><p className="eyebrow">PREFERENCES / CONFIGURATION</p><h2>Make the workspace yours.</h2><p>Local appearance options are saved in this browser. Server settings are read-only diagnostics.</p></div></div>
            <div className="settings-grid">
              <Panel title="Appearance" subtitle="Saved locally in this browser">
                <div className="setting-row"><div><strong>Color theme</strong><p>Choose a comfortable workspace palette.</p></div><div className="segmented"><button className={theme === "dark" ? "selected" : ""} onClick={() => setTheme("dark")}><Moon size={14} /> Dark</button><button className={theme === "light" ? "selected" : ""} onClick={() => setTheme("light")}><Sun size={14} /> Light</button></div></div>
                <label className="setting-row toggle-row"><span><strong>Reduce motion</strong><p>Limit non-essential interface animation.</p></span><input type="checkbox" checked={reduceMotion} onChange={(event) => setReduceMotion(event.target.checked)} /></label>
                <label className="setting-row toggle-row"><span><strong>Compact result tables</strong><p>Use denser spacing in query results.</p></span><input type="checkbox" checked={compact} onChange={(event) => setCompact(event.target.checked)} /></label>
              </Panel>
              <Panel title="AI provider" subtitle="Configured on the backend only">
                <div className="detail-list"><div><span>Provider key</span><Badge tone={runtime?.provider_configured ? "good" : "warn"}>{runtime?.provider_configured ? "Configured" : "Not configured"}</Badge></div><div><span>Model</span><strong>{runtime?.model ?? "Unavailable"}</strong></div></div>
                <p className="security-note"><KeyRound size={15} />Provider credentials are never sent to the browser. Set secrets in the server environment; they cannot be entered here.</p>
              </Panel>
            </div>
          </div>
        )}
      </main>

      {schemaDrawerOpen && <div className="schema-drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setSchemaDrawerOpen(false); }}>
        <section id="schema-drawer" className="schema-drawer" role="dialog" aria-modal="true" aria-labelledby="schema-drawer-title" aria-describedby="schema-drawer-description">
          <header className="schema-drawer-head">
            <div className="schema-drawer-title-block">
              <p className="eyebrow">LIVE SQLITE METADATA</p>
              <h2 id="schema-drawer-title">Browse columns</h2>
              <p id="schema-drawer-description" title={selectedTable}>Table <strong>{selectedTable || "No table selected"}</strong></p>
            </div>
            <button ref={schemaDrawerCloseRef} className="icon-button" type="button" aria-label="Close schema explorer" onClick={() => setSchemaDrawerOpen(false)}><X size={18} /></button>
          </header>
          <div className="schema-drawer-tools">
            <div className="searchbox schema-drawer-search">
              <Search size={14} />
              <input aria-label="Search all columns by name or type" placeholder="Search column name or type…" value={schemaSearch} onChange={(event) => setSchemaSearch(event.target.value)} />
              {schemaSearch && <button className="icon-button mini" type="button" aria-label="Clear column search" onClick={() => setSchemaSearch("")}><X size={12} /></button>}
            </div>
            <label className="schema-control">
              <span>Type</span>
              <select aria-label="Filter columns by SQL type" value={schemaTypeFilter} onChange={(event) => setSchemaTypeFilter(event.target.value)}>
                <option value="all">All types</option>
                {schemaTypeGroups.map(({ type }) => <option key={type} value={type}>{type}</option>)}
              </select>
            </label>
            <label className="schema-control">
              <span>Sort by</span>
              <select aria-label="Sort columns" value={schemaSort} onChange={(event) => setSchemaSort(event.target.value as "name" | "type")}>
                <option value="name">Column name</option>
                <option value="type">SQL type</option>
              </select>
            </label>
          </div>
          <div className="schema-drawer-count" role="status">
            <span><strong>{filteredSchemaColumns.length.toLocaleString()}</strong> matching <span className="muted">of {activeSchema?.columns.length.toLocaleString() ?? "—"} columns</span></span>
            {activeSchema && <span className="schema-drawer-table-name" title={activeSchema.table}>{activeSchema.table}</span>}
          </div>
          {schemaError ? <div className="schema-drawer-state error-notes" role="alert">{schemaError}</div> :
            schemaLoading || !activeSchema ? <div className="schema-drawer-state muted" role="status"><LoaderCircle size={15} className="spin" /> Loading live schema…</div> :
              activeSchema.columns.length === 0 ? <div className="schema-drawer-state muted">This table has no columns in its live schema.</div> :
                filteredSchemaColumns.length === 0 ? <div className="schema-drawer-state muted">No columns match the current search and type filter.</div> : <>
                  <div className="schema-drawer-table-wrap">
                    <table className="schema-drawer-table">
                      <thead><tr><th scope="col">Column</th><th scope="col">Type</th><th scope="col">Metadata</th><th scope="col">Default</th></tr></thead>
                      <tbody>
                        {visibleSchemaColumns.map((column) => <tr key={column.name} className={selectedColumn?.name === column.name ? "is-selected" : ""}>
                          <td><span className="schema-column-name-cell"><button className="schema-row-select" type="button" title={column.name} aria-pressed={selectedColumn?.name === column.name} onClick={() => setSelectedColumn(column)}>{column.is_pk && <KeyRound size={12} className="key-icon" />}{column.name}</button><button className="icon-button mini schema-copy-name" type="button" title={`Copy ${column.name}`} aria-label={`Copy column name ${column.name}`} onClick={() => { void navigator.clipboard.writeText(column.name).then(() => updateNotice("success", `Copied "${column.name}".`)).catch(() => updateNotice("error", "Could not access the clipboard.")); }}><Copy size={11} /></button></span></td>
                          <td><span className="schema-type-badge">{normalizedSqlType(column.type)}</span></td>
                          <td><span className="schema-column-flags">{column.is_pk && <em>PK</em>}{column.is_fk && <em>FK</em>}{column.is_unique && <em>UQ</em>}{column.not_null === true && <em>REQ</em>}{column.not_null === false && <em>NULL</em>}</span></td>
                          <td className="schema-default-cell" title={column.default_value === undefined ? "Not provided" : column.default_value === null ? "NULL" : column.default_value}>{column.default_value === undefined ? "—" : column.default_value === null ? "NULL" : column.default_value}</td>
                        </tr>)}
                      </tbody>
                    </table>
                  </div>
                  <footer className="schema-drawer-footer">
                    <span>{filteredSchemaColumns.length ? `${(schemaPage * schemaPageSize + 1).toLocaleString()}–${Math.min((schemaPage + 1) * schemaPageSize, filteredSchemaColumns.length).toLocaleString()} of ${filteredSchemaColumns.length.toLocaleString()}` : "0 columns"}</span>
                    <div className="schema-pagination">
                      <button className="button small" type="button" disabled={schemaPage === 0} onClick={() => setSchemaPage((current) => Math.max(0, current - 1))}>Previous</button>
                      <span>Page {schemaPage + 1} of {schemaPageCount}</span>
                      <button className="button small" type="button" disabled={schemaPage + 1 >= schemaPageCount} onClick={() => setSchemaPage((current) => Math.min(schemaPageCount - 1, current + 1))}>Next</button>
                    </div>
                  </footer>
                  {selectedColumn && <section className="schema-column-details" aria-label={`Details for ${selectedColumn.name}`}>
                    <div className="schema-column-details-head"><span className="eyebrow">COLUMN DETAILS</span><strong title={selectedColumn.name}>{selectedColumn.name}</strong></div>
                    <dl>
                      <div><dt>Declared type</dt><dd>{selectedColumn.type?.trim() || "Not provided"}</dd></div>
                      <div><dt>Primary key</dt><dd>{selectedColumn.is_pk === undefined ? "Not provided" : selectedColumn.is_pk ? "Yes" : "No"}</dd></div>
                      <div><dt>Declared foreign key</dt><dd>{selectedColumn.is_fk === undefined ? "Not provided" : selectedColumn.is_fk ? "Yes" : "No"}</dd></div>
                      <div><dt>Unique constraint</dt><dd>{selectedColumn.is_unique === undefined ? "Not provided" : selectedColumn.is_unique ? "Yes" : "No"}</dd></div>
                      <div><dt>Nullability</dt><dd>{selectedColumn.not_null === undefined ? "Not provided" : selectedColumn.not_null ? "Required" : "Nullable"}</dd></div>
                      <div><dt>Default value</dt><dd title={selectedColumn.default_value === undefined ? "Not provided" : selectedColumn.default_value === null ? "NULL" : selectedColumn.default_value}>{selectedColumn.default_value === undefined ? "Not provided" : selectedColumn.default_value === null ? "NULL" : selectedColumn.default_value}</dd></div>
                    </dl>
                  </section>}
                </>}
        </section>
      </div>}

      <Toast notice={notice} onClose={() => setNotice(null)} />

      {deleteConfirmation && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeDeleteConfirmation(); }}>
        <section id="delete-dataset-dialog" className="modal" role="alertdialog" aria-modal="true" aria-labelledby="delete-dataset-title" aria-describedby="delete-dataset-description" aria-busy={deletingTable === deleteConfirmation}>
          <header className="modal-head">
            <div><p className="eyebrow">DELETE DATASET</p><h2 id="delete-dataset-title">Delete this dataset?</h2></div>
          </header>
          <div className="modal-body">
            <div className="connection-warning danger-note"><AlertTriangle size={16} />This action cannot be undone.</div>
            <p id="delete-dataset-description">Delete <code className="delete-confirm-table-name">{deleteConfirmation}</code> and its data from the connected SQLite database. If this is an uploaded raw dataset, related staging and processed tables, quality reports, saved-version records, and workflow metadata are also removed. If it is a processed version, its version record is removed. Table previews are read from this data and will no longer be available.</p>
            {deletingTable === deleteConfirmation && <div className="delete-confirm-progress" role="status" aria-live="polite"><LoaderCircle size={14} className="spin" /> Deleting {deleteConfirmation}…</div>}
          </div>
          <footer className="modal-actions delete-confirm-actions">
            <button ref={deleteCancelButtonRef} className="button" type="button" disabled={deletingTable === deleteConfirmation} onClick={closeDeleteConfirmation}>Cancel</button>
            <button className="button danger-button" type="button" aria-disabled={deletingTable === deleteConfirmation} onClick={() => void deleteDataset()}>
              {deletingTable === deleteConfirmation ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}
              {deletingTable === deleteConfirmation ? "Deleting…" : "Delete"}
            </button>
          </footer>
        </section>
      </div>}

      {detail && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setDetail(null); }}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="history-detail-title"><header className="modal-head"><div><p className="eyebrow">STORED QUERY EVENT</p><h2 id="history-detail-title">{detail.question || "Query details"}</h2></div><button className="icon-button" onClick={() => setDetail(null)} aria-label="Close details"><X size={17} /></button></header><div className="modal-body"><div className="detail-list"><div><span>Status</span><Badge>{detail.status}</Badge></div><div><span>Rows</span><strong>{detail.row_count}</strong></div><div><span>Duration</span><strong>{prettyTime(detail.execution_time_ms)}</strong></div><div><span>Timestamp</span><strong>{shortDate(detail.timestamp)}</strong></div></div><pre className="sql-preview">{detail.sql_query}</pre>{detail.error_message && <p className="error-notes">{detail.error_message}</p>}</div><footer className="modal-actions"><button className="button" onClick={() => copy(detail.sql_query)}><Copy size={14} /> Copy SQL</button><button className="button primary" onClick={() => reopen(detail)}>Open in workspace <ArrowRight size={14} /></button></footer></section></div>}
    </div>
  );
}
