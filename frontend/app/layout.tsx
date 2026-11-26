import type { Metadata } from "next";
import InteractiveStarfield from "@/components/InteractiveStarfield";
import "./globals.css";

export const metadata: Metadata = {
  title: "QueryGlass AI",
  description: "A grounded workspace for exploring data and validating SQL.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <InteractiveStarfield />
        {children}
      </body>
    </html>
  );
}
