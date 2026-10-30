"""Reference-voice lab: scripts, quality scoring, draft recordings, Whisper check / audition jobs,
narrator creation from a recording, HTTPS certificate generation. Mock engine (not real speech)."""
from __future__ import annotations

import json
import ssl

import pytest

from conftest import make_speechlike_wav, make_wav_bytes, needs_ffmpeg
from studio import reference as R
from studio.db import row


def test_scripts_have_good_reference_length_and_variety():
    assert len(R.SCRIPTS) >= 8 and len({s["id"] for s in R.SCRIPTS}) == len(R.SCRIPTS)
    for s in R.SCRIPTS:
        assert 24 <= R.word_count(s["text"]) <= 62, s["id"]
        assert 9 <= R.estimated_seconds(s["text"]) <= 20, s["id"]  # fits the 8-15 s ideal (+ slack)
        assert s["tier"] in ("core", "range", "emotion") and s["purpose"] and s["title"]
    core = [s for s in R.SCRIPTS if s["tier"] == "core"]
    assert len(core) >= 5 and any("?" in s["text"] for s in core)  # dialogue/questions
    assert any(any(ch.isdigit() for ch in s["text"]) or "nineteen" in s["text"] for s in core)  # numbers
    assert {s["emotion"] for s in R.SCRIPTS if s["tier"] == "range"} >= {"warm", "tense", "joyful", "sad"}
    assert R.script_by_id("core-harbour") and not R.script_by_id("nope")


def _m(**kw):
    base = {"duration_s": 11.0, "peak_dbfs": -8.0, "clip_ratio": 0.0, "rms_dbfs": -22.0, "noise_floor_dbfs": -62.0,
            "longest_internal_silence_s": 0.4, "lead_silence_s": 0.5}
    base.update(kw)
    return base


def test_quality_report_verdicts_and_tips():
    assert R.quality_report(_m())["verdict"] == "excellent"
    assert R.quality_report(_m(duration_s=2.0))["verdict"] == "poor"
    assert R.quality_report(_m(duration_s=40.0))["verdict"] == "poor"
    assert R.quality_report(_m(peak_dbfs=0.0, clip_ratio=0.01))["verdict"] == "poor"
    noisy = R.quality_report(_m(noise_floor_dbfs=-35.0))
    assert noisy["verdict"] == "poor" and any(c["name"] == "Background noise" and c["status"] == "fail" and c["tip"] for c in noisy["checks"])
    assert R.quality_report(_m(rms_dbfs=-50.0))["verdict"] == "poor"
    one = R.quality_report(_m(longest_internal_silence_s=2.5))
    assert one["verdict"] == "good"
    two = R.quality_report(_m(longest_internal_silence_s=2.5, duration_s=4.0))
    assert two["verdict"] == "usable"


