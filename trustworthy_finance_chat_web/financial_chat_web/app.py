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

@app.route("/showcase", methods=["GET"])
def competition_showcase():
    return render_template("showcase.html")

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
            "raw": resp.text[:2000],
        }


def _unwrap_output(value):
    """兼容直接 dict、JSON 字符串以及 data/output 包装。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {"reply": value}

    if not isinstance(value, dict):
        return {"reply": str(value)}

    if isinstance(value.get("data"), dict):
        value = value["data"]
    elif isinstance(value.get("output"), dict):
        value = value["output"]

    # 某些异步任务 result 可能再次是 JSON 字符串
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return {"reply": value}

    return value if isinstance(value, dict) else {"reply": str(value)}




def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _dcf_per_share(
    operating_cash_flow: float,
    shares: float,
    growth: float,
    wacc: float,
    terminal_growth: float,
    years: int,
    net_debt: float = 0.0,
):
    """
    简化 DCF：以经营现金流作为 FCF 代理。
    这是演示/决策支持口径，不等同于严格 FCFF。
    """
    if not operating_cash_flow or not shares or shares <= 0:
        return None
    if wacc <= terminal_growth:
        return None

    cash = operating_cash_flow
    pv = 0.0
    for t in range(1, years + 1):
        cash *= 1 + growth
        pv += cash / ((1 + wacc) ** t)

    terminal_value = cash * (1 + terminal_growth) / (wacc - terminal_growth)
    pv_terminal = terminal_value / ((1 + wacc) ** years)
    equity_value = pv + pv_terminal - net_debt
    return equity_value / shares


def _rim_per_share(
    net_income: float,
    equity: float,
    shares: float,
    cost_of_equity: float,
    growth: float,
    terminal_growth: float,
    years: int,
):
    """
    简化剩余收益模型（RIM）：
    以当前归母净利润和归母净资产为起点，并将剩余收益按 growth 外推。
    """
    if not net_income or not equity or not shares or shares <= 0:
        return None
    if cost_of_equity <= terminal_growth:
        return None

    residual_income = net_income - cost_of_equity * equity
    ri = residual_income
    pv_ri = 0.0

    for t in range(1, years + 1):
        ri *= 1 + growth
        pv_ri += ri / ((1 + cost_of_equity) ** t)

    terminal_ri = ri * (1 + terminal_growth) / (cost_of_equity - terminal_growth)
    pv_terminal = terminal_ri / ((1 + cost_of_equity) ** years)
    return (equity + pv_ri + pv_terminal) / shares


def _solve_implied_growth(
    market_price: float,
    operating_cash_flow: float,
    shares: float,
    wacc: float,
    terminal_growth: float,
    years: int,
    net_debt: float = 0.0,
):
    """
    在给定 WACC/终值增长率下，反推使简化 DCF 等于市场价格的增长率。
    仅用于“市场隐含预期”提示。
    """
    if not market_price or market_price <= 0:
        return None

    lo, hi = -0.20, 0.30
    for _ in range(80):
        mid = (lo + hi) / 2
        value = _dcf_per_share(
            operating_cash_flow,
            shares,
            mid,
            wacc,
            terminal_growth,
            years,
            net_debt,
        )
        if value is None:
            return None
        if value < market_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _auto_valuation_from_cache(cached: dict) -> dict:
    """
    对已经完成年报解析的缓存数据执行确定性自动估值。
    原则：
    1. 年报事实直接使用；
    2. 可计算参数用公式计算；
    3. 无法从年报得到的估值参数使用“系统默认假设”，并显式标注；
    4. 不把系统假设伪装成年报事实。
    """
    facts = cached.get("financial_facts") or {}

    revenue = _safe_float(facts.get("revenue"))
    net_income = _safe_float(
        facts.get("net_profit_parent"),
        _safe_float(facts.get("net_income")),
    )
    ocf = _safe_float(facts.get("operating_cash_flow"))
    equity = _safe_float(
        facts.get("equity_parent"),
        _safe_float(facts.get("equity")),
    )
    shares = _safe_float(
        facts.get("shares"),
        _safe_float(facts.get("shares_outstanding")),
    )

    # 明确、可解释的系统默认估值参数。
    assumptions = {
        "forecast_growth": 0.05,
        "forecast_years": 5,
        "terminal_growth": 0.03,
        "wacc": 0.08,
        "cost_of_equity": 0.09,
        "net_debt": 0.0,
    }

    # 若缓存文件未来写入了明确的估值假设，则优先使用缓存中的值。
    cached_assumptions = cached.get("valuation_assumptions") or {}
    for key in assumptions:
        if key in cached_assumptions:
            val = _safe_float(cached_assumptions.get(key))
            if val is not None:
                assumptions[key] = int(val) if key == "forecast_years" else val

    forecast_roe = None
    if net_income is not None and equity and equity > 0:
        forecast_roe = net_income / equity

    retention = None
    if forecast_roe and forecast_roe > 0:
        retention = min(max(assumptions["forecast_growth"] / forecast_roe, 0.0), 1.0)

    dcf = _dcf_per_share(
        ocf,
        shares,
        assumptions["forecast_growth"],
        assumptions["wacc"],
        assumptions["terminal_growth"],
        assumptions["forecast_years"],
        assumptions["net_debt"],
    )

    rim = _rim_per_share(
        net_income,
        equity,
        shares,
        assumptions["cost_of_equity"],
        assumptions["forecast_growth"],
        assumptions["terminal_growth"],
        assumptions["forecast_years"],
    )

    # 敏感性场景：只改变增长率/WACC，其他口径保持一致。
    sensitivity = {}
    for label, growth, wacc in (
        ("保守情景", 0.03, 0.09),
        ("基准情景", assumptions["forecast_growth"], assumptions["wacc"]),
        ("积极情景", 0.07, 0.07),
    ):
        sensitivity[label] = _dcf_per_share(
            ocf,
            shares,
            growth,
            wacc,
            assumptions["terminal_growth"],
            assumptions["forecast_years"],
            assumptions["net_debt"],
        )

    market_price = _safe_float(cached.get("market_price"))
    market_price_date = cached.get("market_price_date", "")
    market_price_source = cached.get("market_price_source", "")

    implied_growth = None
    if market_price:
        implied_growth = _solve_implied_growth(
            market_price,
            ocf,
            shares,
            assumptions["wacc"],
            assumptions["terminal_growth"],
            assumptions["forecast_years"],
            assumptions["net_debt"],
        )

    return {
        "revenue": revenue,
        "net_income": net_income,
        "operating_cash_flow": ocf,
        "equity": equity,
        "shares": shares,
        "forecast_roe": forecast_roe,
        "retention": retention,
        "assumptions": assumptions,
        "dcf_per_share": dcf,
        "rim_per_share": rim,
        "sensitivity": sensitivity,
        "market_price": market_price,
        "market_price_date": market_price_date,
        "market_price_source": market_price_source,
        "implied_growth": implied_growth,
    }


def _format_auto_valuation_reply(cached: dict, result: dict) -> str:
    company = cached.get("company") or "该公司"
    a = result["assumptions"]

    lines = [
        f"已解析：{company}年报（缓存命中，自动估值模式）。",
        "",
        "【一、年报事实】",
    ]

    if result["revenue"] is not None:
        lines.append(f"- 营业收入：{result['revenue']:,.2f}")
    if result["net_income"] is not None:
        lines.append(f"- 归母净利润：{result['net_income']:,.2f}")
    if result["operating_cash_flow"] is not None:
        lines.append(f"- 经营现金流：{result['operating_cash_flow']:,.2f}")
    if result["equity"] is not None:
        lines.append(f"- 归母净资产：{result['equity']:,.2f}")
    if result["shares"] is not None:
        lines.append(f"- 总股本：{result['shares']:,.0f}")

    lines += [
        "",
        "【二、系统自动补齐的估值参数】",
        f"- 预测增长率：{_pct(a['forecast_growth'])}（系统默认假设，可修改）",
        f"- 预测期：{a['forecast_years']} 年（系统默认假设，可修改）",
        f"- WACC：{_pct(a['wacc'])}（系统默认假设，可修改）",
        f"- 权益成本：{_pct(a['cost_of_equity'])}（系统默认假设，可修改）",
        f"- 终值增长率：{_pct(a['terminal_growth'])}（系统默认假设，可修改）",
        f"- 净债务：{a['net_debt']:,.2f}（当前缓存未提供时按 0 的演示假设处理）",
    ]

    if result["forecast_roe"] is not None:
        lines.append(
            f"- 参考 ROE：{_pct(result['forecast_roe'])}（归母净利润 ÷ 归母净资产计算）"
        )
    if result["retention"] is not None:
        lines.append(
            f"- 隐含留存率：{_pct(result['retention'])}（预测增长率 ÷ ROE 反推）"
        )

    lines += ["", "【三、自动估值结果】"]

    if result["dcf_per_share"] is not None:
        lines.append(
            f"- 简化 DCF：{result['dcf_per_share']:,.2f} 元/股"
            "（经营现金流作为 FCF 代理）"
        )
    else:
        lines.append("- 简化 DCF：数据不足，未计算")

    if result["rim_per_share"] is not None:
        lines.append(
            f"- 简化 RIM：{result['rim_per_share']:,.2f} 元/股"
            "（剩余收益模型简化口径）"
        )
    else:
        lines.append("- 简化 RIM：数据不足，未计算")

    lines += ["", "【四、DCF 敏感性分析】"]
    for label, value in result["sensitivity"].items():
        if value is not None:
            lines.append(f"- {label}：{value:,.2f} 元/股")

    if result["market_price"] is not None:
        date_text = f"（{result['market_price_date']}）" if result["market_price_date"] else ""
        source_text = (
            f"，来源：{result['market_price_source']}"
            if result["market_price_source"]
            else ""
        )
        lines += [
            "",
            "【五、市场隐含预期】",
            f"- 参考股价：{result['market_price']:,.2f} 元{date_text}{source_text}",
        ]
        if result["implied_growth"] is not None:
            lines.append(
                f"- 在当前 WACC/终值增长率假设下，市场隐含增长率约："
                f"{_pct(result['implied_growth'])}"
            )
    else:
        lines += [
            "",
            "【五、市场隐含预期】",
            "- 当前缓存未接入可核验股价，因此不强行判断“高估/低估”，也不编造市场价格。",
        ]

    lines += [
        "",
        "【审计说明】",
        "- 年报财务数据：来自已完成的真实年报解析缓存。",
        "- ROE/留存率：由公式计算。",
        "- WACC、增长率、预测期、终值增长率等：属于系统估值假设，不是公司披露事实。",
        "- DCF 使用经营现金流作为 FCF 代理，RIM 也采用简化口径，结果用于决策支持，不构成投资建议。",
        "",
        "你也可以继续说“WACC 改成 7%”“增长率改成 4%”，后续可扩展为即时重算。",
    ]
    return "\n".join(lines)

def _local_smalltalk_reply(message: str):
    """
    拦截明显闲聊，避免无意义调用 Coze。
    只拦截非常明确的闲聊/离题表达，金融分析和估值参数仍继续交给工作流。
    """
    text = re.sub(r"\s+", "", (message or "").strip().lower())
    text = re.sub(r"[，。！？!?、,.~～…]+$", "", text)

    if not text:
        return "请输入问题，或上传上市公司年报 PDF。"

    exact_smalltalk = {
        "你好", "您好", "嗨", "哈喽", "hello", "hi", "hey",
        "在吗", "在不在", "有人吗", "哈哈", "哈哈哈", "呵呵",
        "谢谢", "感谢", "谢了", "再见", "拜拜", "晚安", "早上好",
        "中午好", "下午好", "晚上好", "你是谁", "你叫什么",
        "你叫什么名字", "吃了吗", "吃饭了吗"
    }

    if text in exact_smalltalk:
        return "你好。我目前专注于上市公司年报分析与估值支持，请上传年报 PDF，或继续提供 WACC、增长率、ROE、股价等分析参数。"

    off_topic_patterns = (
        r"天气(怎么样|如何|预报)?$",
        r"今天天气",
        r"明天天气",
        r"讲(一个|个)?笑话",
        r"说(一个|个)?笑话",
        r"唱(一首|首)?歌",
        r"写(一首|首)?诗$",
        r"陪我聊天",
        r"随便聊聊",
    )

    if any(re.search(pattern, text) for pattern in off_topic_patterns):
        return "这个版本暂不处理与金融投研无关的闲聊。请上传上市公司年报 PDF，或询问年报数据、财务指标和估值参数。"

    return None

def _normalize_reply(out: dict, session_id: str) -> dict:
    reply = (
        out.get("reply")
        or out.get("message")
        or out.get("answer")
        or "分析已完成，但未返回文本回复。"
    )
    return {
        "session_id": session_id,
        "reply": reply,
        "document_identity": out.get("document_identity", ""),
        "agent1_cached": out.get("agent1_cached", False),
        "markdown_report": out.get("markdown_report", ""),
        "raw": out,
    }


@app.post("/api/chat")
def chat():
    user_message = (request.form.get("message") or "").strip()
    session_id = (
        (request.form.get("session_id") or "").strip()
        or uuid.uuid4().hex
    )
    request_full_report = (
        (request.form.get("request_full_report") or "false").lower() == "true"
    )

    attachment = None
    file = request.files.get("file")

    if file and file.filename:
        original = file.filename
        ext = Path(original).suffix.lower()

        if ext != ".pdf":
            return jsonify({"error": "目前仅支持 PDF 年报。"}), 400

        # 先查静态缓存。命中时不保存 PDF、不调用 Coze、不创建异步任务。
        cache_key = _normalize_pdf_cache_key(original)
        cached = _load_report_cache().get(cache_key)

        if isinstance(cached, dict):
            # 缓存命中后，不再停在 required_inputs。
            # 直接用确定性规则自动补齐估值假设，并输出 DCF/RIM/敏感性结果。
            valuation = _auto_valuation_from_cache(cached)
            out = dict(cached)
            out["agent1_cached"] = True
            out["auto_valuation"] = valuation
            out["reply"] = _format_auto_valuation_reply(cached, valuation)

            normalized = _normalize_reply(out, session_id)
            normalized.update({
                "async": False,
                "cache_hit": True,
                "cache_key": cache_key,
                "auto_valuation": True,
            })
            return jsonify(normalized)

        # 未命中缓存，才真正保存 PDF 并交给 Coze 异步解析。
        if not COZE_API_TOKEN:
            return jsonify({"error": "服务器尚未配置 COZE_API_TOKEN。"}), 500

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

    # 没有上传文件时，先在 Render 本地拦截明显闲聊。
    # 命中时不会调用 Coze，因此不会产生这次聊天的模型积分消耗。
    if attachment is None:
        local_reply = _local_smalltalk_reply(user_message)
        if local_reply is not None:
            return jsonify({
                "session_id": session_id,
                "reply": local_reply,
                "async": False,
                "local_guard": True,
                "cache_hit": False,
                "agent1_cached": False,
                "document_identity": "",
                "markdown_report": "",
            })

    # 不是明显闲聊：继续交给 Coze 处理金融问题、参数补充和正常多轮对话。
    if attachment is None and not COZE_API_TOKEN:
        return jsonify({"error": "服务器尚未配置 COZE_API_TOKEN。"}), 500

    payload = {
        "user_message": user_message,
        "session_id": session_id,
        "request_full_report": request_full_report,
    }

    if attachment is not None:
        payload["attachment"] = attachment

    # 新 PDF 解析可能耗时很长：使用 Coze 部署工作流的异步接口，
    # 立即返回 task_id，让浏览器轮询任务状态，避免 Render/Gunicorn 超时。
    if attachment is not None:
        try:
            resp = requests.post(
                COZE_ASYNC_URL,
                headers=_headers(),
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            return jsonify({"error": f"启动年报分析失败：{exc}"}), 502

        data, parse_error = _json_or_error(resp)
        if parse_error:
            return jsonify(parse_error), 502

        if not resp.ok:
            return jsonify({
                "error": "启动年报分析任务失败。",
                "detail": data,
            }), resp.status_code

        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not task_id and isinstance(data, dict) and isinstance(data.get("data"), dict):
            task_id = data["data"].get("task_id")

        if not task_id:
            return jsonify({
                "error": "Coze 已接受请求，但没有返回 task_id。",
                "detail": data,
            }), 502

        return jsonify({
            "session_id": session_id,
            "async": True,
            "task_id": task_id,
            "status": (
                data.get("status", "pending")
                if isinstance(data, dict)
                else "pending"
            ),
            "reply": "年报已上传，正在解析和分析。这个过程可能需要较长时间，请保持页面打开。",
        })

    # 没有上传新 PDF 的普通多轮聊天/补参数走同步接口，通常很快。
    try:
        resp = requests.post(
            COZE_API_URL,
            headers=_headers(),
            json=payload,
            timeout=90,
        )
    except requests.RequestException as exc:
        return jsonify({"error": f"调用分析服务失败：{exc}"}), 502

    data, parse_error = _json_or_error(resp)
    if parse_error:
        return jsonify(parse_error), 502

    if not resp.ok:
        return jsonify({
            "error": "分析服务返回错误。",
            "detail": data,
        }), resp.status_code

    out = _unwrap_output(data)
    return jsonify(_normalize_reply(out, session_id))


@app.get("/api/task/<task_id>")
def task_status(task_id: str):
    if not COZE_API_TOKEN:
        return jsonify({"error": "服务器尚未配置 COZE_API_TOKEN。"}), 500

    session_id = (request.args.get("session_id") or "").strip()

    try:
        resp = requests.get(
            f"{COZE_TASK_URL}/{task_id}",
            headers={"Authorization": f"Bearer {COZE_API_TOKEN}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return jsonify({"error": f"查询分析任务失败：{exc}"}), 502

    data, parse_error = _json_or_error(resp)
    if parse_error:
        return jsonify(parse_error), 502

    if not resp.ok:
        return jsonify({
            "error": "查询分析任务失败。",
            "detail": data,
        }), resp.status_code

    # 兼容少数可能的 data 包装
    task = data
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        task = data["data"]

    if not isinstance(task, dict):
        return jsonify({
            "error": "异步任务返回格式异常。",
            "detail": data,
        }), 502

    status = str(task.get("status") or "").lower()

    if status in {"pending", "running", ""}:
        return jsonify({
            "status": status or "running",
            "task_id": task_id,
        })

    if status in {"failed", "timeout"}:
        return jsonify({
            "status": status,
            "task_id": task_id,
            "error": task.get("error") or f"任务状态：{status}",
        })

    if status != "completed":
        return jsonify({
            "status": status,
            "task_id": task_id,
            "raw": task,
        })

    result = task.get("result")
    out = _unwrap_output(result)
    normalized = _normalize_reply(out, session_id)
    normalized.update({
        "status": "completed",
        "task_id": task_id,
    })
    return jsonify(normalized)


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "PDF 超过上传限制，请压缩后再上传。"}), 413


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
