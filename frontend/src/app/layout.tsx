import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "FinSights AI",
  description: "AI-powered research over NSE, BSE, and SEC company filings.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
