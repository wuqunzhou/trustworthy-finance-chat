
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
