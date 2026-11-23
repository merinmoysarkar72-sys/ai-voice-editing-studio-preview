# Security and privacy notes

- Audio is processed by the backend. The app does not send recordings to a third-party transcription API. The selected Whisper model is downloaded from Hugging Face on first use; inference runs in the backend process.
- The original recording is stored as an immutable project source. Segment replacement creates separate replacement/working files. Export never overwrites the original.
- In production, `AVES_ENV=production` requires `AVES_ACCESS_TOKEN` or `AVES_USERS_JSON`. Tokens are stored only in server environment settings and sent through an HTTP-only session cookie. Never put secrets in frontend assets or commit `.env` files.
- Every project API checks its owner ID. In multi-user mode, `AVES_USERS_JSON` maps high-entropy tokens to stable user IDs. The mapping is server-side only.
- Project deletion removes that project's directory, including original recordings, working copies, replacement takes and exports, and deletes its SQLite job records. Deletion is blocked while an analysis job is queued or running to avoid a worker recreating deleted project files.
- Detaching a recording does not delete it. It remains archived in the project until the project itself is deleted.
- No automatic cloud backup, remote object storage, or retention scheduler is configured. Protect the mounted disk and arrange backups at the hosting-provider level if required.
- Use HTTPS for public deployments. For split-origin hosting, set `AVES_ALLOWED_ORIGINS` and keep credentialed CORS limited to exact trusted origins.
- The application has not undergone an external penetration test or formal security audit.
