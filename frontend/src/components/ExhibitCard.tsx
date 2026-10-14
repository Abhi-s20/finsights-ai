// components/ExhibitCard.tsx
//
// Ported from the prototype's Exhibit.jsx -- the source excerpt card a
// citation marker points to. Now also clickable itself (not just the
// numbered marker), since with multi-company results the exhibit list is
// the primary way to jump the right-pane viewer to a specific filing/page.

"use client";

import type { SourceChunk } from "@/lib/parseCitations";

interface Props {
  index: number;
  source: SourceChunk;
  onOpen?: (source: SourceChunk) => void;
}

export default function ExhibitCard({ index, source, onOpen }: Props) {
  const preview = source.text.length > 420 ? source.text.slice(0, 420).trimEnd() + "…" : source.text;

  return (
    <div id={`exhibit-${index}`} className="exhibit" onClick={() => onOpen?.(source)}>
      <div className="exhibit__meta">
        <span className="exhibit__index">{index}</span>
        <span className="exhibit__ticker">{source.ticker}</span>
        <span className="exhibit__form">{source.doc_type}</span>
        <span className="exhibit__date">{source.filing_date}</span>
        {source.content_type === "TABLE" && <span className="exhibit__badge">Table</span>}
        <span className="exhibit__item">
          Item {source.item_number}
          {source.item_title ? `: ${source.item_title}` : ""}
        </span>
      </div>
      <p className="exhibit__text">{preview}</p>
      <button type="button" className="exhibit__link" onClick={(e) => { e.stopPropagation(); onOpen?.(source); }}>
        View in source pane →
      </button>
    </div>
  );
}
