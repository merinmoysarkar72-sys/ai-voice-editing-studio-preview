import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from .config import PROJECTS_DIR


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str, fallback: str = "project") -> str:
    name = re.sub(r"[^\w\-. ]+", "", value, flags=re.UNICODE).strip(" .")
    return (name[:80] or fallback)


def project_dir(project_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f-]{36}", project_id):
        raise ValueError("Invalid project ID")
    path = (PROJECTS_DIR / project_id).resolve()
    if PROJECTS_DIR not in path.parents:
        raise ValueError("Invalid project path")
    return path


def read_project(project_id: str) -> dict:
    folder = project_dir(project_id)
    meta = folder / "project.json"
    if not meta.exists():
        raise FileNotFoundError("Project not found")
    return json.loads(meta.read_text(encoding="utf-8"))


def write_project(project: dict) -> None:
    folder = project_dir(project["id"])
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / "project.json.tmp"
    temp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(folder / "project.json")


def create_project(name: str, script_text: str = "") -> dict:
    project_id = str(uuid4())
    folder = project_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    project = {
        "id": project_id,
        "name": safe_name(name),
        "script_text": script_text,
        "script_filename": None,
        "audio_filename": None,
        "audio_original_name": None,
        "audio_duration": None,
        "audio_size": None,
        "status": "draft",
        "analysis": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    write_project(project)
    return project


def list_projects() -> list[dict]:
    items = []
    for folder in PROJECTS_DIR.iterdir():
        if folder.is_dir() and (folder / "project.json").exists():
            try:
                item = json.loads((folder / "project.json").read_text(encoding="utf-8"))
                items.append({k: item.get(k) for k in ("id", "name", "status", "created_at", "updated_at", "audio_duration", "owner_id")})
            except Exception:
                continue
    return sorted(items, key=lambda x: x.get("updated_at") or "", reverse=True)


def delete_project(project_id: str) -> None:
    import shutil
    folder = project_dir(project_id)
    if not (folder / "project.json").exists():
        raise FileNotFoundError("Project not found")
    shutil.rmtree(folder)
