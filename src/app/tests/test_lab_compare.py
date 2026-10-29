"""Multi-take comparison in the reference lab. Mock engine (not real speech)."""
from __future__ import annotations

from conftest import make_speechlike_wav, needs_ffmpeg
from studio import reference as R

TEXT = "The morning fog lay thick over the harbour, and the fishing boats rocked quietly at their moorings."


def _take(i, verdict, floor=-62.0, clip=0.0, warns=0):
    checks = [{"name": f"w{n}", "status": "warn", "detail": "", "tip": ""} for n in range(warns)]
    return {"id": i, "quality": {"verdict": verdict, "checks": checks}, "duration_s": 10.0,
            "metrics": {"peak_dbfs": -8.0, "clip_ratio": clip, "rms_dbfs": -22.0, "noise_floor_dbfs": floor}}


def test_compare_ranks_by_verdict_then_warnings_then_snr_and_skips_poor():
    r = R.compare_takes([_take("a", "usable", warns=2), _take("b", "excellent", floor=-55.0), _take("c", "excellent", floor=-65.0), _take("d", "poor")])
    ranks = {x["id"]: x["rank"] for x in r["rows"]}
    assert ranks == {"c": 1, "b": 2, "a": 3, "d": 4} and r["recommended_id"] == "c"
    assert [x["id"] for x in r["rows"] if x["recommended"]] == ["c"]
    assert R.compare_takes([_take("x", "poor"), _take("y", "poor")])["recommended_id"] is None


@needs_ffmpeg
def test_compare_endpoint(client):
    ids = []
    for _ in range(2):
        r = client.post("/api/recordings", data={"script_id": "core-harbour", "ref_text": TEXT}, files={"file": ("t.wav", make_speechlike_wav(10.0), "audio/wav")})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    c = client.get("/api/recordings/compare", params={"ids": ",".join(ids)})
    assert c.status_code == 200, c.text
    body = c.json()
    assert {x["id"] for x in body["rows"]} == set(ids) and body["recommended_id"] in ids
    assert all({"verdict", "duration_s", "peak_dbfs", "noise_floor_dbfs", "snr_db", "clip_ratio", "rank"} <= set(x) for x in body["rows"])
    assert client.get("/api/recordings/compare", params={"ids": ids[0]}).status_code == 422
    assert client.get("/api/recordings/compare", params={"ids": f"{ids[0]},rec_deadbeef"}).status_code == 404
    assert client.get("/api/recordings/compare", params={"ids": f"{ids[0]},../x"}).status_code == 404
