// components/Citation.tsx
//
// One inline citation marker (the small "[2]" mid-sentence in an answer).
// Clicking it does two things (both ported/extended from the prototype's
// Citation.jsx, which only scrolled the exhibit list): scrolls+flashes the
// matching exhibit card below, AND tells the right-pane viewer to jump to
// that source's page/highlight -- this is the "citation links to exact
// source page" requirement from the dual-pane spec.

"use client";

import type { Segment, SourceChunk } from "@/lib/parseCitations";

interface Props {
  segment: Extract<Segment, { type: "citation" }>;
  onSelectSource?: (source: SourceChunk) => void;
}

export default function Citation({ segment, onSelectSource }: Props) {
  if (segment.index === null || !segment.source) {
    return <span className="citation citation--unmatched">{segment.raw}</span>;
  }

  const handleClick = () => {
    const el = document.getElementById(`exhibit-${segment.index}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("exhibit--flash");
      setTimeout(() => el.classList.remove("exhibit--flash"), 900);
    }
    onSelectSource?.(segment.source!);
  };

  return (
    <button
      type="button"
      className="citation"
      onClick={handleClick}
      aria-label={`Jump to source ${segment.index}: ${segment.source.ticker} ${segment.source.doc_type}`}
    >
      {segment.index}
    </button>
  );
}
