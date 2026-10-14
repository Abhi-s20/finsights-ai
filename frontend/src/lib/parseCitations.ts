// lib/parseCitations.ts
//
// Ported from the prototype's parseCitations.js. The backend's system
// prompt (see backend/app/main.py SYSTEM_INSTRUCTIONS) makes the LLM cite
// every claim inline as:
//   [TICKER DOC_TYPE FILING_DATE Item ITEM_NUMBER]
// e.g. [AAPL 10-Q 2026-05-01 Item 2]
//
// This splits answer text on that pattern and resolves each citation
// against the `sources` array streamed alongside the answer, so the UI can
// render a numbered footnote marker that jumps to (and highlights) both
// the matching exhibit card AND the right-pane PDF/Markdown viewer at the
// cited page.

export interface SourceChunk {
  chunk_id: string;
  ticker: string | null;
  exchange: string | null;
  doc_type: string | null;
  fiscal_year: number | null;
  fiscal_quarter: number | null;
  filing_date: string | null;
  item_number: string | null;
  item_title: string | null;
  content_type: string | null;
  page_number: number | null;
  text: string;
  score: number | null;
  filing_id: string | null;
}

export type Segment =
  | { type: "text"; content: string }
  | { type: "citation"; raw: string; index: number | null; source: SourceChunk | null };

const CITATION_PATTERN = /\[([A-Z0-9.]{1,20}) (10-K|10-Q|8-K|ANNUAL_REPORT|QUARTERLY_RESULT) (\d{4}-\d{2}-\d{2}) Item ([^\]]+)\]/g;

export function parseCitations(text: string, sources: SourceChunk[]): Segment[] {
  const segments: Segment[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  CITATION_PATTERN.lastIndex = 0;
  while ((match = CITATION_PATTERN.exec(text)) !== null) {
    const [raw, ticker, docType, filingDate, itemNumber] = match;

    if (match.index > lastIndex) {
      segments.push({ type: "text", content: text.slice(lastIndex, match.index) });
    }

    const sourceIndex = sources.findIndex(
      (s) =>
        s.ticker === ticker &&
        s.doc_type === docType &&
        s.filing_date === filingDate &&
        String(s.item_number) === itemNumber.trim()
    );

    segments.push({
      type: "citation",
      raw,
      index: sourceIndex === -1 ? null : sourceIndex + 1,
      source: sourceIndex === -1 ? null : sources[sourceIndex],
    });

    lastIndex = CITATION_PATTERN.lastIndex;
  }

  if (lastIndex < text.length) {
    segments.push({ type: "text", content: text.slice(lastIndex) });
  }

  return segments;
}
