from __future__ import annotations
import shutil
import os
import secrets
import uuid
import json as json_module
from concurrent.futures import Future
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .config import APP_NAME, MAX_AUDIO_MB, MAX_SCRIPT_MB, ALLOWED_AUDIO, ALLOWED_SCRIPT, BASE_DIR
from .schemas import ProjectCreate, ScriptUpdate, SentenceDecision, ExportRequest, AnalysisRequest
from .storage import create_project, read_project, write_project, list_projects, delete_project, project_dir, safe_name
from .matching import build_report
from .audio import probe_audio, transcribe, detect_silences, analyze_audio_quality, export_audio, waveform_peaks, replace_audio_segment
from . import jobs

@asynccontextmanager
async def lifespan(app):
    recover_analysis_jobs()
    yield

app = FastAPI(title=APP_NAME, version="0.2.0", description="Script-aware voice editing and audio restoration API", lifespan=lifespan)
cors_origins = [value.strip() for value in os.getenv("AVES_ALLOWED_ORIGINS", "").split(",") if value.strip()]
if cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Authorization", "Content-Type"])

def _user_tokens():
    try:
        value = json_module.loads(os.getenv("AVES_USERS_JSON", "{}"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}

@app.middleware("http")
async def optional_access_guard(request, call_next):
    if not request.url.path.startswith("/api/") or request.url.path in {"/api/health", "/api/auth"} or request.method == "OPTIONS":
        request.state.user_id = "local"
        return await call_next(request)
    users = _user_tokens()
    expected = os.getenv("AVES_ACCESS_TOKEN", "").strip()
    if os.getenv("AVES_ENV", "").lower() == "production" and not users and not expected:
        return JSONResponse(status_code=503, content={"detail": "Production authentication is not configured. Set AVES_ACCESS_TOKEN or AVES_USERS_JSON."})
    provided = request.headers.get("authorization", "")
    token = provided[7:].strip() if provided.lower().startswith("bearer ") else request.cookies.get("aves_access", "")
    user_id = None
    if users:
        user_id = users.get(token)
    elif expected:
        user_id = "default" if token and secrets.compare_digest(token, expected) else None
    else:
        user_id = "local"
    if user_id is None:
        return JSONResponse(status_code=401, content={"detail": "Authentication required."})
    request.state.user_id = str(user_id)
    return await call_next(request)



def update(project: dict):
    project["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_project(project)

@app.post("/api/auth")
def create_access_session(payload: dict, response: Response, request: Request):
    supplied = str(payload.get("token", "")).strip()
    users = _user_tokens()
    expected = os.getenv("AVES_ACCESS_TOKEN", "").strip()
    if os.getenv("AVES_ENV", "").lower() == "production" and not users and not expected:
        raise HTTPException(503, "Production authentication is not configured.")
    if users:
        user_id = users.get(supplied)
    else:
        user_id = "default" if expected and supplied and secrets.compare_digest(supplied, expected) else ("local" if not expected else None)
    if user_id is None:
        raise HTTPException(401, "Invalid access token.")
    allowed_origins = [value.strip() for value in os.getenv("AVES_ALLOWED_ORIGINS", "").split(",") if value.strip()]
    origin = request.headers.get("origin", "")
    cross_origin = bool(origin and origin in allowed_origins)
    is_secure = request.url.scheme == "https"
    if supplied:
        response.set_cookie(key="aves_access", value=supplied, httponly=True, secure=is_secure,
                            samesite="none" if cross_origin and is_secure else "lax", path="/api", max_age=31536000)
    return {"authenticated": True, "access_protected": bool(users or os.getenv("AVES_ACCESS_TOKEN", "").strip()), "user_id": str(user_id)}


def user_project(project_id: str, request: Request):
    try:
        project = read_project(project_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    owner = project.get("owner_id", "local")
    if owner != getattr(request.state, "user_id", "local"):
        raise HTTPException(404, "Project not found.")
    return project

@app.get("/api/health")
def health():
    import importlib.util
    import shutil as shutil_module
    fw = importlib.util.find_spec("faster_whisper") is not None
    ow = importlib.util.find_spec("whisper") is not None
    cpp = bool(shutil_module.which("whisper-cli") and os.path.isfile(os.getenv("WHISPER_CPP_MODEL_PATH", "")))
    return {"ok": True, "app": APP_NAME, "engine": "faster-whisper, OpenAI Whisper, or whisper.cpp fallback",
            "faster_whisper_installed": fw, "openai_whisper_installed": ow, "whisper_cpp_installed": cpp,
            "engine_installed": fw or ow or cpp, "ffmpeg_available": shutil_module.which("ffmpeg") is not None,
            "multi_user_auth": bool(_user_tokens()), "access_protected": bool(_user_tokens() or os.getenv("AVES_ACCESS_TOKEN", "").strip())}

@app.get("/api/projects")
def projects(request: Request):
    return [p for p in list_projects() if p.get("owner_id", "local") == request.state.user_id]

@app.post("/api/projects")
def new_project(payload: ProjectCreate, request: Request):
    if not payload.name.strip():
        raise HTTPException(400, "Project name is required.")
    project = create_project(payload.name, payload.script_text)
    project["owner_id"] = request.state.user_id
    update(project)
    return project

@app.get("/api/projects/{project_id}")
def get_project(project_id: str, request: Request):
    try:
        return user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")

@app.delete("/api/projects/{project_id}")
def remove_project(project_id: str, request: Request):
    try:
        project = user_project(project_id, request)
        if project.get("status") in {"queued", "analyzing"}: raise HTTPException(409, "Wait for the current analysis job to finish before deleting this project.")
        jobs.delete_project(project_id, request.state.user_id)
        delete_project(project_id)
        return {"deleted": True}
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")

@app.put("/api/projects/{project_id}/script")
def save_script(project_id: str, payload: ScriptUpdate, request: Request):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    project["script_text"] = payload.script_text
    project["analysis"] = None
    project.pop("last_export", None)
    project["status"] = "ready" if project.get("audio_filename") else "draft"
    update(project)
    return {"saved": True, "characters": len(payload.script_text)}

@app.post("/api/projects/{project_id}/script-file")
async def upload_script(project_id: str, request: Request, file: UploadFile = File(...)):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_SCRIPT:
        raise HTTPException(415, "Use TXT, PDF or DOCX for the script.")
    raw = await file.read(MAX_SCRIPT_MB * 1024 * 1024 + 1)
    if len(raw) > MAX_SCRIPT_MB * 1024 * 1024:
        raise HTTPException(413, f"Script file exceeds {MAX_SCRIPT_MB} MB.")
    try:
        if ext == ".txt":
            text = raw.decode("utf-8-sig")
        elif ext == ".pdf":
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            from docx import Document
            import io
            doc = Document(io.BytesIO(raw))
            text = "\n".join(p.text for p in doc.paragraphs)
    except Exception as exc:
        raise HTTPException(422, f"Could not read this script file: {exc}")
    if not text.strip():
        raise HTTPException(422, "No readable text was found in the script.")
    project["script_text"] = text
    project["script_filename"] = Path(file.filename or f"script{ext}").name
    project["analysis"] = None
    project.pop("last_export", None)
    project["status"] = "ready" if project.get("audio_filename") else "draft"
    update(project)
    return {"saved": True, "filename": project["script_filename"], "characters": len(text), "script_text": text}

@app.post("/api/projects/{project_id}/audio")
async def upload_audio(project_id: str, request: Request, file: UploadFile = File(...)):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    original = Path(file.filename or "recording").name
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_AUDIO:
        raise HTTPException(415, "Unsupported audio format. Use MP3, WAV, M4A, AAC, OGG or WebM.")
    folder = project_dir(project_id)
    # A replacement is stored as a new immutable source file. Earlier uploads remain
    # in the project folder so replacing a take never destroys the previous original.
    from uuid import uuid4
    target = folder / f"original-audio-{uuid4().hex[:10]}{ext}"
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_AUDIO_MB * 1024 * 1024:
                    output.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(413, f"Audio exceeds {MAX_AUDIO_MB} MB.")
                output.write(chunk)
        details = probe_audio(target)
    except HTTPException:
        raise
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(422, f"Audio could not be decoded. Check the file and codec. ({exc})")
    project["audio_filename"] = target.name
    project["audio_original_name"] = original
    project["audio_duration"] = details["duration"]
    project["audio_size"] = details["size"]
    project["audio_details"] = details
    project["analysis"] = None
    project.pop("working_audio_filename", None)
    project.pop("segment_replacements", None)
    project.pop("working_audio_history", None)
    project.pop("working_audio_redo", None)
    project.pop("timeline_ranges", None)
    project.pop("timeline_splits", None)
    if project.get("analysis") is not None:
        project["analysis"].pop("timeline_removed_segments", None)
        project["analysis"].pop("timeline_removed_seconds", None)
        project["analysis"].pop("split_markers", None)
    project.pop("last_export", None)
    project["status"] = "ready" if project.get("script_text", "").strip() else "draft"
    update(project)
    return {"uploaded": True, "filename": original, **details}

@app.delete("/api/projects/{project_id}/audio")
def detach_audio(project_id: str, request: Request):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    if project.get("status") in {"queued", "analyzing"}: raise HTTPException(409, "Wait for the current analysis job to finish before detaching the recording.")
    # Keep the uploaded file on disk as an archived source; only detach it from the
    # active editing session. Project deletion is the explicit destructive action.
    project["audio_filename"] = None
    project["audio_original_name"] = None
    project["audio_duration"] = None
    project["audio_size"] = None
    project.pop("working_audio_filename", None)
    project.pop("segment_replacements", None)
    project.pop("working_audio_history", None)
    project.pop("working_audio_redo", None)
    project.pop("timeline_ranges", None)
    project.pop("timeline_splits", None)
    project["analysis"] = None
    project.pop("last_export", None)
    project["status"] = "draft"
    update(project)
    return {"detached": True, "source_preserved": True}

def _removed_ranges(keep_ranges: list[dict], duration: float):
    removed=[];cursor=0.0
    for item in keep_ranges:
        start,end=float(item["start"]),float(item["end"])
        if start>cursor: removed.append({"start":round(cursor,3),"end":round(start,3)})
        cursor=max(cursor,end)
    if cursor<duration: removed.append({"start":round(cursor,3),"end":round(duration,3)})
    return removed

@app.get("/api/projects/{project_id}/working-audio")
def preview_working_audio(project_id: str, request: Request):
    project = user_project(project_id, request)
    filename = project.get("working_audio_filename") or project.get("audio_filename")
    if not filename: raise HTTPException(404, "No active recording.")
    path = project_dir(project_id) / filename
    if not path.exists(): raise HTTPException(404, "Working audio file not found.")
    return FileResponse(path, filename="edited-preview.wav" if project.get("working_audio_filename") else (project.get("audio_original_name") or path.name))

@app.post("/api/projects/{project_id}/replace-segment")
async def replace_segment(project_id: str, request: Request, start: float = Form(...), end: float = Form(...),
                          take_start: float = Form(0.0), take_end: float | None = Form(None), file: UploadFile = File(...)):
    project = user_project(project_id, request)
    if project.get("status") in {"queued", "analyzing"}: raise HTTPException(409, "Wait for analysis to finish before replacing a segment.")
    if not project.get("audio_filename"): raise HTTPException(400, "Upload an original recording first.")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_AUDIO: raise HTTPException(415, "Unsupported replacement audio format.")
    folder = project_dir(project_id)
    replacement = folder / f"replacement-{uuid.uuid4().hex[:10]}{ext}"
    output = None
    size=0
    try:
        with replacement.open("wb") as out:
            while chunk := await file.read(1024*1024):
                size += len(chunk)
                if size > 200*1024*1024:
                    out.close(); replacement.unlink(missing_ok=True)
                    raise HTTPException(413, "Replacement take exceeds 200 MB.")
                out.write(chunk)
        current = folder / (project.get("working_audio_filename") or project["audio_filename"])
        output = folder / f"working-audio-{uuid.uuid4().hex[:10]}.wav"
        details = replace_audio_segment(current, replacement, output, start, end, take_start, take_end)
    except HTTPException:
        raise
    except Exception as exc:
        replacement.unlink(missing_ok=True)
        if output is not None: output.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not replace this segment: {exc}")
    project.setdefault("working_audio_history", []).append(project.get("working_audio_filename"))
    project["working_audio_redo"] = []
    project["working_audio_filename"] = output.name
    project.setdefault("segment_replacements", []).append({"replacement_file":replacement.name, **details})
    project.pop("last_export", None)
    update(project)
    return {"replaced":True,"working_audio":output.name,"replacement":details,"source_preserved":True}

@app.post("/api/projects/{project_id}/working-audio/undo")
def undo_working_audio(project_id: str, request: Request):
    project = user_project(project_id, request)
    history = project.get("working_audio_history", [])
    if not history: raise HTTPException(409, "No replacement edit to undo.")
    project.setdefault("working_audio_redo", []).append(project.get("working_audio_filename"))
    project["working_audio_filename"] = history.pop()
    project.pop("last_export", None); update(project)
    return {"filename":project.get("working_audio_filename"),"restored_original":not bool(project.get("working_audio_filename"))}

@app.post("/api/projects/{project_id}/working-audio/redo")
def redo_working_audio(project_id: str, request: Request):
    project = user_project(project_id, request)
    redo = project.get("working_audio_redo", [])
    if not redo: raise HTTPException(409, "No replacement edit to redo.")
    project.setdefault("working_audio_history", []).append(project.get("working_audio_filename"))
    project["working_audio_filename"] = redo.pop()
    project.pop("last_export", None); update(project)
    return {"filename":project.get("working_audio_filename"),"restored_original":not bool(project.get("working_audio_filename"))}

@app.get("/api/projects/{project_id}/waveform")
def get_waveform(project_id: str, request: Request, points: int = 1800):
    project = user_project(project_id, request)
    if not project.get("audio_filename"):
        raise HTTPException(404, "No audio uploaded.")
    source = project_dir(project_id) / (project.get("working_audio_filename") or project["audio_filename"])
    return waveform_peaks(source, points)

@app.put("/api/projects/{project_id}/timeline")
def save_timeline(project_id: str, payload: dict, request: Request):
    project = user_project(project_id, request)
    duration = float(project.get("audio_duration") or 0)
    ranges = payload.get("ranges", [])
    if not isinstance(ranges, list) or len(ranges) > 5000:
        raise HTTPException(422, "Timeline must contain at most 5,000 ranges.")
    clean, previous = [], -1.0
    for item in sorted(ranges, key=lambda x: float(x.get("start", -1))):
        try: start, end = float(item["start"]), float(item["end"])
        except Exception: raise HTTPException(422, "Each timeline range needs numeric start and end values.")
        if start < 0 or end <= start or end > duration + 0.05 or start < previous - 0.001:
            raise HTTPException(422, "Timeline ranges must be valid, ordered and non-overlapping.")
        clean.append({"start": round(start, 3), "end": round(end, 3)})
        previous = end
    splits = payload.get("splits", project.get("timeline_splits", []))
    if not isinstance(splits, list) or len(splits) > 5000:
        raise HTTPException(422, "Timeline must contain at most 5,000 split markers.")
    try:
        clean_splits = sorted({round(float(x), 3) for x in splits if 0 < float(x) < duration})
    except (TypeError, ValueError):
        raise HTTPException(422, "Split markers must be numeric timestamps.")
    project["timeline_ranges"] = clean
    project["timeline_splits"] = clean_splits
    if project.get("analysis") is not None:
        removed=_removed_ranges(clean,duration)
        project["analysis"]["timeline_removed_segments"] = removed
        project["analysis"]["timeline_removed_seconds"] = round(sum(x["end"]-x["start"] for x in removed),3)
        project["analysis"]["split_markers"] = clean_splits
    project.pop("last_export", None)
    update(project)
    return {"saved": True, "ranges": clean, "splits": clean_splits, "duration": sum(x["end"]-x["start"] for x in clean)}

@app.delete("/api/projects/{project_id}/timeline")
def reset_timeline(project_id: str, request: Request):
    project = user_project(project_id, request)
    project.pop("timeline_ranges", None)
    project.pop("timeline_splits", None)
    if project.get("analysis") is not None:
        project["analysis"].pop("timeline_removed_segments", None)
        project["analysis"].pop("timeline_removed_seconds", None)
        project["analysis"].pop("split_markers", None)
    project.pop("last_export", None)
    update(project)
    return {"reset": True}

@app.get("/api/projects/{project_id}/audio")
def preview_audio(project_id: str, request: Request):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    if not project.get("audio_filename"):
        raise HTTPException(404, "No audio uploaded.")
    path = project_dir(project_id) / project["audio_filename"]
    if not path.exists():
        raise HTTPException(404, "Original audio file not found.")
    return FileResponse(path, filename=project.get("audio_original_name") or path.name)


def run_analysis(project_id: str, job_id: str | None = None, language_hint: str = "auto"):
    try:
        if job_id and not jobs.claim(job_id):
            return
        project = read_project(project_id)
        folder = project_dir(project_id)
        source = folder / str(project.get("working_audio_filename") or project.get("audio_filename") or "")
        if not project.get("script_text", "").strip() or not source.exists():
            raise RuntimeError("A non-empty script and a valid audio recording are required.")
        project["status"] = "analyzing"
        update(project)
        if job_id: jobs.update(job_id, progress=10, stage="transcription")
        transcript_segments, language = transcribe(source, language_hint)
        if job_id: jobs.update(job_id, progress=72, stage="script alignment")
        silence_ranges = detect_silences(source)
        quality = analyze_audio_quality(source)
        report = build_report(project["script_text"], transcript_segments, float(project.get("audio_duration") or 0), silence_ranges, audio_path=source)
        report["detected_language"] = language
        report["audio_quality"] = quality
        report["transcript_segments"] = transcript_segments
        report["analyzed_at"] = datetime.now(timezone.utc).isoformat()
        project["analysis"] = report
        project["status"] = "review"
        project.pop("analysis_error", None)
        update(project)
        if job_id: jobs.update(job_id, status="completed", progress=100, stage="completed", error="")
    except Exception as exc:
        try:
            project = read_project(project_id)
            project["status"] = "error"
            project["analysis_error"] = str(exc)
            update(project)
            if job_id: jobs.update(job_id, status="failed", stage="failed", error=str(exc))
        except Exception:
            if job_id: jobs.update(job_id, status="failed", stage="failed", error=str(exc))

@app.post("/api/projects/{project_id}/analyze")
def start_analysis(project_id: str, request: Request, payload: AnalysisRequest = AnalysisRequest()):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    if not project.get("script_text", "").strip():
        raise HTTPException(400, "Add a script before analysis.")
    if not project.get("audio_filename"):
        raise HTTPException(400, "Upload an audio recording before analysis.")
    if project.get("status") in {"queued", "analyzing"}:
        raise HTTPException(409, "Analysis is already queued or running.")
    project["status"] = "queued"
    project.pop("last_export", None)
    project.pop("analysis_error", None)
    update(project)
    job_id = str(uuid.uuid4())
    project["job_id"] = job_id
    project["analysis_language"] = payload.language
    update(project)
    jobs.create(job_id, project_id, request.state.user_id)
    jobs.POOL.submit(run_analysis, project_id, job_id, payload.language)
    return {"queued": True, "status": "queued", "job_id": job_id}

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = jobs.get(job_id, request.state.user_id)
    if not job: raise HTTPException(404, "Job not found.")
    return job

@app.get("/api/projects/{project_id}/analysis")
def get_analysis(project_id: str, request: Request):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    if project.get("status") == "error":
        return {"status": "error", "error": project.get("analysis_error", "Analysis failed.")}
    return {"status": project.get("status"), "analysis": project.get("analysis")}

@app.patch("/api/projects/{project_id}/sentences/{sentence_index}")
def set_sentence_decision(project_id: str, sentence_index: int, payload: SentenceDecision, request: Request):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    report = project.get("analysis")
    if not report or sentence_index < 0 or sentence_index >= len(report.get("sentences", [])):
        raise HTTPException(404, "Sentence entry not found.")
    row = report["sentences"][sentence_index]
    if payload.take_start is not None or payload.take_end is not None:
        if payload.take_start is None or payload.take_end is None or payload.take_end <= payload.take_start:
            raise HTTPException(422, "A take requires valid start and end timestamps.")
        candidates = row.get("take_candidates", [])
        match = next((c for c in candidates if abs(float(c["start"])-payload.take_start) < 0.01 and abs(float(c["end"])-payload.take_end) < 0.01), None)
        if not match:
            raise HTTPException(422, "The selected alternative take is not part of this analysis report.")
        row["start"], row["end"] = match["start"], match["end"]
        row["audio_ranges"] = [{"start": match["start"], "end": match["end"]}]
        row["recognized"] = match["recognized"]
        row["confidence"] = match["confidence"]
        row["coverage"] = match["coverage"]
        row["selected_take"] = match
        row["status"] = "matched" if match["complete"] and match["confidence"] >= 0.80 else "review"
    if payload.include and (row.get("start") is None or row.get("end") is None):
        raise HTTPException(422, "This sentence has no matched audio segment. It cannot be included in export.")
    row["include"] = payload.include
    row["manual_decision"] = True
    report["matched_sentences"] = sum(1 for item in report.get("sentences",[]) if item.get("status")=="matched")
    report["review_sentences"] = sum(1 for item in report.get("sentences",[]) if item.get("status")=="review")
    report["missing_sentences"] = sum(1 for item in report.get("sentences",[]) if item.get("status")=="missing")
    report["selected_sentences"] = sum(1 for item in report.get("sentences",[]) if item.get("include"))
    project.pop("last_export", None)
    project["status"] = "review"
    update(project)
    return {"saved": True, "index": sentence_index, "include": payload.include}

@app.post("/api/projects/{project_id}/export")
def export_project(project_id: str, payload: ExportRequest, request: Request):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    report = project.get("analysis")
    if not report:
        raise HTTPException(400, "Run analysis before exporting.")
    source = project_dir(project_id) / str(project.get("working_audio_filename") or project.get("audio_filename") or "")
    if not source.exists():
        raise HTTPException(404, "Original audio is missing.")
    selected = [row for row in report.get("sentences", []) if row.get("include") and row.get("start") is not None and row.get("end") is not None]
    if not selected:
        raise HTTPException(422, "No matched sentences are selected. Review the report and select at least one valid sentence.")
    base = safe_name(payload.filename, "edited-voiceover")
    filename = f"{base}.{payload.format}"
    output = project_dir(project_id) / filename
    try:
        options = payload.model_dump()
        options["timeline_ranges"] = project.get("timeline_ranges")
        options["timeline_splits"] = project.get("timeline_splits") or []
        details = export_audio(source, output, selected, options)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        output.unlink(missing_ok=True)
        raise HTTPException(500, f"Audio export failed: {exc}")
    project["last_export"] = {"filename": filename, **details, "format": payload.format}
    if project.get("analysis") is not None:
        project["analysis"]["export_details"] = details
    project["status"] = "done"
    update(project)
    return {"filename": filename, **details, "download_url": f"/api/projects/{project_id}/export/download"}

@app.get("/api/projects/{project_id}/export/download")
def download_export(project_id: str, request: Request):
    try:
        project = user_project(project_id, request)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "Project not found.")
    item = project.get("last_export")
    if not item:
        raise HTTPException(404, "No export found.")
    path = project_dir(project_id) / item["filename"]
    if not path.exists():
        raise HTTPException(404, "Export file not found.")
    return FileResponse(path, filename=item["filename"], media_type="audio/mpeg" if path.suffix == ".mp3" else "audio/wav")

# Serve the frontend when running as a single container.
static_dir = BASE_DIR / "frontend"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")

def recover_analysis_jobs():
    for job in jobs.recoverable():
        try:
            project = read_project(job["project_id"])
            if project.get("status") in {"queued", "analyzing"}:
                project["status"] = "queued"; update(project)
                jobs.update(job["id"], status="queued", stage="recovered")
                jobs.POOL.submit(run_analysis, job["project_id"], job["id"], project.get("analysis_language", "auto"))
        except Exception as exc:
            jobs.update(job["id"], status="failed", stage="recovery failed", error=str(exc))
