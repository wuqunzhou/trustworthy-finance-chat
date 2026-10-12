"""公告 PDF 直传：在 Render 提取公告文字，再进入已部署的 Coze 公告分析分支。

只注册新增路由，不改变现有年报 /api/chat、/api/scenario、缓存与路由。
缺失的日期/来源/链接明确标记待核验；不调用模型来填补不存在的公告事实。
"""
from __future__ import annotations

import io
import re
import uuid
from datetime import date
from typing import Callable

import requests
from pypdf import PdfReader

MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_PAGES = 25
MAX_TEXT_CHARS = 15000


def _valid_date(text: str) -> str:
    m = re.search(r"(20\d{2})\s*(?:年|[-/.])\s*(1[0-2]|0?[1-9])\s*(?:月|[-/.])\s*(3[01]|[12]\d|0?[1-9])\s*日?", text)
    if not m:
        return ""
    try:
        return date(*map(int, m.groups())).isoformat()
    except ValueError:
        return ""


def _publisher(first_page: str) -> str:
    """只从首页的发行人标注、标题或公司落款提取主体，无法确定时拒绝猜测。"""
    lines = [re.sub(r"\s+", "", ln) for ln in first_page.splitlines()]
    lines = [ln for ln in lines if ln]
    top = "\n".join(lines[:28])[:2800]
    labeled = re.search(r"(?:^|\n)(?:公司名称|发行人|上市公司名称)[：:]([^\n]{4,65})", top)
    if labeled:
        text = labeled.group(1).split("（")[0].strip()
        if re.search(r"(股份有限公司|有限责任公司|有限公司)$", text):
            return text

    # 尽量使用单独的标题/署名行，避免把公告正文的一整句话当成公司名称。
    for line in lines[:28]:
        if len(line) > 85:
            continue
        line = re.sub(r"^(?:公告编号|证券代码|证券简称)[^\n]*?[：:]", "", line)
        matches = re.findall(r"[\u4e00-\u9fffA-Za-z0-9·]{2,48}?(?:股份有限公司|有限责任公司|有限公司)", line)
        if len(matches) == 1:
            name = matches[0]
            for lead in ("发行人", "公司名称", "公告主体", "本公司"):
                if name.startswith(lead) and len(name) > len(lead)+4:
                    name = name[len(lead):]
            if 5 <= len(name) <= 55:
                return name
    return ""


def _publication_date(first_page: str, last_page: str) -> str:
    # 只使用明确标注的公告/披露日期，或末页签署日期。不能把交易生效日当公告日。
    labeled = re.search(r"(?:公告日期|披露日期|发布日期|发布于)[：:\s]{0,8}([^\n]{0,40})", first_page)
    if labeled:
        d = _valid_date(labeled.group(1))
        if d:
            return d
    lines = last_page.splitlines()
    for i in range(len(lines)-1, max(-1, len(lines)-13), -1):
        d = _valid_date(lines[i])
        if d:
            return d
    return ""


def extract_announcement_pdf(pdf_bytes: bytes) -> dict:
    """纯文本 PDF 提取。对超长、扫描/加密文档拒绝假装成功。"""
    if not pdf_bytes or len(pdf_bytes) > MAX_FILE_BYTES:
        raise ValueError("公告PDF大小应在0至12MB之间")
    if not pdf_bytes[:1024].lstrip().startswith(b"%PDF-"):
        raise ValueError("上传的文件不是有效PDF，请上传原始公告PDF")
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        if reader.is_encrypted:
            raise ValueError("该公告PDF已加密，请上传未加密的版本")
        count = len(reader.pages)
        if count < 1 or count > MAX_PAGES:
            raise ValueError("本版本支持1至25页的单份公告。长篇公告需使用后续长文档处理流程")
        page_texts = []
        for i, page in enumerate(reader.pages, 1):
            content = (page.extract_text(extraction_mode="plain") or "").strip()
            page_texts.append(content)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("公告PDF解析失败，可能是文件损坏或包含不支持的内容") from exc

    total_text = "\n".join(page_texts).strip()
    if len(total_text) < 80:
        raise ValueError("未提取到足够的公告文字。可能是扫描图片PDF，当前版本不支持OCR，请使用可复制文字的原版公告PDF")
    if len(total_text) > MAX_TEXT_CHARS:
        raise ValueError("公告文字超过15000字；为防止截断导致误读，本次不执行不完整分析")
    # 年报 PDF 不得误入公告分析。
    head = "\n".join(page_texts[:2])[:2000]
    if re.search(r"(?:20\d{2}年|二[〇零一二三四五六七八九十]{4}年).*?年度报告", head, re.S) or re.search(r"年度报告[（(]?(?:全文|摘要)[）)]?", head):
        raise ValueError("这看起来是年度报告，请切换到「年报分析」上传")

    company = _publisher(page_texts[0]) or _publisher(page_texts[-1])
    if not company:
        raise ValueError("无法从公告首页可靠识别发布公司。请上传包含完整公司抬头的公告原版PDF")

    publish_date = _publication_date(page_texts[0], page_texts[-1])
    marked = "\n\n".join(
        f"【PDF原文第{i}页】\n{t}" for i, t in enumerate(page_texts, 1) if t
    )
    return {
        "company": company,
        "publish_date": publish_date,
        "page_count": count,
        "text": marked,
        "source_warning": "上传文件真实性及原始下载网址未独立核验",
    }


