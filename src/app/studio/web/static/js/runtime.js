// Formatting for the server's `runtime` dicts (studio/runtime.py): estimated time left / queued estimate / actual processing time.
// The ONLY place that turns a runtime dict into text. Views call fmtRuntime(book.runtime) and never format these themselves.
import { html } from "./ui.js";

/** Seconds as "3 h 12 m" (>= 1 h, rounded to the minute), "5 m 10 s" (>= 1 min) or "45 s". Unknown -> "–". */
export function fmtSpan(seconds) {
  if (seconds === null || seconds === undefined || isNaN(seconds)) return "–";
  const r = Math.round(Math.max(0, Number(seconds)));
  if (r >= 3570) {  // 59 m 30 s and up reads as hours; rounded to the minute
    const total = Math.round(r / 60);
    return `${Math.floor(total / 60)} h ${String(total % 60).padStart(2, "0")} m`;
  }
  if (r >= 60) return `${Math.floor(r / 60)} m ${String(r % 60).padStart(2, "0")} s`;
  return `${r} s`;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
/** Epoch seconds as local 24-hour "14:30"; "Thu 14:30" when more than 12 h from now. Unknown -> "–". */
export function fmtClock(epoch) {
  if (epoch === null || epoch === undefined || isNaN(epoch)) return "–";
  const d = new Date(epoch * 1000);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return Math.abs(d.getTime() - Date.now()) > 12 * 3600 * 1000 ? `${DAYS[d.getDay()]} ${hm}` : hm;
}

/**
 * HTML fragment for a runtime dict; absent/null -> empty. `basis` is the tooltip. opts.compact gives the short form for tight spots
 * (no "about", no start/finish clock).
 *   running  "about 1 h 20 m left · done about 14:30"
 *   queued   "about 2 h 05 m processing · starts about 15:40"   (paused: "paused · about 2 h 05 m processing once resumed")
 *   finished "processed in 3 h 12 m"
 *   idle     "about 40 m to narrate"
 * With no rate data yet the server's basis ("estimate after N more segments") is shown instead of a number.
 */
export function fmtRuntime(rt, opts = {}) {
  if (!rt || !rt.state) return html``;
  const c = !!opts.compact, about = c ? "" : "about ";
  // the tooltip keeps PROCESSING time apart from AUDIO length (both are shown on the same rows)
  const tip = `Processing time, not audio length. ${rt.state === "finished" ? "Actual" : "Estimate"}${rt.basis ? ": " + rt.basis : ""}.`;
  const wrap = (cls, text) => html`<span class="rt rt-${cls}" title="${tip}">${text}</span>`;
  const basis = rt.basis || "";
  const none = () => (basis.startsWith("estimate after") ? wrap("none", c ? "no estimate yet" : basis) : html``);
  if (rt.state === "finished") return rt.processing_s === null || rt.processing_s === undefined ? html`` : wrap("finished", `processed in ${fmtSpan(rt.processing_s)}`);
  if (rt.state === "running") {
    if (rt.remaining_s === null || rt.remaining_s === undefined) return none();
    return wrap("running", `${about}${fmtSpan(rt.remaining_s)} left` + (!c && rt.finish_at ? ` · done about ${fmtClock(rt.finish_at)}` : ""));
  }
  if (rt.state === "queued") {
    const paused = basis.startsWith("paused");
    if (rt.total_est_s === null || rt.total_est_s === undefined) return paused ? wrap("paused", "paused") : none();
    if (paused) return wrap("paused", c ? `paused · ${fmtSpan(rt.total_est_s)}` : `paused · about ${fmtSpan(rt.total_est_s)} processing once resumed`);
    return wrap("queued", `${about}${fmtSpan(rt.total_est_s)} processing` + (!c && rt.starts_at ? ` · starts about ${fmtClock(rt.starts_at)}` : ""));
  }
  if (rt.total_est_s === null || rt.total_est_s === undefined) return none();
  return wrap("idle", `${about}${fmtSpan(rt.total_est_s)} to narrate`);
}
