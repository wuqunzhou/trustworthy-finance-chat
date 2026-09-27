import os
import uuid
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_PDF_BYTES = 30 * 1024 * 1024
COZE_API_URL = os.getenv("COZE_API_URL", "https://bysb7qrsyw.coze.site/run")
COZE_API_TOKEN = os.getenv("COZE_API_TOKEN", "")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_PDF_BYTES + 1024 * 1024


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


def _public_upload_url(filename: str) -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        base = request.url_root.rstrip("/")
    return f"{base}/uploads/{filename}"


@app.post("/api/chat")
def chat():
    if not COZE_API_TOKEN:
        return jsonify({"error": "服务器尚未配置 COZE_API_TOKEN。"}), 500

    user_message = (request.form.get("message") or "").strip()
    session_id = (request.form.get("session_id") or "").strip() or uuid.uuid4().hex
    request_full_report = (request.form.get("request_full_report") or "false").lower() == "true"

    attachment = None
    file = request.files.get("file")
    if file and file.filename:
        original = file.filename
        ext = Path(original).suffix.lower()
        if ext != ".pdf":
            return jsonify({"error": "目前仅支持 PDF 年报。"}), 400

        safe_name = f"{uuid.uuid4().hex}.pdf"
        save_path = UPLOAD_DIR / safe_name
        file.save(save_path)
        if save_path.stat().st_size > MAX_PDF_BYTES:
            save_path.unlink(missing_ok=True)
            return jsonify({"error": "PDF 超过 30 MB，请压缩后再上传。"}), 400

        attachment = {
            "url": _public_upload_url(safe_name),
            "file_type": "document",
        }

    payload = {
        "user_message": user_message,
        "attachment": attachment,
        "session_id": session_id,
        "request_full_report": request_full_report,
    }

    try:
        resp = requests.post(
            COZE_API_URL,
            headers={
                "Authorization": f"Bearer {COZE_API_TOKEN}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=300,
        )
    except requests.RequestException as exc:
        return jsonify({"error": f"调用分析服务失败：{exc}"}), 502

    try:
        data = resp.json()
    except ValueError:
        return jsonify({"error": f"分析服务返回非 JSON（HTTP {resp.status_code}）。", "raw": resp.text[:2000]}), 502

    if not resp.ok:
        return jsonify({"error": "分析服务返回错误。", "detail": data}), resp.status_code

    # Coze /run may return the graph output directly or wrap it.
    if isinstance(data, dict):
        if isinstance(data.get("data"), dict):
            out = data["data"]
        elif isinstance(data.get("output"), dict):
            out = data["output"]
        else:
            out = data
    else:
        out = {"reply": str(data)}

    reply = out.get("reply") or out.get("message") or "分析已完成，但未返回文本回复。"
    return jsonify({
        "session_id": session_id,
        "reply": reply,
        "document_identity": out.get("document_identity", ""),
        "agent1_cached": out.get("agent1_cached", False),
        "markdown_report": out.get("markdown_report", ""),
        "raw": out,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
