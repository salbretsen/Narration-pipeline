# Models, licences and your responsibilities

| Component | Default identifier | Licence (verify before commercial use) |
|---|---|---|
| Qwen3-TTS CustomVoice 1.7B | `mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16` | Apache-2.0 (Qwen). Check the model card. |
| Qwen3-TTS Base 1.7B (cloning) | `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16` | Apache-2.0 (Qwen). Check the model card. |
| Qwen3-TTS VoiceDesign 1.7B | `mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16` | Apache-2.0 (Qwen). Check the model card. |
| Whisper large-v3-turbo (MLX) | `mlx-community/whisper-large-v3-turbo` | MIT (OpenAI) |
| mlx-audio | PyPI `mlx-audio==0.6.3` | MIT |
| MLX, mlx-whisper | PyPI | MIT |
| FFmpeg | Homebrew `ffmpeg` | LGPL/GPL, depending on the build |

The exact package versions installed are recorded in `app/VERSIONS.lock`. Each generated take
also records its engine, model ID, Hugging Face snapshot revision and package versions, in the
database and in a `.wav.json` sidecar.

## Before commercial release you must

* Re-check each model card's licence and use restrictions. They can change between releases.
* Confirm that you own the manuscript rights, including audio rights.
* Hold explicit, preferably written, permission from every person whose voice you clone. The
  studio records your attestation and the reference clip's SHA-256 hash, but it cannot verify
  consent for you.
* Check your distributor's rules on AI or synthetic narration, disclosure and technical
  specifications. ACX/Audible, for example, sets limits on loudness, peak level, noise floor,
  room tone and file format, and some distributors restrict synthetic voices.
* Listen to the book. Automatic QA cannot judge pronunciation or performance.
