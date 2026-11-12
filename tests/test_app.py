import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app, init_db


@pytest.fixture
def client_for(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    init_db()

    def make(user):
        c = TestClient(app)
        assert c.post("/api/login", json={"username": user}).status_code == 200
        return c
    return make


def create(c, title="Doc"):
    return c.post("/api/docs", json={"title": title}).json()["id"]


def test_requires_login(client_for):
    assert TestClient(app).get("/api/docs").status_code == 401


def test_create_edit_rename_and_reload_preserves_formatting(client_for):
    alice = client_for("alice")
    doc = create(alice)
    html = "<h1>Title</h1><p><strong>b</strong><em>i</em><u>u</u></p><ol><li>one</li></ol><ul><li>x</li></ul>"
    assert alice.put(f"/api/docs/{doc}", json={"title": "Renamed", "html": html}).status_code == 200
    got = alice.get(f"/api/docs/{doc}").json()
    assert got["title"] == "Renamed" and got["html"] == html and got["role"] == "owner"
    assert alice.put(f"/api/docs/{doc}", json={"title": "  "}).status_code == 400


def test_html_is_sanitized(client_for):
    alice = client_for("alice")
    doc = create(alice)
    alice.put(f"/api/docs/{doc}", json={"html": "<p onclick='x()'>hi</p><script>alert(1)</script>"})
    stored = alice.get(f"/api/docs/{doc}").json()["html"]
    assert "script" not in stored and "onclick" not in stored and "hi" in stored


def test_sharing_rules(client_for):
    alice, bob = client_for("alice"), client_for("bob")
    doc = create(alice, "Plan")
    assert bob.get(f"/api/docs/{doc}").status_code == 404          # no access yet

    assert alice.post(f"/api/docs/{doc}/share", json={"username": "bob", "role": "viewer"}).status_code == 200
    seen = bob.get(f"/api/docs/{doc}").json()
    assert seen["role"] == "viewer" and "shares" not in seen
    assert bob.put(f"/api/docs/{doc}", json={"html": "<p>x</p>"}).status_code == 403
    assert bob.post(f"/api/docs/{doc}/share", json={"username": "carol"}).status_code == 403
    assert bob.delete(f"/api/docs/{doc}").status_code == 403

    lists = bob.get("/api/docs").json()
    assert doc in [d["id"] for d in lists["shared"]]
    assert doc not in [d["id"] for d in lists["owned"]]

    alice.post(f"/api/docs/{doc}/share", json={"username": "bob", "role": "editor"})  # upgrade
    assert bob.put(f"/api/docs/{doc}", json={"html": "<p>x</p>"}).status_code == 200

    alice.delete(f"/api/docs/{doc}/share/bob")                                         # revoke
    assert bob.get(f"/api/docs/{doc}").status_code == 404


def test_cannot_share_with_unknown_user_or_owner(client_for):
    alice = client_for("alice")
    doc = create(alice)
    assert alice.post(f"/api/docs/{doc}/share", json={"username": "mallory"}).status_code == 400
    assert alice.post(f"/api/docs/{doc}/share", json={"username": "alice"}).status_code == 400


def docx_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Hello docx</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


def test_upload_formats_and_validation(client_for):
    alice = client_for("alice")

    r = alice.post("/api/upload", files={"file": ("notes.txt", b"line <b>1</b>\n\nline 2")})
    assert r.status_code == 201
    body = alice.get(f"/api/docs/{r.json()['id']}").json()
    assert body["title"] == "notes" and "&lt;b&gt;" in body["html"]

    r = alice.post("/api/upload", files={"file": ("plan.md", b"# Heading\n\n- a\n- b")})
    assert "<h1>Heading</h1>" in alice.get(f"/api/docs/{r.json()['id']}").json()["html"]

    r = alice.post("/api/upload", files={"file": ("a.docx", docx_bytes())})
    assert r.status_code == 201
    assert "Hello docx" in alice.get(f"/api/docs/{r.json()['id']}").json()["html"]

    assert alice.post("/api/upload", files={"file": ("evil.exe", b"MZ")}).status_code == 400
    assert alice.post("/api/upload", files={"file": ("big.txt", b"a" * (2 * 1024 * 1024 + 1))}).status_code == 413
    assert alice.post("/api/upload", files={"file": ("bad.docx", b"not a zip")}).status_code == 400


def test_session_cookie_cannot_be_forged(client_for):
    plain = TestClient(app)
    assert plain.get("/api/me", headers={"Cookie": "session=alice"}).status_code == 401
    assert plain.get("/api/me", headers={"Cookie": "user=alice"}).status_code == 401
    token = client_for("alice").cookies.get("session")
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert plain.get("/api/me", headers={"Cookie": f"session={tampered}"}).status_code == 401
    swapped = token.replace("alice", "bob", 1)           # change identity, keep signature
    assert plain.get("/api/me", headers={"Cookie": f"session={swapped}"}).status_code == 401
    assert plain.get("/api/me", headers={"Cookie": f"session={token}"}).json()["username"] == "alice"


def test_export_formats(client_for):
    import mammoth
    alice = client_for("alice")
    doc = create(alice, "My Plan")
    alice.put(f"/api/docs/{doc}", json={"html": "<h1>Head</h1><p>Hello <strong>bold</strong> <em>it</em> <u>un</u></p>"
                                                "<ul><li>a</li></ul><ol><li>one</li><li>two</li></ol>"})
    md = alice.get(f"/api/docs/{doc}/export?format=md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    for part in ("# Head", "**bold**", "*it*", "<u>un</u>", "- a", "1. one", "2. two"):
        assert part in md.text
    page = alice.get(f"/api/docs/{doc}/export?format=html").text
    assert "<h1>Head</h1>" in page and "<title>My Plan</title>" in page
    docx = alice.get(f"/api/docs/{doc}/export?format=docx")
    assert 'filename="My Plan.docx"' in docx.headers["content-disposition"]
    text = mammoth.convert_to_html(io.BytesIO(docx.content)).value
    assert "Hello" in text and "<strong>bold</strong>" in text and "two" in text
    assert alice.get(f"/api/docs/{doc}/export?format=pdf").status_code == 422


def test_export_respects_access(client_for):
    alice, bob = client_for("alice"), client_for("bob")
    doc = create(alice)
    assert bob.get(f"/api/docs/{doc}/export?format=md").status_code == 404
    alice.post(f"/api/docs/{doc}/share", json={"username": "bob", "role": "viewer"})
    assert bob.get(f"/api/docs/{doc}/export?format=md").status_code == 200


def test_conflict_detection_blocks_stale_saves(client_for):
    alice, bob = client_for("alice"), client_for("bob")
    doc = create(alice)
    alice.post(f"/api/docs/{doc}/share", json={"username": "bob", "role": "editor"})
    a, b = alice.get(f"/api/docs/{doc}").json(), bob.get(f"/api/docs/{doc}").json()
    assert a["version"] == b["version"] == 1

    r = alice.put(f"/api/docs/{doc}", json={"html": "<p>alice</p>", "base_version": a["version"]})
    assert r.status_code == 200 and r.json()["version"] == 2

    r = bob.put(f"/api/docs/{doc}", json={"html": "<p>bob</p>", "base_version": b["version"]})
    assert r.status_code == 409 and r.json()["detail"]["version"] == 2
    assert alice.get(f"/api/docs/{doc}").json()["html"] == "<p>alice</p>"   # not overwritten

    r = bob.put(f"/api/docs/{doc}", json={"title": "Renamed", "base_version": 2})   # after reloading
    assert r.status_code == 200 and r.json()["version"] == 3
    assert alice.put(f"/api/docs/{doc}", json={"html": "<p>x</p>"}).json()["version"] == 4  # legacy client


def test_init_db_migrates_legacy_database(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE docs(id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL, "
                "html TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL)")
    con.execute("INSERT INTO docs VALUES('old1','alice','Old','<p>x</p>','2024-01-01T00:00:00+00:00')")
    con.commit()
    con.close()
    monkeypatch.setenv("DB_PATH", str(path))
    init_db()
    init_db()   # idempotent
    con = sqlite3.connect(path)
    assert con.execute("SELECT version FROM docs WHERE id='old1'").fetchone()[0] == 1
