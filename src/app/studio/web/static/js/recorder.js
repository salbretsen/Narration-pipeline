// Microphone capture for the reference-voice lab.
// Records raw, unprocessed mono audio (echo cancellation, noise suppression and auto-gain are OFF
// on purpose: a voice clone should learn your real voice, not a processed one) and encodes 16-bit WAV.

export function micSupport() {
  const secure = window.isSecureContext === true;
  const api = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) && typeof AudioWorkletNode !== "undefined";
  return { secure, api, ok: secure && api };
}

export async function listMics() {
  const devs = await navigator.mediaDevices.enumerateDevices();
  return devs.filter((d) => d.kind === "audioinput").map((d, i) => ({ id: d.deviceId, label: d.label || `Microphone ${i + 1}` }));
}

export const dbfs = (x) => 20 * Math.log10(Math.max(x, 1e-9));
export const BLUETOOTH_RE = /bluetooth|airpods|hands-?free|headset|buds/i;

export class Mic {
  constructor() {
    this.ctx = null; this.stream = null; this.node = null; this.gain = null;
    this.chunks = []; this.recording = false; this.peak = 0; this.rms = 0; this.sampleRate = 0; this.label = ""; this.frames = 0;
  }

  async open(deviceId) {
    await this.close();
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { deviceId: deviceId ? { exact: deviceId } : undefined, channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    });
    this.label = (this.stream.getAudioTracks()[0] || {}).label || "";
    this.ctx = new AudioContext({ latencyHint: "interactive" });
    await this.ctx.audioWorklet.addModule("/static/js/recorder-worklet.js");
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "capture", { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
    this.node.port.onmessage = (e) => this._frame(e.data);
    this.gain = this.ctx.createGain();
    this.gain.gain.value = 0; // keep the graph running without playing the microphone back
    src.connect(this.node); this.node.connect(this.gain); this.gain.connect(this.ctx.destination);
    if (this.ctx.state === "suspended") await this.ctx.resume();
    this.sampleRate = this.ctx.sampleRate;
    return this.label;
  }

  _frame(f) {
    let pk = 0, sum = 0;
    for (let i = 0; i < f.length; i++) { const a = Math.abs(f[i]); if (a > pk) pk = a; sum += f[i] * f[i]; }
    this.peak = Math.max(pk, this.peak * 0.92);  // decaying peak for the meter
    this.rms = Math.sqrt(sum / f.length);
    this.frames++;
    if (this.recording) this.chunks.push(f);
  }

  get isOpen() { return !!(this.ctx && this.stream && this.stream.active); }

  start() { this.chunks = []; this.recording = true; this.t0 = performance.now(); }
  get elapsed() { return this.recording ? (performance.now() - this.t0) / 1000 : 0; }

  stop() {
    this.recording = false;
    const n = this.chunks.reduce((a, c) => a + c.length, 0);
    const out = new Float32Array(n);
    let o = 0;
    for (const c of this.chunks) { out.set(c, o); o += c.length; }
    this.chunks = [];
    return { samples: out, sampleRate: this.sampleRate };
  }

  async close() {
    this.recording = false;
    try { if (this.node) this.node.port.onmessage = null; } catch { /* ignore */ }
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (this.ctx && this.ctx.state !== "closed") { try { await this.ctx.close(); } catch { /* ignore */ } }
    this.ctx = this.stream = this.node = this.gain = null;
    this.peak = 0; this.rms = 0;
  }
}

export function encodeWav(samples, sampleRate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sampleRate, true); v.setUint32(28, sampleRate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  w(36, "data"); v.setUint32(40, samples.length * 2, true);
  let o = 44;
  for (let i = 0; i < samples.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

/** Noise floor (10th percentile of 20 ms frame levels) and peak of a recording, in dBFS. */
export function roomMetrics(samples, sampleRate) {
  const n = Math.max(1, Math.round(sampleRate * 0.02));
  const levels = [];
  let peak = 0;
  for (let i = 0; i + n <= samples.length; i += n) {
    let sum = 0;
    for (let j = i; j < i + n; j++) { const a = samples[j]; sum += a * a; const b = Math.abs(a); if (b > peak) peak = b; }
    levels.push(dbfs(Math.sqrt(sum / n)));
  }
  levels.sort((a, b) => a - b);
  const floor = levels.length ? levels[Math.floor(levels.length * 0.1)] : -90;
  return { noiseFloor: Math.max(-90, floor), peak: dbfs(peak), seconds: samples.length / sampleRate };
}

/** Down-sampled peak envelope for drawing a waveform. */
export function envelope(samples, buckets = 400) {
  const out = new Float32Array(buckets);
  const step = Math.max(1, Math.floor(samples.length / buckets));
  for (let b = 0; b < buckets; b++) {
    let pk = 0;
    for (let i = b * step; i < Math.min(samples.length, (b + 1) * step); i++) pk = Math.max(pk, Math.abs(samples[i]));
    out[b] = pk;
  }
  return out;
}
