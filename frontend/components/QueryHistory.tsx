"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { QueryHistoryDetail } from "@/components/QueryHistoryDetail";
import type { QueryRun } from "@/types/reliability";

type QueryRunStats = {
  total_records: number;
  successful_queries: number;
  blocked_queries: number;
  avg_duration_ms: number;
};

export function QueryHistory() {
  const [items, setItems] = useState<QueryRun[]>([]);
  const [stats, setStats] = useState<QueryRunStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [grounding, setGrounding] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    
    // Fetch stats
    api<QueryRunStats>("/api/query-runs/stats")
      .then((data) => { if (active) setStats(data); })
      .catch((err) => console.error("Failed to load stats:", err));

    // Fetch query runs
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (grounding) params.set("grounding", grounding);
    setLoading(true);
    setError("");
    
    api<{ items: QueryRun[] }>(`/api/query-runs?${params.toString()}`)
      .then((payload) => {
        if (active) setItems(payload.items);
      })
      .catch((failure: unknown) => {
        if (active) setError(failure instanceof Error ? failure.message : "Unable to load query history");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
      
    return () => {
      active = false;
    };
  }, [status, grounding]);

  return (
    <>
      <section className="panel query-history-panel">
        <header className="panel-head">
          <div>
            <h2>Query History</h2>
            <p>Complete execution trace history with full audit trail</p>
          </div>
        </header>

        {stats && (
          <div className="stats-grid">
            <div className="stat-card">
              <span className="stat-label">Total Records</span>
              <strong className="stat-value">{stats.total_records}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">Successful Queries</span>
              <strong className="stat-value good">{stats.successful_queries}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">Blocked Queries</span>
              <strong className="stat-value bad">{stats.blocked_queries}</strong>
            </div>
            <div className="stat-card">
              <span className="stat-label">Avg Duration</span>
              <strong className="stat-value">
                {stats.avg_duration_ms > 0 ? `${stats.avg_duration_ms.toFixed(0)} ms` : "—"}
              </strong>
            </div>
          </div>
        )}

        <div className="history-controls">
          <select className="select-input" value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">All statuses</option>
            <option value="success">Success</option>
            <option value="syntax_error">Syntax error</option>
            <option value="runtime_error">Runtime error</option>
            <option value="timeout">Timeout</option>
          </select>
          <select className="select-input" value={grounding} onChange={(event) => setGrounding(event.target.value)}>
            <option value="">All grounding</option>
            <option value="well_grounded">Well grounded</option>
            <option value="partially_grounded">Partially grounded</option>
            <option value="ungrounded">Ungrounded</option>
          </select>
        </div>

        {loading ? (
          <p role="status">Loading query history…</p>
        ) : error ? (
          <p className="bad" role="alert">{error}</p>
        ) : items.length === 0 ? (
          <p className="muted" role="status">No query execution records found. Execute some queries to see them here.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Question</th>
                  <th>Status</th>
                  <th>Grounding</th>
                  <th>Reliability</th>
                  <th>Time</th>
                  <th>Action</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.query_id}>
                    <td className="question-cell">{item.user_question}</td>
                    <td>
                      <span className={`badge ${item.execution_status === 'success' ? 'good' : 'bad'}`}>
                        {item.execution_status}
                      </span>
                    </td>
                    <td>
                      <span className={`badge ${item.result_grounding === 'well_grounded' ? 'good' : item.result_grounding === 'partially_grounded' ? 'warn' : 'bad'}`}>
                        {item.result_grounding}
                      </span>
                    </td>
                    <td>{item.reliability_score.toFixed(2)}</td>
                    <td className="time-cell">{new Date(item.execution_timestamp).toLocaleString()}</td>
                    <td>
                      <button 
                        className="button small"
                        onClick={() => setSelectedId(item.query_id)}
                      >
                        View Details
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {selectedId && (
        <QueryHistoryDetail queryId={selectedId} onClose={() => setSelectedId(null)} />
      )}
    </>
  );
}