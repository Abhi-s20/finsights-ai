// components/CompanySelector.tsx
//
// Lets the analyst pin up to 5 companies for cross-comparison (the "3D
// Cross-Company RAG" differentiator). When non-empty, this list is passed
// as `tickers` on the query request, overriding auto-detection -- useful
// when the question text itself doesn't name the tickers ("compare their
// R&D spend" after already selecting AAPL/MSFT/GOOGL).

"use client";

import { X } from "lucide-react";
import { useState } from "react";

interface Props {
  selected: string[];
  onChange: (tickers: string[]) => void;
  maxCompanies?: number;
}

export default function CompanySelector({ selected, onChange, maxCompanies = 5 }: Props) {
  const [input, setInput] = useState("");

  const addTicker = () => {
    const ticker = input.trim().toUpperCase();
    if (!ticker || selected.includes(ticker) || selected.length >= maxCompanies) return;
    onChange([...selected, ticker]);
    setInput("");
  };

  const removeTicker = (ticker: string) => onChange(selected.filter((t) => t !== ticker));

  return (
    <div className="company-selector">
      <div className="company-selector__chips">
        {selected.map((t) => (
          <span key={t} className="ticker-chip">
            {t}
            <button type="button" onClick={() => removeTicker(t)} aria-label={`Remove ${t}`}>
              <X size={12} />
            </button>
          </span>
        ))}
        {selected.length < maxCompanies && (
          <input
            className="company-selector__input"
            value={input}
            placeholder={selected.length === 0 ? "Add ticker (e.g. AAPL)" : "Add another…"}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") { e.preventDefault(); addTicker(); }
            }}
            onBlur={addTicker}
          />
        )}
      </div>
      <span className="company-selector__hint">{selected.length}/{maxCompanies} companies pinned</span>
    </div>
  );
}
