// components/ChatPane.tsx
//
// Left pane: company selector, chat stream, streaming answer with inline
// citations. `sessionId` is controlled by the parent (page.tsx) so
// switching sessions in the sidebar or starting a new chat both flow
// through one place instead of two components disagreeing about which
// conversation is active.

"use client";

import { useEffect, useRef, useState } from "react";
import { fetchSessionMessages, streamQuery } from "@/lib/api";
import type { SourceChunk } from "@/lib/parseCitations";
import AnswerMessage from "./AnswerMessage";
import CompanySelector from "./CompanySelector";

type Turn =
  | { role: "user"; content: string }
  | { role: "assistant"; content: string; tickers: string[]; sources: SourceChunk[]; streaming: boolean };

interface Props {
  sessionId: string | null;
  onSelectSource: (source: SourceChunk) => void;
  onSessionUpdated: (sessionId: string) => void; // fires once a turn completes, so the sidebar can refresh
}

export default function ChatPane({ sessionId, onSelectSource, onSessionUpdated }: Props) {
  const [pinnedTickers, setPinnedTickers] = useState<string[]>([]);
  const [input, setInput] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const ownSessionIdRef = useRef<string | null>(null); // the session this chat window itself just produced, if any

  // Load a past session's messages whenever the active session changes --
  // but NOT when the change is just this same chat's own session_id
  // arriving mid-stream (see onSources below): re-fetching then would
  // clobber the in-progress turn with an empty/stale server response and
  // crash the next delta update against a turn that no longer exists.
  useEffect(() => {
    if (sessionId === ownSessionIdRef.current) return;
    ownSessionIdRef.current = sessionId;

    if (!sessionId) {
      setTurns([]);
      return;
    }
    fetchSessionMessages(sessionId).then((messages) => {
      setTurns(
        messages.map((m) =>
          m.role === "USER"
            ? { role: "user", content: m.content }
            : { role: "assistant", content: m.content, tickers: m.tickers_detected, sources: m.sources, streaming: false }
        )
      );
    });
  }, [sessionId]);

  // Immutable update of the last (assistant) turn. Updaters passed to setState must be
  // pure: React StrictMode (dev) runs them twice, so mutating the turn object in place
  // appended every streamed chunk twice and garbled the answer.
  const updateLastAssistant = (fn: (turn: Extract<Turn, { role: "assistant" }>) => Extract<Turn, { role: "assistant" }>) =>
    setTurns((t) => {
      const last = t[t.length - 1];
      if (!last || last.role !== "assistant") return t;
      return [...t.slice(0, -1), fn(last)];
    });

  const ask = async () => {
    const question = input.trim();
    if (!question || busy) return;
    setInput("");
    setBusy(true);

    setTurns((t) => [
      ...t,
      { role: "user", content: question },
      { role: "assistant", content: "", tickers: [], sources: [], streaming: true },
    ]);

    await streamQuery(
      question,
      {
        onSources: ({ sources, tickers_detected, session_id }) => {
          ownSessionIdRef.current = session_id; // mark before the parent's prop update comes back down, so the effect above skips it
          updateLastAssistant((turn) => ({ ...turn, sources, tickers: tickers_detected }));
          onSessionUpdated(session_id);
        },
        onDelta: (text) => updateLastAssistant((turn) => ({ ...turn, content: turn.content + text })),
        onDone: () => {
          updateLastAssistant((turn) => ({ ...turn, streaming: false }));
          setBusy(false);
        },
        onError: (message) => {
          updateLastAssistant((turn) => ({ ...turn, content: `⚠️ ${message}`, streaming: false }));
          setBusy(false);
        },
      },
      { tickers: pinnedTickers.length ? pinnedTickers : undefined, sessionId: sessionId ?? undefined }
    );
  };

  return (
    <div className="chat-pane">
      <CompanySelector selected={pinnedTickers} onChange={setPinnedTickers} />

      <div className="chat-pane__stream">
        {turns.map((turn, i) =>
          turn.role === "user" ? (
            <div key={i} className="turn turn--user">{turn.content}</div>
          ) : (
            <AnswerMessage
              key={i}
              answer={turn.content}
              tickers={turn.tickers}
              sources={turn.sources}
              onSelectSource={onSelectSource}
            />
          )
        )}
      </div>

      <div className="chat-pane__input">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); }
          }}
          placeholder={pinnedTickers.length > 1
            ? `Ask a question comparing ${pinnedTickers.join(", ")}…`
            : "Ask about revenue, risk factors, related-party transactions…"}
          disabled={busy}
        />
        <button type="button" onClick={ask} disabled={busy || !input.trim()}>
          {busy ? "Thinking…" : "Ask"}
        </button>
      </div>
    </div>
  );
}
