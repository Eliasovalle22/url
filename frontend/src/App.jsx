import { useCallback, useEffect, useMemo, useRef, useState } from "react";

const API = import.meta.env.VITE_API_URL || ""; // en dev usa el proxy de Vite
const HISTORY_KEY = "descargalo-history-v1";
const THEME_KEY = "descargalo-theme";

const PLATFORMS = [
  { name: "YouTube", initial: "YT", color: "#FF0033" },
  { name: "TikTok", initial: "TT", color: "#111111" },
  { name: "Instagram", initial: "IG", color: "#E1306C" },
  { name: "Facebook", initial: "FB", color: "#1877F2" },
  { name: "X", initial: "X", color: "#000000" },
  { name: "Vimeo", initial: "VM", color: "#1AB7EA" },
  { name: "Twitch", initial: "TW", color: "#9146FF" },
  { name: "SoundCloud", initial: "SC", color: "#FF5500" },
];

function isValidUrl(v) {
  return /^https?:\/\/\S+$/i.test((v || "").trim());
}

function splitLinks(text) {
  return [...new Set(
    (text || "").split(/[\n,]+/).map((s) => s.trim()).filter(Boolean)
  )];
}

function loadHistory() {
  try { return JSON.parse(localStorage.getItem(HISTORY_KEY)) || []; }
  catch { return []; }
}

function fmtEta(sec) {
  if (sec == null || sec < 0) return "";
  const m = Math.floor(sec / 60), s = Math.round(sec % 60);
  return m > 0 ? `quedan ~${m} min` : `quedan ~${s} s`;
}

let cardSeq = 0;

