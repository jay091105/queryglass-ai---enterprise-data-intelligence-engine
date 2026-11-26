"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { api } from "@/lib/api";

type Issue = {
  type?: string;
  message: string;
  missing_object?: string;
  recommendation?: string;
};

type ReliabilityData = {
  answer_accuracy_score?: number | null;
  hallucination_risk_score?: number | null;
  validation_status?: string;
  evaluated_weight?: number;
};

type ResultMetadata = {
  columns?: string[];
  rows?: Record<string, unknown>[];
  truncated?: boolean;
};

type QueryRunData = {
  user_question: string;
  generated_sql: string;
  table_name?: string;
  dataset_id?: string;
  execution_timestamp: string;
  execution_status: string;
  result_grounding: string;
  execution_time_ms: number;
  row_count: number;
  error_message?: string;
  result_verification_json?: {
    metadata?: ResultMetadata;
  };
};

type QueryRunReport = {
  report_id: string;
  query_run: QueryRunData;
  reliability?: ReliabilityData;
  issues: Issue[];
  metadata_available: boolean;
};

export function QueryHistoryDetail({ queryId, onClose }: { queryId: string; onClose: () => void }) {
  const [data, setData] = useState<QueryRunReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    setLoading(true);
    setError("");
    api<QueryRunReport>(`/api/query-runs/${queryId}/report`)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load query details"))
      .finally(() => setLoading(false));
  }, [queryId]);

  if (loading) {
    return (
      <div className="modal-overlay" onClick={onClose}>
        <div className="modal-card history-detail" onClick={(e) => e.stopPropagation()}>
          <p role="status">Loading query details…</p>
        </div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="modal-overlay" onClick={onClose}>
        <div className="modal-card history-detail" onClick={(e) => e.stopPropagation()}>
          <button className="button small" onClick={onClose}>Close</button>
          <p className="bad" role="alert">{error || "Query not found"}</p>
        </div>
      </div>
    );
  }

  const run = data.query_run;
  const reliability = data.reliability || {};
  const resultData = run.result_verification_json?.metadata || {};

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card history-detail" onClick={(e) => e.stopPropagation()}>
        <header className="detail-header">
          <h2>Query Execution Details</h2>
          <button className="button small" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </header>

        <div className="detail-content">
          <section className="detail-section">
            <h3>Question</h3>
            <p className="question-text">{run.user_question}</p>
          </section>

          <section className="detail-section">
            <h3>Context</h3>
            <dl className="detail-dl">
              <div><dt>Table</dt><dd>{run.table_name || "Not specified"}</dd></div>
              <div><dt>Dataset ID</dt><dd>{run.dataset_id || "Not specified"}</dd></div>
              <div><dt>Timestamp</dt><dd>{new Date(run.execution_timestamp).toLocaleString()}</dd></div>
            </dl>
          </section>

          <section className="detail-section">
            <h3>Generated SQL</h3>
            <pre className="code-block">{run.generated_sql}</pre>
          </section>

          <section className="detail-section">
            <h3>Execution Status</h3>
            <div className="status-grid">
              <div>
                <span className="status-label">Execution</span>
                <span className={`badge ${run.execution_status === 'success' ? 'good' : 'bad'}`}>
                  {run.execution_status}
                </span>
              </div>
              <div>
                <span className="status-label">Result Grounding</span>
                <span className={`badge ${run.result_grounding === 'well_grounded' ? 'good' : run.result_grounding === 'partially_grounded' ? 'warn' : 'bad'}`}>
                  {run.result_grounding}
                </span>
              </div>
              <div>
                <span className="status-label">Duration</span>
                <span className="status-value">{run.execution_time_ms?.toFixed(2)} ms</span>
              </div>
              <div>
                <span className="status-label">Rows Returned</span>
                <span className="status-value">{run.row_count || 0}</span>
              </div>
            </div>
          </section>

          {run.error_message && (
            <section className="detail-section error-section">
              <h3>Error Message</h3>
              <p className="bad">{run.error_message}</p>
            </section>
          )}

          {reliability && Object.keys(reliability).length > 0 && (
            <section className="detail-section">
              <h3>Answer Reliability</h3>
              <div className="reliability-grid-detail">
                <div>
                  <span className="reliability-label">Accuracy Score</span>
                  <strong className="reliability-value">{reliability.answer_accuracy_score ?? "Not evaluated"}</strong>
                </div>
                <div>
                  <span className="reliability-label">Hallucination Risk</span>
                  <strong className="reliability-value">{reliability.hallucination_risk_score ?? "Not evaluated"}</strong>
                </div>
                <div>
                  <span className="reliability-label">Validation</span>
                  <span className="reliability-value">{reliability.validation_status || "Not evaluated"}</span>
                </div>
                <div>
                  <span className="reliability-label">Evaluated Weight</span>
                  <span className="reliability-value">{reliability.evaluated_weight ? `${reliability.evaluated_weight}%` : "Not evaluated"}</span>
                </div>
              </div>
            </section>
          )}

          {data.issues && data.issues.length > 0 && (
            <section className="detail-section">
              <h3>Detected Issues</h3>
              <div className="issues-list">
                {data.issues.map((issue, idx) => (
                  <div key={idx} className="issue-item">
                    <span className="badge bad">{issue.type || "Issue"}</span>
                    <p className="issue-message">{issue.message}</p>
                    {issue.missing_object && <small className="issue-detail">Missing: {issue.missing_object}</small>}
                  </div>
                ))}
              </div>
            </section>
          )}

          {resultData.columns && resultData.rows && (
            <section className="detail-section">
              <h3>Query Results</h3>
              <p className="muted result-info">
                {resultData.rows.length} rows returned
                {resultData.truncated && " (truncated)"}
              </p>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      {resultData.columns.map((col) => (
                        <th key={col}>{col}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {resultData.rows.slice(0, 50).map((row, idx) => (
                      <tr key={idx}>
                        {resultData.columns?.map((col) => (
                          <td key={col}>{row[col] !== null && row[col] !== undefined ? String(row[col]) : "NULL"}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {resultData.rows.length > 50 && (
                <p className="muted result-note">Showing first 50 of {resultData.rows.length} rows</p>
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}