@needs_ffmpeg
def test_recording_flow_check_audition_and_create(client, ctx, make_worker):
    wav = make_speechlike_wav(10.0)
    text = "The morning fog lay thick over the harbour, and the fishing boats rocked quietly at their moorings."
    r = client.post("/api/recordings", data={"script_id": "core-harbour", "ref_text": text}, files={"file": ("recording.wav", wav, "audio/wav")})
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["id"].startswith("rec_") and 9.0 < rec["duration_s"] < 11.0 and rec["quality"]["verdict"] in ("excellent", "good", "usable")
    assert rec["quality"]["checks"] and rec["metrics"]["noise_floor_dbfs"] <= 0
    assert client.get("/api/recordings").json()["recordings"][0]["id"] == rec["id"]
    # the audio is streamed with range support
    rid = rec["id"]
    full = client.get(f"/media/recording/{rid}")
    assert full.status_code == 200 and full.headers["content-type"] == "audio/wav"
    assert client.get(f"/media/recording/{rid}", headers={"Range": "bytes=0-43"}).status_code == 206
    assert client.get("/media/recording/rec_deadbeef").status_code == 404
    assert client.get("/media/recording/..%2F..%2Fetc").status_code in (404, 422)
    # Whisper check job: matches / does not match the transcript (mock ASR reads a sidecar transcript)
    side = ctx.data_dir / "recordings" / rid / "reference-24k.wav.json"
    side.write_text(json.dumps({"meta": {"mock_transcript": text}}))
    w = make_worker()
    j = client.post(f"/api/recordings/{rid}/job", json={"mode": "check"}).json()
    w.run_until_idle()
    res = client.get(f"/api/jobs/{j['id']}").json()
    assert res["status"] == "completed" and res["result"]["matches"] is True and res["result"]["wer"] == 0
    side.write_text(json.dumps({"meta": {"mock_transcript": "something completely different was said here today"}}))
    j = client.post(f"/api/recordings/{rid}/job", json={"mode": "check"}).json()
    w.run_until_idle()
    assert client.get(f"/api/jobs/{j['id']}").json()["result"]["matches"] is False
    # editing the transcript persists
    assert client.patch(f"/api/recordings/{rid}", json={"ref_text": "Edited exact words said"}).json()["ref_text"] == "Edited exact words said"
    # audition: Base-model synthesis from this take, honestly labelled as mock here
    j = client.post(f"/api/recordings/{rid}/job", json={"mode": "audition", "text": "A short audition passage to compare takes."}).json()
    w.run_until_idle()
    res = client.get(f"/api/jobs/{j['id']}").json()
    assert res["status"] == "completed" and res["result"]["mode"] == "audition" and "not speech" in res["result"]["note"]
    assert client.get(f"/media/preview/{j['id']}").headers["content-type"] == "audio/wav"
    # creating a narrator needs the ownership confirmation
    body = {"recording_id": rid, "name": "My recorded voice", "rights_holder": "Jane Author", "owner_attest": False}
    assert client.post("/api/voices/from-recording", json=body).status_code == 422
    v = client.post("/api/voices/from-recording", json={**body, "owner_attest": True})
    assert v.status_code == 200, v.text
    v = v.json()
    assert v["kind"] == "clone" and v["version"]["model_id"].endswith("Base-bf16") and not v["version"]["speaker"]
    assert v["version"]["ref_text"] == "Edited exact words said" and "reference lab" in v["version"]["consent"]["statement"]
    # draft removal
    assert client.delete(f"/api/recordings/{rid}").status_code == 200
    assert client.get("/api/recordings").json()["recordings"] == []


@needs_ffmpeg
def test_bad_recordings_are_rejected_or_blocked(client):
    ok_text = "These are the words that were said in the recording"
    # too short / silent / garbage / wrong type / oversize
    r = client.post("/api/recordings", data={"ref_text": ok_text}, files={"file": ("r.wav", make_wav_bytes(1.0), "audio/wav")})
    assert r.status_code == 422 and r.json()["details"]["quality"]["verdict"] == "poor"
    assert client.post("/api/recordings", data={"ref_text": ok_text}, files={"file": ("r.wav", make_wav_bytes(8, silent=True), "audio/wav")}).status_code == 422
    assert client.post("/api/recordings", data={"ref_text": ok_text}, files={"file": ("r.wav", b"RIFF0000WAVEnope" + b"\0" * 80, "audio/wav")}).status_code == 422
    assert client.post("/api/recordings", data={"ref_text": ok_text}, files={"file": ("r.exe", make_wav_bytes(8), "application/octet-stream")}).status_code == 422
    assert client.post("/api/recordings", data={"ref_text": "no"}, files={"file": ("r.wav", make_wav_bytes(8), "audio/wav")}).status_code == 422
    # a clipped take is saved for inspection but cannot become a narrator
    r = client.post("/api/recordings", data={"ref_text": ok_text}, files={"file": ("r.wav", make_speechlike_wav(8, amp=4.0), "audio/wav")})
    assert r.status_code == 200 and r.json()["quality"]["verdict"] == "poor"
    assert any(c["name"] == "Clipping" and c["status"] == "fail" for c in r.json()["quality"]["checks"])
    v = client.post("/api/voices/from-recording", json={"recording_id": r.json()["id"], "name": "x", "rights_holder": "Me Myself", "owner_attest": True})
    assert v.status_code == 422 and "poor" in v.json()["error"]
    assert client.get("/api/recordings/rec_nonexistent").status_code in (404, 405)
    assert client.post("/api/recordings/rec_deadbeef/job", json={"mode": "check"}).status_code == 404
    assert client.post(f"/api/recordings/{r.json()['id']}/job", json={"mode": "nope"}).status_code == 422


