// About page (version, credits, what this is), plus system health, worker control, QA limits and licences.
import { get, post, html, setHTML, busy } from "../ui.js";

export async function render(view) {
  const [about, worker] = await Promise.all([get("/api/about"), get("/api/worker")]);
  // the environment check is administrator-only; operators still see the rest of the page
  const doctor = await get("/api/doctor").catch(() => null);
  const comp = about.components || {};
  setHTML(view, html`<div class="page-head"><h1 class="sr-only">About Audiobook Studio</h1></div>
    <section class="card about-hero" aria-labelledby="about-h">
      <div class="about-logo" aria-hidden="true"><svg viewBox="0 0 32 32" width="52" height="52"><path d="M9 20v-4a7 7 0 0 1 14 0v4" stroke="#fff" stroke-width="2.2" fill="none"/><rect x="7" y="18" width="5" height="8" rx="2" fill="#fff"/><rect x="20" y="18" width="5" height="8" rx="2" fill="#fff"/></svg></div>
      <div>
        <div class="about-title"><h2 id="about-h" class="about-name">${about.name}</h2><span class="stage">${about.version_label}</span></div>
        <p class="about-credit"><strong>Developed by ${about.developer}</strong>, ${about.company}</p>
        <p class="muted small">${about.copyright}</p>
      </div>
    </section>
    <section class="card" aria-labelledby="what-h"><h2 id="what-h">What it is</h2>
      <p>A local, autonomous audiobook production studio for Apple Silicon Macs. Upload complete manuscripts, choose a narrator, queue them,
        and the Mac narrates them with Qwen3-TTS, checks every passage with an independent Whisper transcription, lets you review and correct
        anything flagged, and produces chapter-marked audiobook files for your own release process.</p>
      <p>Everything runs on this Mac. Manuscripts and voice recordings never leave it, and there is no telemetry.</p>
      <div class="callout info small"><strong>Version ${about.version_label}.</strong> Keep backups of your library. Automatic QA is not a substitute
        for listening, and nothing is uploaded to Audible, ACX or any other distributor: this studio produces local publication candidates only.</div>
      <dl class="about-facts">
        <div><dt>Version</dt><dd>${about.version_label}</dd></div>
        <div><dt>Build</dt><dd>${about.build || "development"}</dd></div>
        <div><dt>Developer</dt><dd>${about.developer}</dd></div>
        <div><dt>Company</dt><dd>${about.company}</dd></div>
        <div><dt>Narration engine</dt><dd>${about.engine_mode === "mlx" ? "MLX (real local inference)" : "Mock (test tones, not narration)"}</dd></div>
        <div><dt>Python</dt><dd>${about.python}</dd></div>
        ${Object.entries(comp).filter(([, v]) => v).map(([k, v]) => html`<div><dt>${k}</dt><dd>${v}</dd></div>`)}
      </dl>
    </section>
    <section class="card" aria-labelledby="w-h"><h2 id="w-h">GPU worker</h2>
      <p>${worker.online ? html`<span class="good-text">Online</span> – ${worker.state}${worker.message ? html`: ${worker.message}` : ""} (pid ${worker.pid})`
        : html`<span class="err">Offline</span> – no heartbeat.`} ${worker.supervisor && worker.supervisor.managed ? html` · restarts: ${worker.supervisor.restarts}` : ""}</p>
      <p class="small muted">One worker owns the GPU; inference never runs inside web requests. A crashed worker is restarted automatically and resumes where it stopped.</p>
      <button class="btn" data-act="restart">Restart worker</button> <span class="small muted">(administrators only)</span>
    </section>
    <section class="card" aria-labelledby="d-h"><h2 id="d-h">Environment check</h2>
      ${doctor ? html`<div class="table-wrap"><table class="table compact"><thead><tr><th scope="col">Check</th><th scope="col">Status</th><th scope="col">Detail</th></tr></thead>
      <tbody>${doctor.checks.map((c) => html`<tr><td>${c.name}</td><td><span class="badge tone-${c.status === "ok" ? "good" : c.status === "warn" ? "warn" : "bad"}">${c.status}</span></td><td class="small">${c.detail}</td></tr>`)}</tbody></table></div>
      <p class="small muted">Home <code>${about.home}</code></p>`
        : html`<p class="muted">The environment check is available to administrators.</p>`}
    </section>
    <section class="card" aria-labelledby="q-h"><h2 id="q-h">Limits of automatic QA</h2><ul>${about.qa_limitations.map((x) => html`<li>${x}</li>`)}</ul></section>
    <section class="card" aria-labelledby="l-h"><h2 id="l-h">Models, licences and your responsibilities</h2>
      <p class="small muted">Audiobook Studio is built on the following open components. It does not bundle their weights; they are downloaded from their public sources.</p>
      <div class="table-wrap"><table class="table compact"><thead><tr><th scope="col">Component</th><th scope="col">Source</th><th scope="col">Licence</th></tr></thead>
      <tbody>${about.licenses.map((l) => html`<tr><td>${l.component}</td><td class="small">${l.source}<br><code>${l.url}</code></td><td class="small">${l.license}</td></tr>`)}</tbody></table></div>
      <p class="small">Verify current model licences and commercial-use terms on the model cards, confirm you own the rights to each manuscript and every cloned voice,
        and check your distributor's technical and AI-narration policies.</p>
      <p class="small muted">Privacy: inference, transcription and storage are local. After model weights are downloaded you can disable downloads in Settings; Hugging Face telemetry is disabled by the launchers.</p>
    </section>
    <p class="small muted center">${about.name} ${about.version_label} · ${about.copyright}</p>`);
  const onClick = async (e) => {
    const b = e.target.closest("[data-act=restart]"); if (!b) return;
    await busy(b, () => post("/api/worker/restart"), "Worker restarting (it finishes or abandons the current segment safely)");
  };
  view.addEventListener("click", onClick);
  return () => view.removeEventListener("click", onClick);
}
