"""Isolated, user-authorized event scenario recalculation for the financial research demo.

This module does NOT approve financial candidates, change Coze Graph state, or
change formal valuation parameters. It intentionally reuses the existing Flask
app's `_dcf_per_share` and `_auto_valuation_from_cache` functions.
"""

from __future__ import annotations

import math
import re
from typing import Callable


def _field(text: str, name: str) -> str:
    matched = re.search(rf"^\s*{re.escape(name)}\s*[：:]\s*(.*?)\s*$", text, re.M)
    return matched.group(1).strip()[:500] if matched else ""


def _normalize_company(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", str(text or "")).lower()


def _find_matching_case(cases: dict, company: str):
    requested = _normalize_company(company)
    if len(requested) < 3:
        return None, None
    matches = []
    for key, case in cases.items():
        if not isinstance(case, dict):
            continue
        name = _normalize_company(case.get("company", ""))
        if len(name) >= 3 and (name in requested or requested in name):
            matches.append((key, case))
    # Prevent accidentally using another company's cached financial figures.
    return matches[0] if len(matches) == 1 else (None, None)


def calculate_isolated_scenario(payload: dict, cases: dict,
                                calculate_auto: Callable,
                                dcf_per_share: Callable) -> dict:
    """Return a *demo-only* valuation delta; reject unmatched or uncertain inputs.

    `analysis_reply` is a user-supplied copy of the Coze result. It is *not*
    cryptographically authenticated, so do not use it as a production approval.
    """
    if not isinstance(payload, dict):
        raise ValueError("情景请求格式错误")

    reply = str(payload.get("analysis_reply") or "")
    if len(reply) > 20000 or "【公告事件分析】" not in reply:
        raise ValueError("需要先完成一次公告AI分析，才能进行隔离情景试算")

    company = _field(reply, "公司")
    event = _field(reply, "事件")
    influence = _field(reply, "影响变量")
    reviewer = _field(reply, "审核状态")
    direction = _field(reply, "方向")
    evidence = _field(reply, "原文证据")
    if not company or not event or not reviewer or not evidence:
        raise ValueError("公告分析缺少公司、事件、原文证据或审核状态，不能接入试算")

    key, cached = _find_matching_case(cases, company)
    if cached is None:
        raise ValueError("这家公司的财务基准尚未在案例缓存中，不能借用其他公司的年报数据计算")

    model = calculate_auto(cached)
    ocf = model.get("operating_cash_flow")
    shares = model.get("shares")
    assumptions = model.get("assumptions") or {}
    base_growth = assumptions.get("forecast_growth")
    wacc = assumptions.get("wacc")
    terminal_growth = assumptions.get("terminal_growth")
    years = assumptions.get("forecast_years")
    net_debt = assumptions.get("net_debt", 0)

    if not all(isinstance(x, (int, float)) and math.isfinite(x)
               for x in (ocf, shares, base_growth, wacc, terminal_growth, years, net_debt)):
        raise ValueError("缓存中的经营现金流、股份数或估值参数不完整")
    if ocf <= 0 or shares <= 0 or not (1 <= years <= 15) or not (wacc > terminal_growth):
        raise ValueError("财务基准不满足安全计算条件")

    scenario_type = str(payload.get("scenario_type", ""))
    try:
        delta = float(payload.get("delta"))
    except (ValueError, TypeError):
        raise ValueError("请输入有效的模拟冲击参数") from None
    if not math.isfinite(delta):
        raise ValueError("模拟冲击参数必须为有限数字")

    adjusted_ocf = float(ocf)
    adjusted_growth = float(base_growth)
    if scenario_type == "growth_delta_pp":
        if influence not in {"revenue_growth", "growth", "sales_growth"}:
            raise ValueError("事件影响变量并非营收增长率，不能自动映射成增长率冲击")
        if not (-3 <= delta <= 3):
            raise ValueError("增长率冲击应在 -3 至 +3 个百分点之间")
        adjusted_growth += delta / 100
        assumption_label = f"人工授权：预测增长率调整 {delta:+.2f} 个百分点"
    elif scenario_type == "ocf_delta_pct":
        if influence not in {"margin", "operating_cash_flow", "cash_flow"}:
            raise ValueError("事件影响变量不支持经营现金流冲击映射")
        if not (-15 <= delta <= 15):
            raise ValueError("现金流情景冲击应在 -15% 至 +15% 之间")
        adjusted_ocf *= 1 + delta / 100
        assumption_label = f"人工授权：经营现金流代理值调整 {delta:+.2f}%"
    else:
        raise ValueError("不支持的情景类型")

    base = dcf_per_share(ocf, shares, base_growth, wacc, terminal_growth, int(years), net_debt)
    scenario = dcf_per_share(adjusted_ocf, shares, adjusted_growth, wacc,
                             terminal_growth, int(years), net_debt)
    if base is None or scenario is None or base <= 0 or not all(map(math.isfinite, (base, scenario))):
        raise ValueError("当前参数无法计算稳定的简化DCF情景")

    difference = scenario - base
    return {
        "mode": "isolated_demo_only",
        "company": cached.get("company"),
        "cache_key": key,
        "financial_audit_status": str(cached.get("audit_status", "unknown")),
        "event": {"title": event, "direction": direction,
                  "impact_variable": influence, "reviewer_status": reviewer,
                  "evidence": evidence},
        "calculation": {
            "method": "ocf_simplified_dcf",
            "base_per_share": round(base, 4),
            "scenario_per_share": round(scenario, 4),
            "delta_per_share": round(difference, 4),
            "delta_percent": round(100 * difference / base, 4),
            "original_growth_percent": round(100 * base_growth, 3),
            "scenario_growth_percent": round(100 * adjusted_growth, 3),
            "wacc_percent": round(100 * wacc, 3),
            "terminal_growth_percent": round(100 * terminal_growth, 3),
            "forecast_years": int(years),
            "input_delta": delta,
            "scenario_type": scenario_type,
            "assumption_note": assumption_label,
        },
        "official_valuation_changed": False,
        "warnings": [
            "本次试算不修改任何正式估值参数；事件审核文本不等于准许入模。",
            "财务基准来自静态年报解析缓存，当前未完成独立真实性与口径审核，不构成正式估值。",
            "经营现金流仅作FCF简化代理，未扣除资本开支，非标准FCFF。",
            "情景冲击数值由用户填写，不是LLM根据公告自动量化的影响。",
            "公告与年报可能存在跨期（例如2023年事件与2025年财报）；仅为方法演示，不是历史回测或真实事件因果测算。",
        ],
    }


def register_scenario(app, load_cache: Callable,
                      calculate_auto: Callable,
                      dcf_per_share: Callable):
    """Register one isolated demo endpoint without changing existing routes."""
    from flask import jsonify, request

    @app.post("/api/scenario")
    def isolated_event_scenario():
        if request.content_length and request.content_length > 30000:
            return jsonify({"error": "分析文本过长"}), 413
        data = request.get_json(silent=True)
        try:
            result = calculate_isolated_scenario(
                data, load_cache(), calculate_auto, dcf_per_share
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 422
        return jsonify(result)
