"""RunLedger 后端：纯标准库 http.server 实现，零第三方依赖、离线可用。

启动：python app.py  ->  http://127.0.0.1:7860
每次请求都重读 runs.json 并 recompute，保证 Web 与提醒脚本看到一致的「待看结果」状态。
"""
from __future__ import annotations

import json
import os
import re
import threading
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import helpers

HOST = os.environ.get("RT_HOST", "127.0.0.1")
PORT = int(os.environ.get("RT_PORT", "7860"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
WRITE_LOCK = threading.Lock()


def _send_json(handler, obj, status=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler):
    try:
        length = int(handler.headers.get("Content-Length", 0) or 0)
    except ValueError:
        raise ValueError("Content-Length 必须是整数")
    if not 0 <= length <= 1024 * 1024:
        raise ValueError("请求体不得超过 1 MiB")
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ValueError("请求体必须是有效的 JSON 对象")
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    for key, value in body.items():
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串或 null")
    return body


def _transaction(fn):
    """保护完整的读取、修改、保存过程，避免并发请求丢失更新。"""
    @wraps(fn)
    def locked(*args, **kwargs):
        with WRITE_LOCK:
            return fn(*args, **kwargs)
    return locked


def _find(runs, rid):
    for r in runs:
        if r.get("id") == rid:
            return r
    return None


def _resolve_parent(runs, pid, child_id=None):
    """校验 parent_id：必须是已存在的「待办项」，且不能形成环（parent 不能是 child 的后代）。否则返回空（不隶属）。"""
    pid = (pid or "").strip()
    if not pid or pid == child_id:
        return ""
    p = _find(runs, pid)
    if not p or p.get("type") != "待办项":
        return ""
    if child_id:  # 防环：沿 parent 链向上找，若遇到 child 本身则拒绝
        seen = set()
        cur = p.get("parent_id")
        while cur and cur not in seen:
            if cur == child_id:
                return ""
            seen.add(cur)
            cur = next((r.get("parent_id") for r in runs if r.get("id") == cur), None)
    return pid


# ---------- 业务逻辑 ----------

def api_list():
    runs = helpers.load_runs()
    helpers.recompute(runs)
    now = helpers.now_dt()
    return {"runs": [helpers.enrich(r, now) for r in runs],
            "projects": helpers.collect_projects(runs)}


@_transaction
def api_create(body):
    runs = helpers.load_runs()
    task = (body.get("task") or "").strip()
    if not task:
        return {"error": "task 不能为空"}, 400
    rtype = body.get("type") or "运行任务"
    if rtype not in helpers.TYPES:
        rtype = "运行任务"
    if rtype == "运行任务":
        est_label = (body.get("est_label") or "").strip()
        est_min = helpers.parse_duration(est_label)
        if est_min <= 0:
            return {"error": "est_label 无法解析为正时长（如 30m / 2h / 1h30m）"}, 400
        status0, est_lbl, est_m, st = "待提交", est_label, est_min, None
        parent_id = _resolve_parent(runs, body.get("parent_id"))
    else:  # 待办项：无运行时长、无提交时间，可隶属另一个待办项
        status0, est_lbl, est_m, st = "待办", "", 0, None
        parent_id = _resolve_parent(runs, body.get("parent_id"))
    rec = {
        "id": helpers.new_id(runs),
        "type": rtype,
        "task": task,
        "project": (body.get("project") or "").strip() or helpers.DEFAULT_PROJECT,
        "action": (body.get("action") or "").strip(),
        "job_id": (body.get("job_id") or "").strip(),
        "parent_id": parent_id,
        "est_label": est_lbl,
        "est_minutes": est_m,
        "submit_time": st,
        "done_at": None,
        "status": status0,
        "result_note": "",
        "created_at": helpers.now_iso(),
        "updated_at": helpers.now_iso(),
    }
    runs.append(rec)
    helpers.save_runs(runs)
    return {"run": helpers.enrich(rec)}, 201


@_transaction
def api_update(rid, body):
    runs = helpers.load_runs()
    rec = _find(runs, rid)
    if not rec:
        return {"error": "未找到该任务"}, 404
    for k in ("task", "project", "action", "job_id", "type"):
        if k in body and body[k] is not None:
            v = str(body[k]).strip()
            if k == "type" and v not in helpers.TYPES:
                continue
            rec[k] = v
    if not rec.get("task"):
        return {"error": "task 不能为空"}, 400
    if rec.get("type") == "运行任务" and any(r.get("parent_id") == rid for r in runs):
        return {"error": "含有子任务的待办项不能改为运行任务"}, 400
    final_type = "运行任务" if rec.get("type") == "运行任务" else "待办项"
    if final_type == "运行任务":
        if "est_label" in body:
            est_label = str(body["est_label"]).strip()
            if est_label:  # 空字符串表示"不修改"，保留原值（避免改父节点/改其它字段时误清空或误报错）
                est_min = helpers.parse_duration(est_label)
                if est_min <= 0:
                    return {"error": "est_label 无法解析为正时长（如 30m / 2h / 1h30m）"}, 400
                rec["est_label"] = est_label
                rec["est_minutes"] = est_min
    else:  # 待办项：无运行时长，强制清空
        rec["est_label"] = ""
        rec["est_minutes"] = 0
    if rec.get("type") == "运行任务" and not rec.get("est_minutes"):
        return {"error": "运行任务需要填预估时长（如 30m / 2h / 1h30m）"}, 400
    if "parent_id" in body:
        rec["parent_id"] = _resolve_parent(runs, body.get("parent_id"), rec.get("id"))
    if "status" in body and body["status"] in helpers.STATUSES:
        rec["status"] = body["status"]
    # 类型与状态一致性：待办项只允许 待办/已完成
    if rec.get("type") == "待办项" and rec.get("status") not in ("待办", "已完成"):
        rec["status"] = "待办"
    if rec.get("type") == "运行任务" and rec.get("status") == "待办":
        rec["status"] = "待提交"
    # 状态与时间标签联动：已完成 → 记完成时间；从已完成改回 → 清空完成时间
    if rec.get("status") == "已完成":
        if not rec.get("done_at"):
            rec["done_at"] = helpers.now_iso()
    else:
        rec["done_at"] = None
    rec["updated_at"] = helpers.now_iso()
    helpers.save_runs(runs)
    return {"run": helpers.enrich(rec)}, 200


def _collect_subtree(runs, rid):
    """收集 rid 及其所有后代 id（子待办、挂的运行任务，任意层级）。"""
    ids = {rid}
    changed = True
    while changed:
        changed = False
        for r in runs:
            if r.get("parent_id") in ids and r.get("id") not in ids:
                ids.add(r.get("id"))
                changed = True
    return ids


@_transaction
def api_delete(rid):
    """删除一条任务；若它有待办子节点/挂的运行任务，一并级联删除（避免留下孤儿）。"""
    runs = helpers.load_runs()
    if not _find(runs, rid):
        return {"error": "未找到该任务"}, 404
    victims = _collect_subtree(runs, rid)
    new = [r for r in runs if r.get("id") not in victims]
    helpers.save_runs(new)
    return {"ok": True, "deleted": len(victims), "ids": sorted(victims)}, 200


@_transaction
def api_submit(rid):
    runs = helpers.load_runs()
    rec = _find(runs, rid)
    if not rec:
        return {"error": "未找到该任务"}, 404
    if not rec.get("est_minutes"):
        return {"error": "缺少预估时长，无法提交"}, 400
    if rec.get("type") != "运行任务" or not rec.get("est_minutes"):
        return {"error": "只有填写预估时长的运行任务可以提交或重跑"}, 400
    now = helpers.now_iso()
    rec["status"] = "运行中"
    rec["submit_time"] = now
    rec["done_at"] = None
    rec["updated_at"] = now
    helpers.save_runs(runs)
    return {"run": helpers.enrich(rec)}, 200


@_transaction
def api_check(rid, body):
    runs = helpers.load_runs()
    rec = _find(runs, rid)
    if not rec:
        return {"error": "未找到该任务"}, 404
    now = helpers.now_iso()
    rec["status"] = "已完成"
    rec["result_note"] = (body.get("result_note") or "").strip()
    rec["done_at"] = now
    rec["updated_at"] = now
    helpers.save_runs(runs)
    return {"run": helpers.enrich(rec)}, 200


@_transaction
def api_rerun(rid):
    runs = helpers.load_runs()
    rec = _find(runs, rid)
    if not rec:
        return {"error": "未找到该任务"}, 404
    if rec.get("type") != "运行任务" or not rec.get("est_minutes"):
        return {"error": "只有填写预估时长的运行任务可以提交或重跑"}, 400
    now = helpers.now_iso()
    rec["status"] = "运行中"
    rec["submit_time"] = now
    rec["done_at"] = None
    rec["updated_at"] = now
    helpers.save_runs(runs)
    return {"run": helpers.enrich(rec)}, 200


@_transaction
def api_reopen(rid):
    """重开：回到初始未开始状态。运行任务→待提交(清空提交时间)；待办项→待办。"""
    runs = helpers.load_runs()
    rec = _find(runs, rid)
    if not rec:
        return {"error": "未找到该任务"}, 404
    if rec.get("type") == "待办项":
        rec["status"] = "待办"
    else:
        rec["status"] = "待提交"
        rec["submit_time"] = None
    rec["done_at"] = None
    rec["updated_at"] = helpers.now_iso()
    helpers.save_runs(runs)
    return {"run": helpers.enrich(rec)}, 200


def api_summary():
    runs = helpers.load_runs()
    helpers.recompute(runs)
    now = helpers.now_dt()
    counts = {s: 0 for s in helpers.STATUSES}
    for r in runs:
        counts[r.get("status", "待提交")] = counts.get(r.get("status", "待提交"), 0) + 1
    # 本周完成数
    week_start = now - helpers.timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    done_week = 0
    for r in runs:
        # 只统计顶层（无父节点）的完成项；子待办/挂的运行任务不重复计入
        if r.get("status") == "已完成" and not r.get("parent_id"):
            # 优先用 done_at（可靠完成时间）；老数据没有则回退 updated_at
            ua = helpers.parse_dt(r.get("done_at")) or helpers.parse_dt(r.get("updated_at"))
            if ua and ua >= week_start:
                done_week += 1
    pending = [helpers.enrich(r, now) for r in runs if r.get("status") == "待看结果"]
    todo_open = [helpers.enrich(r, now) for r in runs
                 if r.get("type") == "待办项" and r.get("status") == "待办"]
    return {"counts": counts, "done_week": done_week, "pending": pending,
            "todo_open": todo_open, "todo_count": len(todo_open)}


# ---------- HTTP 处理 ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默日志，保持控制台干净

    def _serve_static(self, path):
        rel = path.lstrip("/")
        if rel == "" or rel == "index.html":
            rel = "index.html"
        full = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if os.path.commonpath([full, os.path.realpath(STATIC_DIR)]) != os.path.realpath(STATIC_DIR):
            self.send_error(403)
            return
        if not os.path.isfile(full):
            self.send_error(404)
            return
        ctype = "text/html; charset=utf-8" if full.endswith(".html") else "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 前端改完刷新即生效：禁止浏览器缓存 index.html
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._write_request(self._get)

    def _get(self):
        p = urlparse(self.path).path
        if p == "/api/runs":
            _send_json(self, api_list())
        elif p == "/api/summary":
            _send_json(self, api_summary())
        else:
            self._serve_static(p)

    def _write_request(self, method):
        try:
            method()
        except ValueError as exc:
            _send_json(self, {"error": str(exc)}, 400)
        except OSError:
            _send_json(self, {"error": "无法读写台账，请检查文件和权限"}, 500)

    def do_POST(self):
        self._write_request(self._post)

    def do_PUT(self):
        self._write_request(self._put)

    def do_DELETE(self):
        self._write_request(self._delete)

    def _post(self):
        p = urlparse(self.path).path
        body = _read_body(self)
        m = re.match(r"^/api/runs/([^/]+)/(submit|check|rerun|reopen)$", p)
        if p == "/api/runs":
            _send_json(self, *api_create(body))
        elif m:
            rid, act = m.group(1), m.group(2)
            if act == "submit":
                _send_json(self, *api_submit(rid))
            elif act == "check":
                _send_json(self, *api_check(rid, body))
            elif act == "rerun":
                _send_json(self, *api_rerun(rid))
            elif act == "reopen":
                _send_json(self, *api_reopen(rid))
        else:
            self.send_error(404)

    def _put(self):
        p = urlparse(self.path).path
        m = re.match(r"^/api/runs/([^/]+)$", p)
        if m:
            _send_json(self, *api_update(m.group(1), _read_body(self)))
        else:
            self.send_error(404)

    def _delete(self):
        p = urlparse(self.path).path
        m = re.match(r"^/api/runs/([^/]+)$", p)
        if m:
            _send_json(self, *api_delete(m.group(1)))
        else:
            self.send_error(404)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"RunLedger 已启动 -> http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
