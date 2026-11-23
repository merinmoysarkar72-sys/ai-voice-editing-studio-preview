# AI Voice Editing Studio

A script-aware voiceover editor for Bengali and English. It combines real Whisper transcription, script-to-audio alignment, conservative take ranking, non-destructive timeline edits, segment replacement, FFmpeg processing, and MP3/WAV export.

## Implemented workflow

1. Create a project, paste a script or import TXT/PDF/DOCX, then upload or record audio.
2. Choose Auto, Bengali, or English speech language. `large-v3-turbo` is the default multilingual model; CPU `int8` inference is supported.
3. The backend transcribes with faster-whisper, VAD, word timestamps and recognition probabilities. If unavailable, it tries OpenAI Whisper, then an installed `whisper-cli` with `WHISPER_CPP_MODEL_PATH`.
4. Script matching preserves script order and intentional repeated lines. Bengali Unicode combining marks remain within words, and fuzzy token alignment tolerates small ASR spelling variations. Missing lines, unmatched speech, long pauses, possible stutters and silences are reported.
5. Alternative takes are ranked using script coverage, completeness, ASR word probability, speech-rate plausibility and real decoded-signal measurements (RMS, peak, low-level noise indicator and clipping ratio). The score is an objective heuristic, not a human pronunciation or naturalness judgement. Repeated script lines already aligned in sequence are not reassigned to a later take.
6. Review each sentence and choose among alternative takes. Low-confidence content is not automatically removed or synthesized.
7. Drag over the waveform to select a range. Trim to selection, delete selection, split at playhead, replace the selected segment with another take, undo/redo timeline edits, and undo/redo segment replacement. The uploaded original remains immutable. A separate edited working preview is available.
8. Export approved script ranges to MP3 or WAV with bitrate, sample rate, mono/stereo, pause handling, FFmpeg denoising and loudness normalization.

## Architecture

- **Frontend:** responsive single-page studio, waveform canvas, recording, script editor, sentence review, timeline controls, original/edited preview, PWA shell.
- **Backend:** FastAPI, FFmpeg/FFprobe, faster-whisper, optional OpenAI Whisper and whisper.cpp fallbacks.
- **Storage:** private project directories and JSON metadata under `AVES_DATA_DIR`; SQLite WAL job registry at `AVES_DATA_DIR/jobs.sqlite3`.
- **Jobs:** bounded worker pool, persistent job status, atomic job claims, queued/running job recovery on backend restart.
- **Security:** optional single-user token or `AVES_USERS_JSON` token-to-user mapping; HTTP-only session cookie; project ownership checks; production mode refuses API access if authentication is not configured.

The worker pool and SQLite registry are intended for one persistent backend instance. For horizontal scaling, replace them with a shared queue (Redis/RQ or Celery) and shared object storage. Do not run multiple replicas against separate ephemeral disks.

## Requirements

- Python 3.11 recommended.
- FFmpeg and FFprobe on `PATH`.
- 2 GB RAM minimum for `large-v3-turbo` CPU-int8 is a practical starting point; more memory and CPU improve long-recording throughput.
- Persistent storage for original recordings, working audio, job records and cached model files.
- Internet access on first use to download the Whisper model. After caching, inference can run offline.

## Run locally

