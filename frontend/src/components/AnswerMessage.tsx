// components/AnswerMessage.tsx
// Ported from the prototype's AnswerMessage.jsx.

"use client";

import { parseCitations, type SourceChunk } from "@/lib/parseCitations";
import Citation from "./Citation";
import ExhibitCard from "./ExhibitCard";

interface Props {
  answer: string;
  tickers: string[];
  sources: SourceChunk[];
  onSelectSource?: (source: SourceChunk) => void;
}

export default function AnswerMessage({ answer, tickers, sources, onSelectSource }: Props) {
  const segments = parseCitations(answer, sources);

  return (
    <div className="turn turn--answer">
      {tickers?.length > 0 && (
        <div className="answer__tickers">
          {tickers.map((t) => (
            <span key={t} className="ticker-tag">{t}</span>
          ))}
        </div>
      )}

      <div className="answer__text">
        {segments.map((seg, i) =>
          seg.type === "text" ? (
            <span key={i}>{seg.content}</span>
          ) : (
            <Citation key={i} segment={seg} onSelectSource={onSelectSource} />
          )
        )}
      </div>

      {sources?.length > 0 && (
        <div className="exhibits">
          <div className="exhibits__label">Exhibits</div>
          {sources.map((source, i) => (
            <ExhibitCard key={source.chunk_id} index={i + 1} source={source} onOpen={onSelectSource} />
          ))}
        </div>
      )}
    </div>
  );
}