def build_coze_request(document: dict, session_id: str) -> dict:
    """严格沿用线上已验证的 /公告 指令格式。"""
    command = "\n".join([
        "/公告",
        f"公司：{document['company']}",
        f"日期：{document['publish_date'] or '未提供（待核验）'}",
        "来源：用户上传的公告PDF（未独立核验真实性）",
        "链接：未提供（待核验）",
        "正文：" + document["text"],
    ])
    return {
        "user_message": command,
        "session_id": session_id,
        "request_full_report": False,
        "run_mode": "production",
        "allow_demo": False,
    }


def register_announcement_pdf(app, *, coze_api_url: str,
                              headers_fn: Callable, unwrap_fn: Callable,
                              normalize_fn: Callable):
    """在现有 app.py 中注册 /api/announcement_pdf，不覆盖任何旧路由。"""
    from flask import jsonify, request

    @app.post("/api/announcement_pdf")
    def announcement_pdf_analyze():
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "请选择一份上市公司公告PDF"}), 400
        if not file.filename.lower().endswith(".pdf"):
            return jsonify({"error": "只支持PDF格式公告"}), 400
        try:
            blob = file.stream.read(MAX_FILE_BYTES + 1)
            document = extract_announcement_pdf(blob)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422

        session_id = (request.form.get("session_id") or "").strip() or uuid.uuid4().hex
        if len(session_id) > 200:
            return jsonify({"error": "会话标识过长"}), 400

        payload = build_coze_request(document, session_id)
        headers = headers_fn()
        if not str(headers.get("Authorization", "")).replace("Bearer", "").strip():
            return jsonify({"error": "服务器未配置Coze分析服务凭据"}), 500
        try:
            resp = requests.post(
                coze_api_url,
                headers=headers,
                json=payload,
                timeout=90,
            )
        except requests.RequestException:
            return jsonify({"error": "公告分析服务暂时无法访问，请稍后重试"}), 502
        try:
            data = resp.json()
        except ValueError:
            return jsonify({"error": "公告分析服务未返回有效JSON"}), 502
        if not resp.ok:
            return jsonify({"error": "公告分析服务返回错误，请检查部署日志"}), 502
        result = unwrap_fn(data)
        output = normalize_fn(result, session_id)
        reply = str(output.get("reply") or "")
        # 让来源核验边界在用户界面可见，但不把核验缺失伪装为正式批准。
        output["reply"] = reply + (
            "\n\n【PDF来源说明】\n"
            f"公告文件：{file.filename[:100]}\n"
            f"自动识别公司：{document['company']}\n"
            f"公告日期：{document['publish_date'] or '待核验'}\n"
            f"原文页数：{document['page_count']}页\n"
            "来源真实性及PDF版本尚未独立核验；上述事件分析不自动修改正式估值。"
        )
        if "【公告事件分析】" in reply:
            output["markdown_report"] = "\n".join([
                f"# {document['company']} · 公告事件研究简报（初步分析）",
                "",
                "## 一、输入材料",
                f"- 文件：{file.filename[:100]}",
                f"- PDF页数：{document['page_count']}",
                f"- 公告日期：{document['publish_date'] or '尚未核实'}",
                "- 来源状态：用户上传PDF，尚未独立核验真实性和原始下载网址",
                "",
                "## 二、事件提取与Reviewer审核",
                reply,
                "",
                "## 三、估值权限与方法边界",
                "- 公告事件的识别不代表已经核实其真实财务影响。",
                "- Reviewer未批准的数据及信号不得直接改变正式估值参数。",
                "- 仅在具备匹配的公司财务基准与用户授权假设时，才可进一步执行隔离情景试算。",
                "- 情景试算不是公告的真实股价因果影响，亦不构成投资建议。",
            ])
        output.update({
            "async": False,
            "announcement_pdf": True,
            "pdf_pages": document["page_count"],
            "pdf_company": document["company"],
            "pdf_date": document["publish_date"],
            "pdf_source_verified": False,
        })
        return jsonify(output)
