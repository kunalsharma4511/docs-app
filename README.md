# Docs: a lightweight collaborative document editor

FastAPI + SQLite + vanilla JS + Quill. No build step, no paid services.

## Features
- **Create / rename / edit / save / reopen** documents. Autosave (800 ms debounce, with a visible Saved / Saving / Not saved indicator; `Ctrl/Cmd+S` saves immediately).
- **Rich text:** bold, italic, underline, headings (H1-H3), bulleted and numbered lists, clear formatting.
- **File upload:** `.txt`, `.md` and `.docx` become a new editable document (max 2 MB). Other types are rejected with a clear message. `.docx` keeps headings, bold/italic, lists and links; tables and images are not carried over.
- **Sharing:** every document has an owner. Owners share with another user as **editor** or **viewer**, change the role, or revoke access. The sidebar separates *My documents* from *Shared with me*, with owner and role shown. Viewers get a read-only editor.
- **Conflict detection:** every save carries the version it was based on. If someone else saved in the meantime the server rejects it (HTTP 409), nothing is overwritten, and the editor offers *Load latest version* or *Overwrite with my version*.
- **Export:** download any document you can open (owned or shared, including view-only) as Word (.docx), Markdown (.md) or HTML. Formatting (headings, bold, italic, underline, lists) carries over; the .docx is generated without extra dependencies.
- **Sessions:** login sets an HMAC-signed, expiring, HttpOnly cookie, so identity can't be forged by editing a cookie by hand.
- **Persistence:** SQLite. Formatting is stored as sanitized HTML.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload                         # http://localhost:8000
```
Demo users are seeded (Alice, Bob, Carol). Use the "Signed in as" menu in the header to switch.
**Demo script:** as Alice open "Welcome to Docs" (already shared with Bob), create a doc, Share it with Carol as *viewer*, switch to Carol and see it under *Shared with me* in read-only mode.

## Tests
```bash
pytest -q
```
Covers: auth required, forged/tampered/swapped session cookies, export in all three formats (docx round-tripped through mammoth) and its access rules, conflict detection (stale save returns 409 and leaves content intact, retry succeeds) and migration of pre-existing databases, create/rename/reload preserving formatting, HTML sanitization, the full sharing permission matrix (no access, viewer, editor, revoke, owner-only actions), and upload (txt/md/docx, bad type, oversize, corrupt file).

## CI
`.github/workflows/ci.yml` runs the test suite and builds the Docker image on every push and pull request (GitHub Actions, free for public repos). It must sit at the repository root: if your repo root is the parent of this folder, move `.github` there and add `defaults: run: working-directory: docs-app` plus `working-directory`-style paths for the Docker build.

## Deploy (free)
The app ships as one Docker image. **Set `SESSION_SECRET` to a long random value** (e.g. `python -c "import secrets;print(secrets.token_hex(32))"`) so sessions survive restarts; otherwise a random one is generated at startup. Set `DB_PATH` to change where the SQLite file lives (default in the image: `/tmp/docs.db`).

**Render (Docker, free web service):** push this folder to GitHub, then New > Web Service > connect repo > Runtime: **Docker** > Instance type: **Free**. Render sets `$PORT` automatically.

**Hugging Face Spaces (alternative):** create a Space with SDK **Docker**, push this folder, and add `app_port: 7860` to the README front matter plus a Space variable `PORT=7860`.

> Free tiers have ephemeral disks, so data resets when the instance restarts or redeploys. The demo users and welcome document are re-seeded on every start so the demo always works. For real use, point `DB_PATH` at a persistent volume or move to Postgres.

## Architecture note
**What I prioritized and why**
1. **Correct, server-enforced permissions.** Access is resolved in one function (`get_access`) used by every document endpoint, so roles can't drift between routes. Users with no access get 404, not 403, so document ids don't leak. This is the part that is hardest to retrofit, so it got the most tests.
2. **Safe rich text.** Quill's HTML is whitelist-sanitized with `nh3` on every write, including uploads, so stored XSS isn't possible even if a client is modified. HTML is the storage format because it round-trips with Quill and renders anywhere.
3. **Minimal moving parts.** One Python service, one SQLite file, one HTML file, one CDN script. That makes it easy to run, review, and deploy.
4. **Upload as an import flow.** Upload turns a file into an editable document, which is the most useful product behavior for a docs tool, and it reuses the same sanitize-and-store path as editing.

**Deliberate trade-offs**
- Auth is mocked: anyone can pick any seeded user (no passwords), though the session itself is signed and can't be forged. Production would use SSO/OAuth.
- Concurrency is optimistic: a per-document version number is checked inside the SQL `UPDATE`, so conflicts are detected atomically, but there is no merge and no live co-editing. The path forward is a CRDT (Yjs) over WebSockets. API clients that omit `base_version` fall back to last-write-wins.
- Quill 1.3.7 is loaded from jsDelivr; vendor it into `static/` for offline or locked-down environments.
- Not built: version history, comments, PDF export, search, attachments on existing documents.

## API summary
`POST /api/login` · `GET /api/me` · `GET/POST /api/docs` · `GET/PUT/DELETE /api/docs/{id}` (PUT accepts `base_version`; stale value returns 409) · `POST /api/docs/{id}/share` · `DELETE /api/docs/{id}/share/{username}` · `GET /api/docs/{id}/export?format=docx|md|html` · `POST /api/upload`
