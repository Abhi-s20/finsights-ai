// components/SourceViewer.tsx
//
// Right pane: PDF.js-rendered viewer that jumps to the exact page a
// clicked citation/exhibit points to and highlights the cited passage's
// bounding box (filing_chunks.bbox from Postgres, set during ingestion --
// see backend/ingestion/chunker.py / tasks.py). Falls back to a plain
// text/markdown view for filings that were parsed from HTML rather than PDF.

"use client";

import { useEffect, useRef, useState } from "react";
import type { SourceChunk } from "@/lib/parseCitations";

// pdfjs-dist is loaded from the CDN UMD build rather than bundled, per this
// environment's artifact constraints; in a real Next.js app install
// `pdfjs-dist` and import it directly instead.
declare global {
  interface Window {
    pdfjsLib: any;
  }
}

interface Props {
  activeSource: SourceChunk | null;
}

interface BBox { x0: number; y0: number; x1: number; y1: number }

export default function SourceViewer({ activeSource }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const highlightRef = useRef<HTMLDivElement>(null);
  const [loading, setLoading] = useState(false);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [bbox, setBbox] = useState<BBox | null>(null);

  const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  useEffect(() => {
    if (!activeSource?.filing_id) return;
    // Backend serves the original PDF (or a signed object-storage URL) plus
    // this chunk's page/bbox metadata for highlighting.
    setLoading(true);
    fetch(`${API_URL}/api/v1/filings/${activeSource.filing_id}/source-url?chunk_id=${activeSource.chunk_id}`)
      .then((r) => r.json())
      .then((data) => {
        setPdfUrl(data.url);
        setBbox(data.bbox ?? null);
      })
      .finally(() => setLoading(false));
  }, [activeSource?.filing_id, activeSource?.chunk_id]);

  useEffect(() => {
    if (!pdfUrl || !window.pdfjsLib || !canvasRef.current || !activeSource?.page_number) return;

    (async () => {
      const doc = await window.pdfjsLib.getDocument(pdfUrl).promise;
      const page = await doc.getPage(activeSource.page_number);
      const viewport = page.getViewport({ scale: 1.4 });
      const canvas = canvasRef.current!;
      const ctx = canvas.getContext("2d")!;
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      await page.render({ canvasContext: ctx, viewport }).promise;

      if (bbox && highlightRef.current) {
        const scaleX = viewport.width / page.view[2];
        const scaleY = viewport.height / page.view[3];
        Object.assign(highlightRef.current.style, {
          display: "block",
          left: `${bbox.x0 * scaleX}px`,
          top: `${bbox.y0 * scaleY}px`,
          width: `${(bbox.x1 - bbox.x0) * scaleX}px`,
          height: `${(bbox.y1 - bbox.y0) * scaleY}px`,
        });
      }
    })();
  }, [pdfUrl, bbox, activeSource?.page_number]);

  if (!activeSource) {
    return (
      <div className="source-viewer source-viewer--empty">
        <p>Click a citation or an exhibit to view its exact source page here.</p>
      </div>
    );
  }

  return (
    <div className="source-viewer">
      <div className="source-viewer__header">
        <span>{activeSource.ticker} · {activeSource.doc_type} · {activeSource.filing_date}</span>
        <span>Page {activeSource.page_number ?? "—"}</span>
      </div>

      {loading && <div className="source-viewer__loading">Loading source page…</div>}

      <div className="source-viewer__canvas-wrap">
        <canvas ref={canvasRef} />
        <div ref={highlightRef} className="source-viewer__highlight" />
      </div>

      {/* Markdown/plain-text fallback for non-PDF (HTML-native) filings */}
      {!activeSource.page_number && (
        <pre className="source-viewer__fallback-text">{activeSource.text}</pre>
      )}
    </div>
  );
}
