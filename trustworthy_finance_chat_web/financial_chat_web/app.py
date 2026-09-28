import json
import os
import re
import uuid
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MAX_PDF_BYTES = 30 * 1024 * 1024

COZE_API_URL = os.getenv(
    "COZE_API_URL",
    "https://bysb7qrsyw.coze.site/run",
).rstrip("/")

if COZE_API_URL.endswith("/run"):
    COZE_BASE_URL = COZE_API_URL[:-4]
else:
    COZE_BASE_URL = COZE_API_URL

COZE_ASYNC_URL = f"{COZE_BASE_URL}/async_run"
COZE_TASK_URL = f"{COZE_BASE_URL}/task"
COZE_API_TOKEN = os.getenv("COZE_API_TOKEN", "")

CACHE_FILE = APP_DIR / "cache" / "reports.json"


def _load_report_cache() -> dict:
    """读取随项目部署的静态年报缓存。读取失败时安全回退为空缓存。"""
    try:
        if CACHE_FILE.exists():
            with CACHE_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _normalize_pdf_cache_key(filename: str) -> str:
    """
    将浏览器下载产生的 (1)、(2) 等副本后缀去掉并转小写。
    例如：
    600519_20260417_9QS4(1).pdf
    -> 600519_20260417_9qs4.pdf
    """
    name = Path(filename or "").name.strip()
    stem = Path(name).stem
    stem = re.sub(r"\s*[（(]\d+[)）]\s*$", "", stem)
    return f"{stem.lower()}.pdf"


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


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {COZE_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _json_or_error(resp):
    try:
        return resp.json(), None
    except ValueError:
        return None, {
            "error": f"分析服务返回非 JSON（HTTP {resp.status_code}）。",
