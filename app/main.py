"""Lightweight collaborative docs: FastAPI + SQLite. Architecture note: see README."""
import hashlib
import hmac
import html as htmllib
import io
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import mammoth
import markdown
import nh3
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import export

# Mock auth: seeded users, identity kept in a cookie. Swap for real auth in production.
USERS = {"alice": "Alice Johnson", "bob": "Bob Smith", "carol": "Carol Diaz"}
MAX_UPLOAD = 2 * 1024 * 1024
# Session cookies are HMAC-signed. Set SESSION_SECRET in production; without it a random
# secret is generated per process, so sessions reset on restart (fine for a single demo instance).
SECRET = (os.environ.get("SESSION_SECRET") or secrets.token_hex(32)).encode()
SESSION_MAX_AGE = 7 * 24 * 3600
ALLOWED_EXT = {".txt", ".md", ".docx"}
TAGS = {"p", "br", "strong", "em", "u", "s", "h1", "h2", "h3", "h4",
        "ul", "ol", "li", "blockquote", "a"}
INDEX = Path(__file__).parent / "static" / "index.html"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean(raw: str) -> str:
    """Whitelist-sanitize HTML before it is stored or served (prevents stored XSS)."""
    return nh3.clean(raw, tags=TAGS, attributes={"a": {"href"}},
                     url_schemes={"http", "https", "mailto"})


