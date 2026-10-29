"""Minimal real-engine probe for the "Tokenizer not loaded" investigation: load the CustomVoice and Base models in the same process (as the
smoke test does), then synthesize one short line with the CustomVoice model.

    HF_HOME=<cache> <venv>/bin/python tools/e2e/tts_probe.py
Prints OK or the exception. Run it under different environment variables to bisect what the launcher changes.
"""
import os
import sys

from mlx_audio.tts.utils import load_model

cv = load_model("mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16")
print("cv tokenizer after load:", getattr(cv, "tokenizer", "<none>") is not None, flush=True)
base = load_model("mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16")
print("cv tokenizer after loading Base:", getattr(cv, "tokenizer", "<none>") is not None, flush=True)
try:
    n = sum(1 for _ in cv.generate_custom_voice(text="The harbour lights trembled on the black water.", speaker="ryan", language="English",
                                                max_tokens=200, verbose=False))
    print("OK", n, "chunk(s); HF_HOME =", os.environ.get("HF_HOME"), "TP =", os.environ.get("TOKENIZERS_PARALLELISM"))
except Exception as exc:
    print("FAIL", type(exc).__name__, exc, "| HF_HOME =", os.environ.get("HF_HOME"), "TP =", os.environ.get("TOKENIZERS_PARALLELISM"))
    sys.exit(1)
