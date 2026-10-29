// System health, worker control, QA limitations and model licences.
import { get, post, html, setHTML, busy } from "../ui.js";

export async function render(view) {
  const [about, doctor, worker] = await Promise.all([get("/api/about"), get("/api/doctor"), get("/api/worker")]);
  setHTML(view, html`<div class="page-head"><h1>System &amp; Licenses</h1></div>
    <section class="card" aria-labelledby="w-h"><h2 id="w-h">GPU worker</h2>
      <p>${worker.online ? html`<span class="good-text">Online</span> – ${worker.state}${worker.message ? html`: ${worker.message}` : ""} (pid ${worker.pid})`
        : html`<span class="err">Offline</span> – no heartbeat.`} ${worker.supervisor && worker.supervisor.managed ? html` · restarts: ${worker.supervisor.restarts}` : ""}</p>
      <p class="small muted">One worker owns the GPU; inference never runs inside web requests. A crashed worker is restarted automatically and resumes where it stopped.</p>
      <button class="btn" data-act="restart">Restart worker</button>
    </section>
    <section class="card" aria-labelledby="d-h"><h2 id="d-h">Environment check</h2>
      <table class="table compact"><thead><tr><th scope="col">Check</th><th scope="col">Status</th><th scope="col">Detail</th></tr></thead>
      <tbody>${doctor.checks.map((c) => html`<tr><td>${c.name}</td><td><span class="badge tone-${c.status === "ok" ? "good" : c.status === "warn" ? "warn" : "bad"}">${c.status}</span></td><td class="small">${c.detail}</td></tr>`)}</tbody></table>
      <p class="small muted">Studio ${about.version} · Python ${about.python} · engine <strong>${about.engine_mode}</strong> · home <code>${about.home}</code></p>
    </section>
    <section class="card" aria-labelledby="q-h"><h2 id="q-h">Limits of automatic QA</h2><ul>${about.qa_limitations.map((x) => html`<li>${x}</li>`)}</ul></section>
    <section class="card" aria-labelledby="l-h"><h2 id="l-h">Models, licences and your responsibilities</h2>
      <table class="table compact"><thead><tr><th scope="col">Component</th><th scope="col">Source</th><th scope="col">Licence</th></tr></thead>
      <tbody>${about.licenses.map((l) => html`<tr><td>${l.component}</td><td class="small">${l.source}<br><code>${l.url}</code></td><td class="small">${l.license}</td></tr>`)}</tbody></table>
      <p class="small">Verify current model licences and commercial-use terms on the model cards, confirm you own the rights to each manuscript and every cloned voice,
        and check your distributor's technical and AI-narration policies. This studio does not upload anything: it produces local publication candidates.</p>
      <p class="small muted">Privacy: inference, transcription and storage are local. After model weights are downloaded you can disable downloads in Settings; Hugging Face telemetry is disabled by the launchers.</p>
    </section>`);
  const onClick = async (e) => {
    const b = e.target.closest("[data-act=restart]"); if (!b) return;
    await busy(b, () => post("/api/worker/restart"), "Worker restarting (it finishes or abandons the current segment safely)");
  };
  view.addEventListener("click", onClick);
  return () => view.removeEventListener("click", onClick);
}
