"use client";
import { useState } from "react";

export default function Login() {
  const [token, setToken] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  async function go() {
    const r = await fetch("/api/dev-login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
    setMsg(r.ok ? "Signed in." : "Token rejected.");
  }
  return (
    <>
      <h1>Sign in (local development)</h1>
      <p className="muted">Paste a development token (python scripts/make_dev_token.py reviewer approver). Production uses Keycloak OIDC.</p>
      <label>Token<textarea value={token} onChange={(e) => setToken(e.target.value)} rows={4} /></label>
      <button onClick={go}>Sign in</button>
      {msg && <p role="status">{msg}</p>}
    </>
  );
}
