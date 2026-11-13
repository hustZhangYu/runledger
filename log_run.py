"""对话录入：把一条任务追加进 runs.json。

用法（命令行参数或标准输入）：
  python log_run.py '{"type":"运行任务","task":"...","project":"我的项目","est_label":"2h","action":"...","job_id":"...","parent_id":"r0001"}'
  python log_run.py '{"type":"待办项","task":"整理交接文档","project":"我的项目","action":"..."}'
与 Web 应用共用数据格式；执行前请停止 Web 服务，避免跨进程同时写入。
"""
import sys
import json
import helpers

raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
try:
    body = json.loads(raw)
except Exception as e:
    print(f"ERROR: JSON 解析失败 - {e}")
    sys.exit(1)

if not isinstance(body, dict) or any(v is not None and not isinstance(v, str) for v in body.values()):
    print("ERROR: 输入必须是 JSON 对象，字段值须为字符串或 null")
    sys.exit(1)

task = (body.get("task") or "").strip()
if not task:
    print("ERROR: task 不能为空")
    sys.exit(1)

rtype = body.get("type") or "运行任务"
if rtype not in helpers.TYPES:
    rtype = "运行任务"

runs = helpers.load_runs()

# 隶属校验：父必须是已存在的「待办项」（运行任务和子待办都可挂）
parent_id = ""
pid = (body.get("parent_id") or "").strip()
if pid:
    p = next((r for r in runs if r.get("id") == pid), None)
    if p and p.get("type") == "待办项":
        parent_id = pid
    else:
        print(f"ERROR: parent_id {pid} 不是有效的待办项，请先创建该待办")
        sys.exit(1)

if rtype == "运行任务":
    est_min = helpers.parse_duration(body.get("est_label"))
    if est_min <= 0:
        print("ERROR: 运行任务需要 est_label（如 30m / 2h / 1h30m）")
        sys.exit(1)
    est_label, status0 = (body.get("est_label") or "").strip(), "待提交"
else:  # 待办项：无时长、无提交时间，可挂为子待办
    est_label, est_min, status0 = "", 0, "待办"

rec = {
    "id": helpers.new_id(runs),
    "type": rtype,
    "task": task,
    "project": (body.get("project") or "").strip() or helpers.DEFAULT_PROJECT,
    "action": (body.get("action") or "").strip(),
    "job_id": (body.get("job_id") or "").strip(),
    "parent_id": parent_id,
    "est_label": est_label,
    "est_minutes": est_min,
    "submit_time": None,
    "done_at": None,
    "status": status0,
    "result_note": "",
    "created_at": helpers.now_iso(),
    "updated_at": helpers.now_iso(),
}
runs.append(rec)
helpers.save_runs(runs)
if rtype == "运行任务":
    extra = f"（隶属 {parent_id}）" if parent_id else ""
    print(f"已记录运行任务：{rec['id']} · {rec['task']}（状态：待提交，预估 {rec['est_label']}{extra}）")
    print("提示：实际提交到集群后，打开 http://127.0.0.1:7860 点「提交」即可开始倒计时。")
else:
    print(f"已记录待办项：{rec['id']} · {rec['task']}（状态：待办）")
