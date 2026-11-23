# Test results

Run date: 2026-11-23

## Local automated tests

Command: `pytest -q`

Result: **28 passed**.

Coverage includes:

- Bengali Unicode combining marks remain in complete orthographic words.
- Bengali spelling variants from ASR output align to the provided script.
- Three intentional occurrences of the same script sentence remain separate.
- Later, cleaner repeated takes do not steal earlier intentional script positions.
- Best-take candidates prioritize complete sentences over incomplete attempts.
- Missing lines, extra speech and review states.
- Faster-whisper language-hint forwarding.
- Multi-user owner isolation and forbidden cross-user reads/deletes.
- Production API rejects requests when authentication is not configured.
- SQLite job claim is exclusive.
- Background job pipeline completes, records status and produces actual FFmpeg audio/signal metrics (transcriber stubbed only for this orchestration test).
- Waveform generation, timeline keep ranges, split markers, MP3/WAV export, noise reduction settings and download.
- Empty timeline cannot accidentally fall back to exporting the full source.
- Segment replacement changes a working copy while preserving the original.
- Replacement undo/redo.
- Per-take RMS, peak, noise-floor indicator and clipping measurements.

Additional checks:

- `python -m compileall -q backend/app tests`: passed.
- `node --check frontend/app.js`: passed.
- FastAPI server startup: passed.
- `GET /api/health`: HTTP 200.
- `GET /`: HTTP 200; frontend HTML served.
- 10-minute, 16 kHz mono WAV: 1,800-point waveform envelope generated in 0.41 s; a 10-second mid-recording segment exported with denoising/normalization in 0.70 s and probed at 10.19 s. This checks long-file FFmpeg/waveform behavior, not long-form Whisper inference.
- API integration: project create/list/read/delete, script save/import, WAV upload/probe, waveform, timeline edit, MP3/WAV export/download, segment replacement, undo/redo and source preservation passed.

## Actual speech-model inference

A separate network-enabled Linux sandbox had `faster-whisper` available. `large-v3-turbo` was loaded on CPU with `compute_type='int8'`. Bengali and English test speech were generated as audio, then passed through actual `WhisperModel.transcribe` with `word_timestamps=True` and VAD enabled.

- Bengali: forced language `bn`, detected `bn` with probability 1.0. The model returned Bengali-script text and per-word timestamps/probabilities, but several orthographic errors. The exact returned transcript fixture was passed through this project's script matcher: 2/2 sentences matched, weighted token coverage 80.9%, zero missing sentences and zero extra-speech groups.
- English: forced language `en`, detected `en` with probability 1.0. The two-sentence English sample was transcribed correctly with word-level timestamps.

These are controlled generated-speech tests, not a representative evaluation on varied human microphones, dialects, noise conditions or code-switching. Bengali recognition must remain reviewable.

## Environment / deployment checks not completed

- The local build container had FFmpeg but no installed Whisper package or cached model. Actual ASR was verified separately as above.
- Docker CLI was unavailable, so a container image build could not be run.
- No local Chromium executable was installed, so browser automation and physical Android testing were not run. JavaScript syntax, HTML serving and API behavior were checked.
- No live Render deployment was created. The available Render service is attached to the unrelated `creovanta-voice-cloning` repository; it was not changed. The GitHub integration did not expose a create-repository action. `render.yaml` is ready as a blueprint but requires a new repository connection, a paid Standard service, persistent disk and a configured access token.
