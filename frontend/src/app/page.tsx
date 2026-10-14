// app/page.tsx
//
// Top-level layout: Sidebar (chat history + new chat) | ChatPane (chat +
// citations + exhibits) | SourceViewer (PDF/markdown with highlight).
// `activeSessionId` lives here since both the sidebar and the chat pane
// need to agree on which conversation is open.

"use client";

import { useEffect, useState } from "react";
import ChatPane from "@/components/ChatPane";
import Sidebar from "@/components/Sidebar";
import SourceViewer from "@/components/SourceViewer";
import type { SourceChunk } from "@/lib/parseCitations";

const LAST_SESSION_KEY = "finsights:last-session-id";

export default function Home() {
  const [activeSource, setActiveSource] = useState<SourceChunk | null>(null);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [sidebarRefreshKey, setSidebarRefreshKey] = useState(0);

  // Resume the last open conversation on reload, instead of always landing
  // on a blank "New chat" -- read after mount (not in useState's
  // initializer) since localStorage isn't available during server render.
  useEffect(() => {
    const saved = localStorage.getItem(LAST_SESSION_KEY);
    if (saved) setActiveSessionId(saved);
  }, []);

  const selectSession = (id: string | null) => {
    setActiveSessionId(id);
    if (id) localStorage.setItem(LAST_SESSION_KEY, id);
    else localStorage.removeItem(LAST_SESSION_KEY);
  };

  return (
    <main className="app-shell">
      <Sidebar
        activeSessionId={activeSessionId}
        onSelectSession={selectSession}
        onNewChat={() => selectSession(null)}
        refreshKey={sidebarRefreshKey}
      />
      <div className="dual-pane">
        <section className="dual-pane__left">
          <ChatPane
            sessionId={activeSessionId}
            onSelectSource={setActiveSource}
            onSessionUpdated={(sessionId) => {
              selectSession(sessionId);
              setSidebarRefreshKey((k) => k + 1); // pulls the new/renamed session into the sidebar list
            }}
          />
        </section>
        <section className="dual-pane__right">
          <SourceViewer activeSource={activeSource} />
        </section>
      </div>
    </main>
  );
}
