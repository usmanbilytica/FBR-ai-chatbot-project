import React, { useEffect, useState } from "react";

const API = import.meta.env.VITE_API_BASE || "";

export default function App() {
  const [question, setQuestion] = useState("");
  const [suggestions, setSuggestions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [me, setMe] = useState(null);
  const [userid, setUserid] = useState("");
  const [authError, setAuthError] = useState("");
  const [loginBusy, setLoginBusy] = useState(false);

  async function refreshMe() {
    const params = new URLSearchParams(window.location.search);
    const launchUser = params.get("userid");
    const url = launchUser
      ? `${API}/api/v1/me?userid=${encodeURIComponent(launchUser)}`
      : `${API}/api/v1/me`;
    const r = await fetch(url, { credentials: "include" });
    if (r.status === 401) {
      setMe({ authenticated: false });
      return;
    }
    const data = await r.json();
    setMe(data);
    if (launchUser && data.authenticated) {
      // Clean launch param from URL after cookie is set
      window.history.replaceState({}, "", window.location.pathname);
    }
  }

  useEffect(() => {
    refreshMe().catch(() => setMe({ authenticated: false }));
    fetch(`${API}/api/v1/chat/suggestions`)
      .then((r) => r.json())
      .then((d) => setSuggestions(d.suggestions || []))
      .catch(() => {});
  }, []);

  async function login(e) {
    e.preventDefault();
    setAuthError("");
    setLoginBusy(true);
    try {
      const res = await fetch(`${API}/api/v1/auth/login`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ userid: Number(userid) }),
      });
      const data = await res.json();
      if (!res.ok) {
        setAuthError(data.detail || "Login failed");
        return;
      }
      setMe({ ...data, authenticated: true });
    } catch (err) {
      setAuthError(String(err));
    } finally {
      setLoginBusy(false);
    }
  }

  async function logout() {
    await fetch(`${API}/api/v1/auth/logout`, { method: "POST", credentials: "include" });
    setMe({ authenticated: false });
    setResult(null);
  }

  async function ask() {
    if (!question.trim()) return;
    if (!me?.authenticated && me?.access_id == null) {
      setAuthError("Sign in with your HRMS UserID before asking.");
      return;
    }
    setLoading(true);
    setResult(null);
    setAuthError("");
    try {
      const res = await fetch(`${API}/api/v1/chat/ask`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (res.status === 401) {
        setMe({ authenticated: false });
        setAuthError("Session expired — please sign in again.");
        return;
      }
      const data = await res.json();
      if (!res.ok) {
        setResult({ answer: data.detail || "Request failed", error: true });
        return;
      }
      setResult(data);
    } catch (e) {
      setResult({ answer: String(e), error: true });
    } finally {
      setLoading(false);
    }
  }

  const signedIn = Boolean(me?.authenticated || (me?.access_id != null && me?.user_id != null));

  return (
    <div className="app">
      <div className="brand">FBR HR Decision Support</div>
      <div className="sub">
        Live multi-source assistant — HRMS · Personal Files · Policies · Rule engines
      </div>

      {!signedIn ? (
        <div className="panel auth">
          <strong>Sign in</strong>
          <p className="meta">Use your HRMS UserID (launch token must be valid and unexpired).</p>
          <form className="row" onSubmit={login}>
            <input
              type="number"
              min="1"
              value={userid}
              onChange={(e) => setUserid(e.target.value)}
              placeholder="HRMS UserID"
              required
            />
            <button type="submit" disabled={loginBusy}>
              {loginBusy ? "…" : "Sign in"}
            </button>
          </form>
          {authError && <div className="meta error">{authError}</div>}
        </div>
      ) : (
        <>
          <div className="identity">
            AccessID: {me?.access_id ?? "—"} · Employee: {me?.employee_id ?? "—"} · User:{" "}
            {me?.user_id ?? "—"}
            <button type="button" className="linkish" onClick={logout}>
              Sign out
            </button>
          </div>

          <div className="suggestions">
            {suggestions.map((s) => (
              <button key={s} className="chip" type="button" onClick={() => setQuestion(s)}>
                {s}
              </button>
            ))}
          </div>

          <div className="panel">
            <div className="row">
              <textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="Ask about sanctioned strength, transfer eligibility, promotion, Personal Files, or policies…"
              />
              <button type="button" onClick={ask} disabled={loading}>
                {loading ? "…" : "Ask"}
              </button>
            </div>
            {authError && <div className="meta error">{authError}</div>}
          </div>

          {result && (
            <div className="panel" style={{ marginTop: 16 }}>
              <strong>Answer</strong>
              <div className="answer">{result.answer}</div>
              <div className="meta">
                topic: {result.topic || "—"} · request: {result.request_id || "—"} ·{" "}
                {result.duration_ms ? `${Math.round(result.duration_ms)} ms` : ""}
              </div>
              {!!(result.rule_trace || []).length && (
                <div className="trace">
                  <strong>Rule trace</strong>
                  {(result.rule_trace || []).map((t, i) => (
                    <div className="item" key={i}>
                      {t.status}: {(t.reasons || []).join(" · ")}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
