import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  distDir: process.env.NODE_ENV === "development" ? ".next-dev" : ".next",
  reactStrictMode: true,
  experimental: {
    middlewareClientMaxBodySize: "5gb",
    // Large uploads and preprocessing runs legitimately take minutes on the
    // backend; the rewrite proxy aborts them client-side after its 30s default.
    proxyTimeout: 30 * 60 * 1000,
  },
  async rewrites() {
    const apiOrigin = (process.env.QUERYGLASS_API_URL ?? "http://127.0.0.1:8002").replace(/\/$/, "");
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;
