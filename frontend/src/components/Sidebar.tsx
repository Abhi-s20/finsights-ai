// components/Sidebar.tsx
//
// "+ New chat" plus a list of past sessions, sourced from Postgres
// (chat_sessions/chat_messages) via GET /api/v1/sessions -- not
// localStorage, so history survives a page reload and, later, works
// across devices once auth exists.

"use client";

import { Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { fetchSessions, type ChatSession } from "@/lib/api";

interface Props {
  activeSessionId: string | null;
  onSelectSession: (id: string) => void;
  onNewChat: () => void;
  refreshKey: number; // bump this after a turn completes to pull in the new/updated session
}

export default function Sidebar({ activeSessionId, onSelectSession, onNewChat, refreshKey }: Props) {
  const [sessions, setSessions] = useState<ChatSession[]>([]);

  useEffect(() => {
    fetchSessions().then(setSessions).catch(() => setSessions([]));
  }, [refreshKey]);

  return (
    <aside className="sidebar">
      <div className="sidebar__brand">FinSights AI</div>

      <button type="button" className="sidebar__new-chat" onClick={onNewChat}>
        <Plus size={16} /> New chat
      </button>

      <div className="sidebar__list">
        {sessions.map((s) => (
          <button
            key={s.id}
            type="button"
            className={`sidebar__item ${s.id === activeSessionId ? "sidebar__item--active" : ""}`}
            onClick={() => onSelectSession(s.id)}
            title={s.title}
          >
            {s.title}
          </button>
        ))}
      </div>
    </aside>
  );
}
