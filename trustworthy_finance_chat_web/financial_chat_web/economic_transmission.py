"""证据约束的价格事件条件传导器（教学/初赛演示版）。

独立于Coze、财报缓存和DCF；所有数值计算均由Python完成。不会
修改正式估值，也不会将用户提交的模型回复当成真实原始公告。
"""
from __future__ import annotations

import math
import re
from typing import Any

_ALLOWED_INFLUENCE = {"revenue_growth", "sales_growth", "growth"}
_PRICE_EVENT = re.compile(r"提价|涨价|价格上调|价格上涨|上调.*?价格|出厂价.*?上调")
# 仅从模型引用的公告片段寻找明示的价格上调幅度，不猜测价格之外的百分比。
_PRICE_CHANGE = re.compile(
    r"(?:平均上调幅度|提价幅度|价格上调幅度|调价幅度)"
    r"\s*(?:约为|约|为|达到)?\s*(?P<value>\d{1,3}(?:\.\d{1,2})?)\s*[%％](?![A-Za-z])"
)
_PRICE_WITH_VERB = re.compile(
    r"(?:出厂价(?:格)?|售价|价格)[^。；;\n]{0,20}?"
    r"(?:上调|上涨|提高|提升|增加)\s*(?:约为|约|了|为)?\s*"
    r"(?P<value>\d{1,3}(?:\.\d{1,2})?)\s*[%％](?![A-Za-z])"
)
_REVERSE_PRICE = re.compile(
    r"(?:上调|上涨|提价|涨价)\s*(?:约为|约|了|为)?\s*"
    r"(?P<value>\d{1,3}(?:\.\d{1,2})?)\s*[%％](?![A-Za-z])"
)



def _field(text: str, key: str) -> str:
    m = re.search(rf"^\s*{re.escape(key)}\s*[：:]\s*(.*?)\s*$", text, re.M)
    return m.group(1).strip()[:1800] if m else ""


def _number(value: Any, name: str, minimum: float, maximum: float) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name}必须为数字")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}必须为数字") from None
    if not math.isfinite(v) or not minimum <= v <= maximum:
        raise ValueError(f"{name}必须在{minimum:g}至{maximum:g}之间")
    return v


def _model_extracted_price(evidence: str) -> float | None:
    """只有匹配到价格相关措辞才提取数值；该提取不等于真实性核验。"""
    for pattern in (_PRICE_CHANGE, _PRICE_WITH_VERB, _REVERSE_PRICE):
        match = pattern.search(evidence)
        if match:
            v = float(match.group("value"))
            if 0 <= v <= 100:
                return v
    return None


def analyze_price_transmission(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("请求格式错误")
    reply = str(payload.get("analysis_reply") or "")
    if len(reply) > 20000 or "【公告事件分析】" not in reply:
        raise ValueError("请先完成公告分析，再查看经济传导与证据缺口")

    company = _field(reply, "公司")
    event = _field(reply, "事件")
    influence = _field(reply, "影响变量")
    evidence = _field(reply, "原文证据")
    reviewer = _field(reply, "审核状态") or "unknown"
    if not company or not event or not evidence:
        raise ValueError("公告分析缺少公司、事件或引用的原文证据")
    if influence not in _ALLOWED_INFLUENCE or not _PRICE_EVENT.search(event + " " + evidence):
        return {
            "supported": False,
            "company": company,
            "event": event,
            "reviewer_status": reviewer,
            "explanation": "目前仅对明确涉及产品价格上调、且潜在变量为营收增长的公告提供条件收入传导；其他事件仍保留原Reviewer审核。",
            "official_valuation_changed": False,
        }

    extracted_price = _model_extracted_price(evidence)
    manual_price = _number(payload.get("price_change_pct"), "产品价格变动", -50, 100)
    price = manual_price if manual_price is not None else extracted_price
    source = "人工输入（未核验假设）" if manual_price is not None else (
        "模型引用的公告片段（真实性未核验）" if price is not None else "未提供")
    share = _number(payload.get("affected_revenue_share_pct"), "受影响业务收入占比", 0, 100)
    volume_low = _number(payload.get("volume_low_pct"), "销量变化下界", -100, 200)
    volume_high = _number(payload.get("volume_high_pct"), "销量变化上界", -100, 200)
    if volume_low is not None and volume_high is not None and volume_low > volume_high:
        raise ValueError("销量变化下界不得大于上界")

    missing = []
    if price is None:
        missing.append("受影响产品的实际平均价格变动百分比")
    if share is None:
        missing.append("受影响产品占公司原总营业收入的比例")
    if volume_low is None or volume_high is None:
        missing.append("价格调整后受影响产品的销量变化区间")
    # 完全没有现金流、资本开支、税率等资料，不推导DCF目标价。
    missing.extend(["成本、税负与现金流传导依据（估值所需）", "资本开支及营运资本等FCFF项目（估值所需）"])

    result = {
        "supported": True,
        "company": company,
        "event": event,
        "evidence_excerpt": evidence,
        "reviewer_status": reviewer,
        "event_price_change_pct": price,
        "event_price_source": source,
        "affected_revenue_share_pct": share,
        "volume_low_pct": volume_low,
        "volume_high_pct": volume_high,
        "transmission_chain": ["受影响产品价格", "销量及收入权重", "公司营业收入", "成本与自由现金流", "企业估值"],
        "missing_evidence": missing,
        "calculation": None,
        "official_valuation_changed": False,
        "warnings": [
            "公告文字来自模型回复中的证据摘录，未自动核验原公告真实性。",
            "受影响收入占比及销量区间如由用户输入，仅为条件假设，非已验证事实。",
            "假设其他产品收入不变、产品组合不变；此模型未计入成本、税费、资本开支或宏观反馈。",
            "收入条件区间不等于利润或自由现金流区间，更不等于DCF估值或股价预测。",
            "结果只在本次请求中计算，不写入公司财务缓存或正式估值模型。",
        ],
    }
    if price is not None and share is not None and volume_low is not None and volume_high is not None:
        if payload.get("assumptions_acknowledged") is not True:
            raise ValueError("请确认销量、收入占比等未验证数值仅用于条件情景")
        # R = R_unaffected + sR * (1+p)*(1+q)
        low = share / 100 * ((1 + price / 100) * (1 + volume_low / 100) - 1) * 100
        high = share / 100 * ((1 + price / 100) * (1 + volume_high / 100) - 1) * 100
        result["calculation"] = {
            "method": "fixed_unaffected_revenue_price_volume",
            "company_revenue_change_low_pct": round(low, 6),
            "company_revenue_change_high_pct": round(high, 6),
            "formula": "s×[(1+p)×(1+q)−1]",
            "inputs": {"s": share / 100, "p": price / 100,
                       "q_low": volume_low / 100, "q_high": volume_high / 100},
            "meaning": "在明示条件下，公司总营业收入的变化区间；不自动进入DCF。",
        }
    return result


def register_economic_transmission(app):
    """与已有Flask app独立注册，不覆盖原路由。"""
    from flask import jsonify, request

    @app.post("/api/economic_transmission")
    def economic_transmission_endpoint():
        if request.content_length and request.content_length > 24000:
            return jsonify({"error": "请求过长"}), 413
        try:
            result = analyze_price_transmission(request.get_json(silent=True))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(result)