def test_reference_scripts_endpoint(client):
    d = client.get("/api/reference-scripts").json()
    assert len(d["scripts"]) == len(R.SCRIPTS) and d["scripts"][0]["est_seconds"] > 5 and d["audition_text"]


def test_lab_assets_served_and_mic_policy_allows_self(client):
    for p in ("/static/js/recorder.js", "/static/js/recorder-worklet.js", "/static/js/views/voicelab.js"):
        assert client.get(p).status_code == 200
    assert "microphone=(self)" in client.get("/api/overview").headers["permissions-policy"]


def test_self_signed_certificate_for_lan_use(ctx):
    from cryptography import x509

    from studio.tls import ensure_certificate, fingerprint_sha256

    s = ctx.settings().model_copy(deep=True)
    s.server.allowed_hosts = ["studio.example.lan"]
    ctx.save_settings(s)
    cert, key = ensure_certificate(ctx)
    c = x509.load_pem_x509_certificate(cert.read_bytes())
    san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "studio.example.lan" in san.get_values_for_type(x509.DNSName) and "127.0.0.1" in {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)  # usable by a TLS server
    assert (key.stat().st_mode & 0o077) == 0 or __import__("os").name == "nt"  # private key not group/world readable
    assert ensure_certificate(ctx) == (cert, key) and fingerprint_sha256(cert).count(":") == 31  # reused, not regenerated
    before = cert.read_bytes()
    s.server.allowed_hosts = ["studio.example.lan", "another-name.lan"]
    ctx.save_settings(s)
    ensure_certificate(ctx)
    assert cert.read_bytes() != before  # a new host name triggers a new certificate


def test_https_settings_validation():
    from studio.config import Settings

    assert Settings.model_validate({"server": {"https_enabled": True, "https_port": 8443}}).server.https_port == 8443
    with pytest.raises(Exception):
        Settings.model_validate({"server": {"https_enabled": True, "port": 8443, "https_port": 8443}})


@needs_ffmpeg
def test_noise_floor_estimates_and_continuous_reads_are_not_called_noisy(tmp_path):
    quiet = tmp_path / "quiet.wav"
    quiet.write_bytes(make_speechlike_wav(10.0, noise_db=-66.0))
    m = R.analyze_recording(quiet)["metrics"]
    assert m["noise_floor_dbfs"] < -55 and R.quality_report(m)["verdict"] in ("excellent", "good")
    noisy = tmp_path / "noisy.wav"
    noisy.write_bytes(make_speechlike_wav(10.0, noise_db=-33.0))
    mn = R.analyze_recording(noisy)["metrics"]
    q = R.quality_report(mn)
    assert mn["noise_floor_dbfs"] > -40 and any(c["name"] == "Background noise" and c["status"] == "fail" for c in q["checks"])
    cont = tmp_path / "continuous.wav"
    cont.write_bytes(make_wav_bytes(10.0))  # one unbroken tone: no quiet moment to measure
    qc = R.quality_report(R.analyze_recording(cont)["metrics"])
    bn = next(c for c in qc["checks"] if c["name"] == "Background noise")
    assert bn["status"] == "warn" and "No quiet moment" in bn["detail"]  # unknown, not a false 'too noisy' failure
    assert qc["verdict"] != "poor"