export default function App() {
  const [input, setInput] = useState("");
  const [kind, setKind] = useState("video");
  const [cards, setCards] = useState([]); // {key,url,phase,info,quality,error,job}
  const [globalError, setGlobalError] = useState("");
  const [history, setHistory] = useState(loadHistory);
  const [theme, setTheme] = useState(
    () => localStorage.getItem(THEME_KEY) ||
      (window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light")
  );
  const timer = useRef(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  const active = useMemo(
    () => cards.some((c) => c.job && ["queued", "downloading"].includes(c.job.status)),
    [cards]
  );

  const patchCard = useCallback((key, patch) => {
    setCards((prev) => prev.map((c) => (c.key === key ? { ...c, ...patch } : c)));
  }, []);

  // --- agregar enlaces (uno o varios, separados por coma o salto de línea) ---
  function handleAdd(e) {
    e?.preventDefault();
    setGlobalError("");
    const links = splitLinks(input);
    if (!links.length) {
      setGlobalError("Pega al menos un enlace.");
      return;
    }
    const bad = links.filter((l) => !isValidUrl(l));
    if (bad.length) {
      setGlobalError(`Estos ${bad.length === 1 ? "enlace no es válido" : "enlaces no son válidos"}: ${bad.slice(0, 2).join(", ")}${bad.length > 2 ? "…" : ""}`);
      return;
    }
    const existing = new Set(cards.map((c) => c.url));
    const fresh = links.filter((l) => !existing.has(l)).map((url) => ({
      key: `${Date.now()}-${cardSeq++}`,
      url, phase: "info", info: null, quality: "best", error: "", job: null,
    }));
    if (!fresh.length) {
      setGlobalError("Esos enlaces ya están en la lista.");
      return;
    }
    setCards((prev) => [...fresh, ...prev]);
    setInput("");
    fresh.forEach((c) => fetchInfo(c.key, c.url));
  }

  async function fetchInfo(key, url) {
    try {
      const res = await fetch(`${API}/api/info`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "No pudimos leer ese enlace.");
      patchCard(key, { phase: "choose", info: data, quality: "best" });
    } catch (err) {
      patchCard(key, { phase: "error", error: err.message });
    }
  }

  // --- iniciar descarga con progreso ---
  async function startJob(card) {
    patchCard(card.key, { error: "" });
    try {
      const res = await fetch(`${API}/api/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: card.url, kind, quality: card.quality }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "No se pudo iniciar la descarga.");
      patchCard(card.key, { phase: "downloading", job: data });
    } catch (err) {
      patchCard(card.key, { error: err.message });
    }
  }

  // --- polling de trabajos activos ---
  useEffect(() => {
    if (!active) return;
    timer.current = setInterval(async () => {
      const actives = cards.filter((c) => c.job && ["queued", "downloading"].includes(c.job.status));
      for (const c of actives) {
        try {
          const res = await fetch(`${API}/api/jobs/${c.job.id}`);
          const data = await res.json();
          if (!res.ok) throw new Error();
          patchCard(c.key, { job: data });
          if (data.status === "ready") {
            // descarga automática + historial
            const a = document.createElement("a");
            a.href = `${API}/api/jobs/${data.id}/file`;
            a.download = data.filename || "descarga";
            document.body.appendChild(a);
            a.click();
            a.remove();
            patchCard(c.key, { phase: "done" });
            setHistory((prev) => {
              const entry = {
                title: data.title, thumbnail: data.thumbnail, kind: data.kind,
                quality: data.quality, sizeLabel: data.sizeLabel,
                date: new Date().toISOString(), url: data.url,
              };
              const next = [entry, ...prev].slice(0, 30);
              localStorage.setItem(HISTORY_KEY, JSON.stringify(next));
              return next;
            });
          } else if (data.status === "error") {
            patchCard(c.key, { phase: "error", error: data.error || "Falló la descarga." });
          }
        } catch {
          /* reintenta en el siguiente ciclo */
        }
      }
    }, 900);
    return () => clearInterval(timer.current);
  }, [active, cards, patchCard]);

  function removeCard(key) {
    const card = cards.find((c) => c.key === key);
    if (card?.job && ["queued", "downloading"].includes(card.job.status)) {
      fetch(`${API}/api/jobs/${card.job.id}`, { method: "DELETE" }).catch(() => {});
    }
    setCards((prev) => prev.filter((c) => c.key !== key));
  }

  function repeatFromHistory(item) {
    if (cards.some((c) => c.url === item.url)) return;
    const card = {
      key: `${Date.now()}-${cardSeq++}`,
      url: item.url, phase: "info", info: null,
      quality: item.quality || "best", error: "", job: null,
    };
    setCards((prev) => [card, ...prev]);
    fetchInfo(card.key, card.url);
  }

  function clearHistory() {
    setHistory([]);
    localStorage.removeItem(HISTORY_KEY);
  }

  async function handlePaste() {
    try {
      const text = await navigator.clipboard.readText();
      if (text) setInput((prev) => (prev ? prev + "\n" + text.trim() : text.trim()));
    } catch {
      setGlobalError("Tu navegador bloqueó el portapapeles. Pega con Ctrl+V.");
    }
  }

  return (
    <div className="page">
      <div className="bg-decor" aria-hidden="true" />
      <header className="header">
        <div className="container header-inner">
          <a className="brand" href="#" onClick={(e) => e.preventDefault()}>
            <span className="brand-mark" aria-hidden="true">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 4v11m0 0l-4-4m4 4l4-4" />
                <path d="M4 19h16" />
              </svg>
            </span>
            <span className="brand-name">Descargalo</span>
            <span className="brand-badge">Gratis</span>
          </a>
          <nav className="header-nav">
            <a href="#descargas">Descargas</a>
            <a href="#plataformas">Plataformas</a>
            <a href="#historial">Historial</a>
          </nav>
          <button
            className="theme-btn"
            onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
            aria-label={theme === "dark" ? "Cambiar a modo claro" : "Cambiar a modo oscuro"}
            title={theme === "dark" ? "Modo claro" : "Modo oscuro"}
          >
            {theme === "dark" ? (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="12" cy="12" r="4" /><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4l1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></svg>
            ) : (
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" /></svg>
            )}
          </button>
        </div>
      </header>

      <main className="container">
        <section className="hero">
          <p className="pill">
            <span className="pulse" aria-hidden="true" />
            YouTube · TikTok · Instagram · +1000 sitios
          </p>
          <h1>
            Pega el enlace.
            <br />
            <span className="accent">Descarga.</span> Así de simple.
          </h1>
          <p className="subtitle">
            Videos en <strong>MP4</strong> y música en <strong>MP3</strong>. Puedes pegar{" "}
            <strong>varios enlaces</strong> a la vez, ver el progreso y tu historial.
          </p>
        </section>

        <section className="card" aria-label="Descargador" id="descargas">
          <form onSubmit={handleAdd} className="url-row">
            <label className="sr-only" htmlFor="url">Enlaces del video o audio (uno por línea o separados por coma)</label>
            <div className="url-field">
              <svg className="field-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
                <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
              </svg>
              <input
                id="url"
                type="text"
                inputMode="url"
                autoComplete="off"
                spellCheck="false"
                placeholder="https://…  (puedes pegar varios separados por coma o salto de línea)"
                value={input}
                onChange={(e) => setInput(e.target.value)}
              />
              {input && (
                <button type="button" className="icon-btn" onClick={() => setInput("")} aria-label="Limpiar">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M18 6L6 18M6 6l12 12" /></svg>
                </button>
              )}
            </div>
            <div className="url-actions">
              <button type="button" className="btn btn-ghost" onClick={handlePaste}>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="8" y="2" width="8" height="4" rx="1" /><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2" /></svg>
                Pegar
              </button>
              <button type="submit" className="btn btn-secondary">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><path d="M12 5v14m0 0l-6-6m6 6l6-6" /></svg>
                Agregar
              </button>
            </div>
          </form>

          <div className="options-row">
            <div className="segmented" role="tablist" aria-label="Tipo de descarga">
              <button role="tab" aria-selected={kind === "video"} className={kind === "video" ? "active" : ""} onClick={() => setKind("video")} type="button">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="2" y="6" width="13" height="12" rx="2" /><path d="M15 10l7-3v10l-7-3" /></svg>
                Video MP4
              </button>
              <button role="tab" aria-selected={kind === "audio"} className={kind === "audio" ? "active" : ""} onClick={() => setKind("audio")} type="button">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" /></svg>
                Audio MP3 {kind === "audio" && ""}
              </button>
            </div>
          </div>

          {globalError && <p className="alert alert-error" role="alert">{globalError}</p>}

          {cards.length > 0 && (
            <div className="job-list">
              {cards.map((card) => (
                <article className="job" key={card.key}>
                  <div className="job-top">
                    <div className="job-main">
                      {card.phase === "info" && (
                        <>
                          <div className="skeleton-line" style={{ width: "70%" }} />
                          <p className="job-url">{card.url}</p>
                          <p className="job-status"><span className="spinner" aria-hidden="true" /> Leyendo enlace…</p>
                        </>
                      )}
                      {(card.phase === "choose" || card.phase === "downloading" || card.phase === "done") && card.info && (
                        <>
                          <div className="job-head">
                            {card.info.thumbnail && <img src={card.info.thumbnail} alt="" loading="lazy" />}
                            <div>
                              <h3>{card.info.title}</h3>
                              <p className="job-meta">{card.info.uploader} · {card.info.durationLabel}</p>
                            </div>
                          </div>
                        </>
                      )}
                      {card.phase === "error" && (
                        <>
                          <p className="job-url">{card.url}</p>
                          <p className="alert alert-error" role="alert">{card.error}</p>
                          <div className="job-btns">
                            <button className="btn btn-secondary btn-sm" onClick={() => { patchCard(card.key, { phase: "info", error: "" }); fetchInfo(card.key, card.url); }}>Reintentar</button>
                            <button className="btn btn-ghost btn-sm" onClick={() => removeCard(card.key)}>Quitar</button>
                          </div>
                        </>
                      )}
                    </div>
                    {card.phase !== "downloading" && (
                      <button className="icon-btn" onClick={() => removeCard(card.key)} aria-label="Quitar de la lista">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M18 6L6 18M6 6l12 12" /></svg>
                      </button>
                    )}
                  </div>

                  {card.phase === "choose" && card.info && (
                    <div className="job-choose">
                      {kind === "video" ? (
                        <label className="quality">
                          <span>Calidad</span>
                          <select value={card.quality} onChange={(e) => patchCard(card.key, { quality: e.target.value })}>
                            {(card.info.qualities || [{ id: "best", label: "La mejor" }]).map((q) => (
                              <option key={q.id} value={q.id}>
                                {q.label}{q.sizeLabel ? ` (${q.sizeLabel})` : ""}
                              </option>
                            ))}
                          </select>
                        </label>
                      ) : (
                        <p className="job-meta">MP3 a 192 kbps{card.info.audioSizeLabel ? ` (${card.info.audioSizeLabel})` : ""}</p>
                      )}
                      <button className="btn btn-primary" onClick={() => startJob(card)}>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 4v11m0 0l-4-4m4 4l4-4" /><path d="M4 19h16" /></svg>
                        Descargar {kind === "audio" ? "MP3" : "MP4"}
                      </button>
                    </div>
                  )}

                  {card.phase === "downloading" && card.job && (
                    <div className="progress-wrap">
                      <div className="progress-track" role="progressbar"
                        aria-valuemin="0" aria-valuemax="100"
                        aria-valuenow={card.job.progress ?? 0}
                        aria-label={`Progreso de ${card.job.title || card.url}`}>
                        <div
                          className={`progress-fill ${card.job.progress == null ? "indeterminate" : ""}`}
                          style={card.job.progress != null ? { width: `${card.job.progress}%` } : undefined}
                        />
                      </div>
                      <p className="job-status">
                        {card.job.status === "queued" && "En cola…"}
                        {card.job.status === "downloading" && (
                          card.job.progress != null
                            ? `${card.job.progress}%${card.job.speedLabel ? ` · ${card.job.speedLabel}` : ""}${card.job.eta != null ? ` · ${fmtEta(card.job.eta)}` : ""}`
                            : `Descargando…${card.job.speedLabel ? ` ${card.job.speedLabel}` : ""}`
                        )}
                      </p>
                    </div>
                  )}

                  {card.phase === "done" && card.job && (
                    <div className="job-done">
                      <p className="alert alert-ok" role="status">
                        Listo{card.job.sizeLabel ? ` (${card.job.sizeLabel})` : ""}. Si no empezó solo, pulsa aquí:
                      </p>
                      <div className="job-btns">
                        <a className="btn btn-primary btn-sm" href={`${API}/api/jobs/${card.job.id}/file`} download={card.job.filename || true}>
                          Guardar de nuevo
                        </a>
                        <button className="btn btn-ghost btn-sm" onClick={() => removeCard(card.key)}>Quitar</button>
                      </div>
                    </div>
                  )}
                </article>
              ))}
            </div>
          )}
          <p className="legal">Solo descarga contenido propio o con permiso. Respeta los derechos de autor.</p>
        </section>

        <section className="steps">
          {[
            { n: "1", t: "Pega uno o varios enlaces", d: "YouTube, TikTok, Instagram… separados por coma o salto de línea." },
            { n: "2", t: "Elige formato y calidad", d: "Video MP4 con tamaño estimado, o solo el audio en MP3." },
            { n: "3", t: "Sigue el progreso", d: "Barra con % y velocidad. Al terminar se descarga solo." },
          ].map((s) => (
            <div className="step" key={s.n}>
              <span className="step-num">{s.n}</span>
              <h3>{s.t}</h3>
              <p>{s.d}</p>
            </div>
          ))}
        </section>

        <section id="plataformas" className="section">
          <h2>Funciona con tus sitios favoritos</h2>
          <p className="section-sub">Compatibilidad con más de 1000 sitios gracias al motor de descarga abierto.</p>
          <div className="platforms">
            {PLATFORMS.map((p) => (
              <div className="platform" key={p.name}>
                <span className="platform-dot" style={{ background: p.color }}>{p.initial}</span>
                {p.name}
              </div>
            ))}
          </div>
        </section>

        <section id="historial" className="section">
          <div className="section-head">
            <div>
              <h2>Historial</h2>
              <p className="section-sub" style={{ textAlign: "left", margin: "4px 0 0" }}>Tus últimas descargas en este navegador.</p>
            </div>
            {history.length > 0 && (
              <button className="btn btn-ghost btn-sm" onClick={clearHistory}>Borrar</button>
            )}
          </div>
          {history.length === 0 ? (
            <p className="empty">Aún no hay descargas. Aparecerán aquí cuando completes la primera.</p>
          ) : (
            <div className="history-list">
              {history.map((h, i) => (
                <div className="history-item" key={`${h.date}-${i}`}>
                  {h.thumbnail && <img src={h.thumbnail} alt="" loading="lazy" />}
                  <div className="history-body">
                    <p className="history-title">{h.title || h.url}</p>
                    <p className="history-meta">
                      {h.kind === "audio" ? "MP3" : "MP4"} · {h.sizeLabel || ""} ·{" "}
                      {new Date(h.date).toLocaleString("es", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}
                    </p>
                  </div>
                  <button className="btn btn-secondary btn-sm" onClick={() => repeatFromHistory(h)}>Otra vez</button>
                </div>
              ))}
            </div>
          )}
        </section>
      </main>

      <footer className="footer">
        <div className="container footer-inner">
          <p><strong>Descargalo</strong> · Hecho con Python + React. Úsalo solo con contenido que tengas derecho a descargar.</p>
        </div>
      </footer>
    </div>
  );
}