@contextmanager
def db():
    conn = sqlite3.connect(os.environ.get("DB_PATH", "docs.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def new_doc(c, owner: str, title: str, body: str) -> str:
    doc_id = uuid.uuid4().hex[:12]
    c.execute("INSERT INTO docs(id,owner,title,html,updated_at) VALUES(?,?,?,?,?)",
              (doc_id, owner, title, clean(body), now()))
    return doc_id


def init_db():
    Path(os.environ.get("DB_PATH", "docs.db")).resolve().parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS docs(
          id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES users(username),
          title TEXT NOT NULL, html TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS shares(
          doc_id TEXT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
          username TEXT NOT NULL REFERENCES users(username),
          role TEXT NOT NULL CHECK(role IN ('viewer','editor')),
          PRIMARY KEY(doc_id, username));
        """)
        # Migrate databases created before optimistic concurrency was added.
        if "version" not in [r["name"] for r in c.execute("PRAGMA table_info(docs)")]:
            c.execute("ALTER TABLE docs ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        c.executemany("INSERT OR IGNORE INTO users VALUES(?,?)", USERS.items())
        if not c.execute("SELECT 1 FROM docs").fetchone():
            did = new_doc(c, "alice", "Welcome to Docs",
                          "<h1>Welcome</h1><p>This demo document is shared with Bob as an "
                          "<strong>editor</strong>.</p><ul><li>Format text with the toolbar</li>"
                          "<li>Switch user (top right) to see sharing</li></ul>")
            c.execute("INSERT INTO shares VALUES(?,?,?)", (did, "bob", "editor"))


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Docs", lifespan=lifespan)


def _mac(msg: str) -> str:
    return hmac.new(SECRET, msg.encode(), hashlib.sha256).hexdigest()


def sign_session(user: str) -> str:
    msg = f"{user}:{int(time.time())}"
    return f"{msg}:{_mac(msg)}"


def verify_session(token: str | None) -> str | None:
    try:
        user, ts, sig = (token or "").split(":")
        if not hmac.compare_digest(sig, _mac(f"{user}:{ts}")):
            return None
        if time.time() - int(ts) > SESSION_MAX_AGE:
            return None
    except ValueError:
        return None
    return user if user in USERS else None


def current_user(request: Request) -> str:
    user = verify_session(request.cookies.get("session"))
    if not user:
        raise HTTPException(401, "Log in first.")
    return user


def get_access(c, doc_id: str, user: str, edit=False, owner=False):
    """Return (doc_row, role). Users without access get 404 so ids don't leak."""
    row = c.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Document not found.")
    if row["owner"] == user:
        role = "owner"
    else:
        s = c.execute("SELECT role FROM shares WHERE doc_id=? AND username=?",
                      (doc_id, user)).fetchone()
        if not s:
            raise HTTPException(404, "Document not found.")
        role = s["role"]
    if owner and role != "owner":
        raise HTTPException(403, "Only the owner can do this.")
    if edit and role == "viewer":
        raise HTTPException(403, "You have view-only access to this document.")
    return row, role


def share_list(c, doc_id: str):
    rows = c.execute("SELECT s.username, u.name, s.role FROM shares s JOIN users u "
                     "ON u.username=s.username WHERE s.doc_id=? ORDER BY u.name", (doc_id,))
    return [dict(r) for r in rows]


class Login(BaseModel):
    username: str


class DocCreate(BaseModel):
    title: str = Field("Untitled document", max_length=200)


class DocPatch(BaseModel):
    title: str | None = Field(None, max_length=200)
    html: str | None = Field(None, max_length=1_000_000)
    # The version this edit was based on. If given and stale, the save is rejected with 409.
    # Omitting it keeps last-write-wins behaviour for simple API clients.
    base_version: int | None = Field(None, ge=1)


class ShareIn(BaseModel):
    username: str
    role: Literal["viewer", "editor"] = "editor"


@app.get("/")
def index():
    return FileResponse(INDEX)


@app.get("/api/users")
def users():
    return [{"username": u, "name": n} for u, n in USERS.items()]


@app.post("/api/login")
def login(body: Login, request: Request, response: Response):
    if body.username not in USERS:
        raise HTTPException(400, "Unknown user.")
    response.set_cookie("session", sign_session(body.username), httponly=True, samesite="lax",
                        max_age=SESSION_MAX_AGE, secure=request.url.scheme == "https")
    return {"username": body.username, "name": USERS[body.username]}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"username": user, "name": USERS[user]}


@app.get("/api/docs")
def list_docs(user=Depends(current_user)):
    with db() as c:
        owned = c.execute(
            "SELECT d.id,d.title,d.updated_at,d.owner,"
            "(SELECT COUNT(*) FROM shares s WHERE s.doc_id=d.id) AS n_shared "
            "FROM docs d WHERE d.owner=? ORDER BY d.updated_at DESC", (user,)).fetchall()
        shared = c.execute(
            "SELECT d.id,d.title,d.updated_at,d.owner,u.name AS owner_name,s.role "
            "FROM docs d JOIN shares s ON s.doc_id=d.id JOIN users u ON u.username=d.owner "
            "WHERE s.username=? ORDER BY d.updated_at DESC", (user,)).fetchall()
    return {"owned": [dict(r) for r in owned], "shared": [dict(r) for r in shared]}


@app.post("/api/docs", status_code=201)
def create_doc(body: DocCreate, user=Depends(current_user)):
    title = body.title.strip() or "Untitled document"
    with db() as c:
        return {"id": new_doc(c, user, title, "")}


@app.get("/api/docs/{doc_id}")
def read_doc(doc_id: str, user=Depends(current_user)):
    with db() as c:
        row, role = get_access(c, doc_id, user)
        out = {"id": row["id"], "title": row["title"], "html": row["html"],
               "owner": row["owner"], "owner_name": USERS[row["owner"]],
               "updated_at": row["updated_at"], "version": row["version"], "role": role}
        if role == "owner":
            out["shares"] = share_list(c, doc_id)
        return out


@app.get("/api/docs/{doc_id}/export")
def export_doc(doc_id: str, fmt: Literal["docx", "md", "html"] = Query("docx", alias="format"),
               user=Depends(current_user)):
    """Download a document (owners, editors and viewers alike) as .docx, .md or .html."""
    with db() as c:
        row, _ = get_access(c, doc_id, user)
    if fmt == "docx":
        data = export.to_docx(row["html"])
        mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    elif fmt == "md":
        data, mime = export.to_markdown(row["html"]).encode(), "text/markdown; charset=utf-8"
    else:
        data, mime = export.to_html(row["title"], row["html"]).encode(), "text/html; charset=utf-8"
    name = re.sub(r"[^\w\- ]+", "", row["title"]).strip() or "document"
    ascii_name = name.encode("ascii", "ignore").decode().strip() or "document"
    disp = f"attachment; filename=\"{ascii_name}.{fmt}\"; filename*=UTF-8''{quote(name)}.{fmt}"
    return Response(data, media_type=mime, headers={"Content-Disposition": disp})


@app.put("/api/docs/{doc_id}")
def update_doc(doc_id: str, body: DocPatch, user=Depends(current_user)):
    with db() as c:
        row, _ = get_access(c, doc_id, user, edit=True)
        title, html = row["title"], row["html"]
        if body.title is not None:
            title = body.title.strip()
            if not title:
                raise HTTPException(400, "Title can't be empty.")
        if body.html is not None:
            html = clean(body.html)
        stamp = now()
        guard = "" if body.base_version is None else " AND version=?"
        args = (title, html, stamp, doc_id) + (() if body.base_version is None else (body.base_version,))
        # The version check is part of the UPDATE itself, so it is atomic even with concurrent saves.
        res = c.execute("UPDATE docs SET title=?, html=?, updated_at=?, version=version+1 "
                        "WHERE id=?" + guard, args)
        latest = c.execute("SELECT version FROM docs WHERE id=?", (doc_id,)).fetchone()
        if res.rowcount == 0:
            if latest is None:
                raise HTTPException(404, "Document not found.")
            raise HTTPException(409, {"message": "Someone else saved a newer version of this document.",
                                      "version": latest["version"]})
    return {"title": title, "updated_at": stamp, "version": latest["version"]}


@app.delete("/api/docs/{doc_id}", status_code=204)
def delete_doc(doc_id: str, user=Depends(current_user)):
    with db() as c:
        get_access(c, doc_id, user, owner=True)
        c.execute("DELETE FROM docs WHERE id=?", (doc_id,))


@app.post("/api/docs/{doc_id}/share")
def share_doc(doc_id: str, body: ShareIn, user=Depends(current_user)):
    with db() as c:
        row, _ = get_access(c, doc_id, user, owner=True)
        if body.username not in USERS:
            raise HTTPException(400, "Unknown user.")
        if body.username == row["owner"]:
            raise HTTPException(400, "The owner already has full access.")
        c.execute("INSERT INTO shares VALUES(?,?,?) ON CONFLICT(doc_id,username) "
                  "DO UPDATE SET role=excluded.role", (doc_id, body.username, body.role))
        return share_list(c, doc_id)


@app.delete("/api/docs/{doc_id}/share/{username}")
def unshare_doc(doc_id: str, username: str, user=Depends(current_user)):
    with db() as c:
        get_access(c, doc_id, user, owner=True)
        c.execute("DELETE FROM shares WHERE doc_id=? AND username=?", (doc_id, username))
        return share_list(c, doc_id)


@app.post("/api/upload", status_code=201)
async def upload(file: UploadFile = File(...), user=Depends(current_user)):
    """Turn a .txt, .md or .docx file into a new editable document."""
    name = file.filename or ""
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "Unsupported file type. Upload a .txt, .md or .docx file.")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File is too large (limit 2 MB).")
    try:
        if ext == ".docx":
            body = mammoth.convert_to_html(io.BytesIO(data)).value
        else:
            text = data.decode("utf-8", errors="replace")
            if ext == ".md":
                body = markdown.markdown(text)
            else:
                body = "".join(f"<p>{htmllib.escape(line) or '<br>'}</p>"
                               for line in text.splitlines())
    except Exception:
        raise HTTPException(400, "Couldn't read that file. Check it isn't corrupted.")
    title = Path(name).stem.strip()[:200] or "Imported document"
    with db() as c:
        return {"id": new_doc(c, user, title, body), "title": title}
