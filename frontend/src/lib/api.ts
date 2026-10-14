// lib/api.ts
//
// Talks to the FastAPI backend. /query is now a Server-Sent Events stream
// (see backend/app/main.py): a `sources` event fires immediately so the
// exhibit panel + right-pane viewer can populate before generation even
// starts, then `delta` events stream the answer token-by-token, then `done`.

import type { SourceChunk } from "./parseCitations";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export interface QueryStreamHandlers {
  onSources: (payload: { sources: SourceChunk[]; tickers_detected: string[]; session_id: string }) => void;
  onDelta: (text: string) => void;
  onDone: (fullAnswer: string) => void;
  onError: (message: string) => void;
}

export interface QueryOptions {
  tickers?: string[];
  docTypes?: string[];
  fiscalYear?: number;
  sessionId?: string;
}

export async function streamQuery(question: string, handlers: QueryStreamHandlers, opts: QueryOptions = {}) {
  const response = await fetch(`${API_URL}/api/v1/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question,
      tickers: opts.tickers,
      doc_types: opts.docTypes,
      fiscal_year: opts.fiscalYear,
      session_id: opts.sessionId,
    }),
  });

  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null);
    handlers.onError(body?.detail || response.statusText);
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line; parse complete frames only.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      const eventLine = frame.split("\n").find((l) => l.startsWith("event:"));
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!eventLine || !dataLine) continue;

      const event = eventLine.replace("event:", "").trim();
      const data = JSON.parse(dataLine.replace("data:", "").trim());

      if (event === "sources") handlers.onSources(data);
      else if (event === "delta") handlers.onDelta(data.text);
      else if (event === "done") handlers.onDone(data.answer);
      else if (event === "error") handlers.onError(data.detail);
    }
  }
}

export async function fetchRedFlags(ticker: string) {
  const res = await fetch(`${API_URL}/api/v1/red-flags/${encodeURIComponent(ticker)}`);
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || res.statusText);
  return res.json();
}

export async function exportExcel(tickers: string[], fiscalYears?: number[]) {
  const res = await fetch(`${API_URL}/api/v1/export/excel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tickers, fiscal_years: fiscalYears }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || res.statusText);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `finsights_export_${tickers.join("_")}.xlsx`;
  a.click();
  URL.revokeObjectURL(url);
}

export interface ChatSession {
  id: string;
  title: string;
  updated_at: string;
}

export interface StoredMessage {
  role: "USER" | "ASSISTANT" | "SYSTEM";
  content: string;
  tickers_detected: string[];
  sources: import("./parseCitations").SourceChunk[];
}

export async function fetchSessions(): Promise<ChatSession[]> {
  const res = await fetch(`${API_URL}/api/v1/sessions`);
  if (!res.ok) throw new Error("Failed to load chat history");
  return res.json();
}

export async function fetchSessionMessages(sessionId: string): Promise<StoredMessage[]> {
  const res = await fetch(`${API_URL}/api/v1/sessions/${sessionId}/messages`);
  if (!res.ok) throw new Error("Failed to load conversation");
  return res.json();
}
