"use client";
import { useEffect, useState } from "react";

export function useCountdown(due: string | null): { text: string; overdue: boolean } {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(t); }, []);
  if (!due) return { text: "—", overdue: false };
  const ms = new Date(due).getTime() - now;
  const abs = Math.abs(ms);
  const h = Math.floor(abs / 3.6e6), d = Math.floor(h / 24);
  const txt = d >= 1 ? `${d}d ${h % 24}h` : `${h}h ${Math.floor((abs % 3.6e6) / 6e4)}m`;
  return { text: ms < 0 ? `overdue by ${txt}` : `${txt} left`, overdue: ms < 0 };
}
