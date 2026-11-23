from pathlib import Path
import os

APP_NAME = "AI Voice Editing Studio"
BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("AVES_DATA_DIR", BASE_DIR / "data")).resolve()
PROJECTS_DIR = DATA_DIR / "projects"
os.environ.setdefault("HF_HOME", str(DATA_DIR / "models"))
MAX_AUDIO_MB = int(os.getenv("AVES_MAX_AUDIO_MB", "500"))
MAX_SCRIPT_MB = int(os.getenv("AVES_MAX_SCRIPT_MB", "20"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
ALLOWED_AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm", ".flac"}
ALLOWED_SCRIPT = {".txt", ".pdf", ".docx"}

PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
