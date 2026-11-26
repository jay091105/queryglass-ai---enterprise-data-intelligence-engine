// Call the same-origin Next.js route. next.config.ts proxies /api/* to the
// backend over IPv4, avoiding browser localhost/IPv6 and CORS mismatches.
const API_BASE = "";

export class ApiError extends Error {
  constructor(message: string, readonly status?: number, readonly payload?: unknown) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const method = init?.method ?? "GET";
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }), ...init?.headers },
      cache: "no-store",
    });
  } catch (error) {
    const cause = error instanceof Error ? ` (${error.message})` : "";
    console.error("[QueryGlass API] Network request failed", {
      method,
      path,
      error,
    });
    throw new ApiError(
      `Could not reach the QueryGlass API through the web app${cause}. Start both services with RUN_QUERYGLASS.bat from the project root, or start the backend with RUN_API.bat.`,
    );
  }

  const responseText = await response.text();
  let payload: unknown = null;
  if (responseText) {
    try {
      payload = JSON.parse(responseText) as unknown;
    } catch {
      payload = responseText;
    }
  }

  if (!response.ok) {
    const detail = typeof payload === "object" && payload !== null && "detail" in payload
      ? (payload as { detail: unknown }).detail
      : null;
    let printableDetail: unknown = detail ?? payload;
    if (typeof printableDetail === "string") {
      // Proxy/HTML error pages are noise in the console; keep a short marker.
      printableDetail = printableDetail.slice(0, 500);
    }
    console.error(
      "[QueryGlass API] Request returned an error:",
      method,
      path,
      `status ${response.status}`,
      typeof printableDetail === "object" ? JSON.stringify(printableDetail) : printableDetail,
    );
    const message = describeErrorDetail(detail) ?? `Request failed (${response.status}).`;
    throw new ApiError(message, response.status, payload);
  }
  return payload as T;
}

function describeErrorDetail(detail: unknown): string | null {
  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }
  if (Array.isArray(detail)) {
    // FastAPI request-validation errors: [{loc, msg, type}, ...]
    const parts = detail
      .map((item) => {
        if (typeof item === "object" && item !== null && "msg" in item) {
          const location = Array.isArray((item as { loc: unknown[] }).loc)
            ? (item as { loc: unknown[] }).loc.join(".")
            : "";
          return location ? `${location}: ${String((item as { msg: unknown }).msg)}` : String((item as { msg: unknown }).msg);
        }
        return typeof item === "string" ? item : JSON.stringify(item);
      })
      .filter(Boolean);
    return parts.length ? parts.join("; ") : null;
  }
  if (typeof detail === "object" && detail !== null) {
    if ("message" in detail && typeof (detail as { message: unknown }).message === "string") {
      return (detail as { message: string }).message;
    }
    try {
      const serialized = JSON.stringify(detail);
      return serialized && serialized !== "{}" ? serialized : null;
    } catch {
      return null;
    }
  }
  return null;
}

export function jsonBody(value: unknown): Pick<RequestInit, "method" | "body"> {
  return { method: "POST", body: JSON.stringify(value) };
}