From the project root:

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r backend/requirements.txt
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`.

For a smaller development model, set `WHISPER_MODEL=small`. Bengali transcription quality is generally weaker with the smaller model. For Bengali voiceovers, choose **Bengali** in the language selector if auto-detection chooses the wrong language.

## Docker

```bash
cp .env.example .env
# Set AVES_ACCESS_TOKEN in .env before exposing the service publicly.
docker compose up --build
```

The Compose file mounts a persistent named volume at `/data`. It runs a single backend process and persists project files and model cache.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `AVES_ENV` | `development` | Set `production` on public deployments; auth is mandatory in production. |
| `AVES_ACCESS_TOKEN` | empty | Single-user access token. |
| `AVES_USERS_JSON` | `{}` | JSON mapping of high-entropy access tokens to stable user IDs for multi-user isolation. |
| `AVES_ALLOWED_ORIGINS` | empty | Comma-separated CORS origins for split frontend/backend hosting. |
| `AVES_DATA_DIR` | `./data` | Persistent project, job and model storage. |
| `AVES_MAX_AUDIO_MB` | `500` | Maximum original upload size. |
| `AVES_MAX_SCRIPT_MB` | `20` | Maximum imported script file size. |
| `WHISPER_MODEL` | `large-v3-turbo` | faster-whisper model ID; can be changed to `small`, `medium`, etc. |
| `WHISPER_DEVICE` | `cpu` | CPU by default. |
| `WHISPER_COMPUTE_TYPE` | `int8` | Efficient CPU inference. |
| `WHISPER_CPP_MODEL_PATH` | empty | Optional GGML model file for whisper.cpp fallback. |

For `AVES_USERS_JSON`, use tokens generated from a cryptographically secure random source. Never put tokens or API secrets in frontend code. The session cookie is HTTP-only. Existing projects without an `owner_id` are treated as local-owner data and need a deliberate migration before multi-user mode is enabled.

## API overview

- `GET /api/health`
- `POST /api/auth`
- `GET /api/projects`, `POST /api/projects`
- `GET /api/projects/{id}`, `DELETE /api/projects/{id}`
- `PUT /api/projects/{id}/script`, `POST /api/projects/{id}/script-file`
- `POST /api/projects/{id}/audio`, `GET /api/projects/{id}/audio`, `DELETE /api/projects/{id}/audio`
- `POST /api/projects/{id}/analyze`, `GET /api/jobs/{job_id}`, `GET /api/projects/{id}/analysis`
- `PATCH /api/projects/{id}/sentences/{index}`
- `GET /api/projects/{id}/waveform`, `PUT/DELETE /api/projects/{id}/timeline`
- `POST /api/projects/{id}/replace-segment`
- `GET /api/projects/{id}/working-audio`
- `POST /api/projects/{id}/working-audio/undo`, `POST /api/projects/{id}/working-audio/redo`
- `POST /api/projects/{id}/export`, `GET /api/projects/{id}/export/download`

Interactive API documentation: `/docs`.

## Optional speech-engine fallbacks

The runtime engine order is:

1. faster-whisper (preferred, native word timestamps and CPU int8)
2. OpenAI Whisper (`pip install -r backend/requirements-fallback.txt`; larger PyTorch dependency, segment timestamps with estimated word boundaries)
3. whisper.cpp (`bash scripts/install-whisper-cpp.sh`, then set `WHISPER_CPP_MODEL_PATH`; word boundaries are estimated)

The backend never fabricates a transcript. Fallback engines clearly mark estimated word timestamps in the analysis payload.

## Deployment

This is a stateful Python/FFmpeg service, not a static-only frontend. `render.yaml` defines a separate Docker web service with a persistent 10 GB disk and a paid Standard plan sized for the default model. It is a deployment blueprint only; it has not been provisioned. Before deployment, connect this repository to Render and set `AVES_ACCESS_TOKEN` as a secret. Existing unrelated Render services and Vercel projects were not modified.

The connected GitHub integration in this session exposed the existing `creovanta-voice-cloning` repository but no repository-creation action. That repository belongs to another project and was not changed. The existing Render service is linked to it, so it was not repointed. No live URL is claimed.

## Tests

```bash
pip install pytest httpx
pytest -q
node --check frontend/app.js
python -m compileall -q backend/app tests
```

See [TEST_RESULTS.md](TEST_RESULTS.md) for executed tests and environment limitations. Security and data deletion behavior are documented in [SECURITY.md](SECURITY.md). CI configuration is in `.github/workflows/ci.yml`.
