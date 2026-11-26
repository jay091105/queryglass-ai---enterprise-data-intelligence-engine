"use client";

import {
  CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

export type ActivityPoint = {
  day: string;
  success: number;
  blocked: number;
  failed: number;
};

export function ActivityChart({ data }: { data: ActivityPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={245}>
      <LineChart data={data} margin={{ top: 10, right: 12, bottom: 0, left: -20 }}>
        <CartesianGrid stroke="var(--border)" strokeDasharray="3 5" vertical={false} />
        <XAxis dataKey="day" tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false} axisLine={false} />
        <YAxis allowDecimals={false} tick={{ fill: "var(--muted)", fontSize: 11 }} tickLine={false} axisLine={false} />
        <Tooltip contentStyle={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, color: "var(--text)" }} />
        <Line type="monotone" dataKey="success" name="Successful" stroke="var(--success)" strokeWidth={2.5} dot={false} />
        <Line type="monotone" dataKey="blocked" name="Blocked" stroke="var(--accent)" strokeWidth={2} dot={false} />
        <Line type="monotone" dataKey="failed" name="Failed" stroke="var(--error)" strokeWidth={1.8} strokeDasharray="5 4" dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
