"""提醒脚本：输出当前「待看结果」的任务摘要。

供系统定时任务或个人自动化调用——直接读 runs.json，不依赖 Web 服务在线。
有超时任务时打印清单；无则打印一句 ✓，便于自动化只在有事时提醒。
"""
import sys
import helpers

runs = helpers.load_runs()
helpers.recompute(runs)
pending = [r for r in runs if r.get("status") == "待看结果"]

if not pending:
    print("✓ RunLedger：当前没有待看结果的任务")
    sys.exit(0)

print(f"⏰ RunLedger：有 {len(pending)} 条任务已到预估时间，该去看结果了：")
todo_map = {r.get("id"): r.get("task") for r in runs}
for r in pending:
    over = helpers.enrich(r).get("overdue_min") or 0
    job = r.get("job_id") or "—"
    pname = todo_map.get(r.get("parent_id")) if r.get("parent_id") else ""
    belong = f" · 隶属「{pname}」" if pname else ""
    print(f"  • [{r.get('project','')}] {r.get('task','')}{belong}（作业号 {job}，已超时约 {over} 分钟，预估 {r.get('est_label','')}）")
print("打开 http://127.0.0.1:7860 查看详情并标记完成。")
