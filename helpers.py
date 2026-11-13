"""run_tracker 共用工具：时长解析、状态重算、JSON 读写。

后端 app.py 与对话录入 skill / 提醒脚本都复用本文件，保证数据模型一致。
仅依赖 Python 标准库，零第三方依赖。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta

# 数据文件与本文件同目录
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs.json")

# 项目名不预设、不写死：一律从台账数据里提取，用户填什么就有什么。
DEFAULT_PROJECT = "默认项目"
STATUSES = ["待提交", "运行中", "待看结果", "待办", "已完成", "异常"]
TYPES = ["运行任务", "待办项"]


def collect_projects(runs: list) -> list:
    """从台账里汇总出现过的项目名（去重、保持首次出现顺序）。

    这样新增项目不需要改代码，直接填一个新的项目名即可。
    """
    seen = []
    for r in runs:
        p = (r.get("project") or "").strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def now_dt() -> datetime:
    """本地时区的当前时间（带时区信息）。"""
    return datetime.now().astimezone()


def now_iso() -> str:
    return now_dt().isoformat(timespec="seconds")


def parse_dt(s: str | None) -> datetime | None:
    """解析 ISO 时间字符串；无时区则按本地时区处理；失败返回 None。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def parse_duration(label) -> int:
    """把人类可读时长解析成分钟（int）。

    支持：'30m' / '2h' / '1h30m' / '90'（纯数字按分钟）。
    无法解析时返回 0。
    """
    if label is None:
        return 0
    s = str(label).strip().lower()
    if not s:
        return 0
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return int(float(s))
    total = 0
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?\s*h\s*)?(?:\d+(?:\.\d+)?\s*m)?", s):
        return 0
    mh = re.search(r"(\d+(?:\.\d+)?)\s*h", s)
    mm = re.search(r"(\d+(?:\.\d+)?)\s*m", s)
    if mh:
        total += int(float(mh.group(1)) * 60)
    if mm:
        total += int(float(mm.group(1)))
    if total == 0:
        try:
            return int(float(s))
        except Exception:
            return 0
    return total


def load_runs() -> list:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
            raise OSError("台账格式错误，请从备份恢复；原文件未修改")
    except FileNotFoundError:
        data = []
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise OSError("台账无法解析，请从备份恢复；原文件未修改") from exc
    return data


def save_runs(runs: list) -> None:
    """原子写：先写临时文件再 os.replace，避免半截写坏台账。"""
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(runs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def recompute(runs: list, now: datetime | None = None) -> list:
    """把「运行中且已超预估时长」的自动翻成「待看结果」。"""
    if now is None:
        now = now_dt()
    for r in runs:
        if r.get("status") == "运行中" and r.get("submit_time") and r.get("est_minutes"):
            st = parse_dt(r["submit_time"])
            if st is None:
                continue
            end = st + timedelta(minutes=r["est_minutes"])
            if now >= end:
                r["status"] = "待看结果"
    return runs


def enrich(r: dict, now: datetime | None = None) -> dict:
    """给单条记录附加前端展示用的派生字段。"""
    if now is None:
        now = now_dt()
    r = dict(r)
    remaining = None
    overdue = None
    st = parse_dt(r.get("submit_time"))
    if st is not None and r.get("est_minutes"):
        end = st + timedelta(minutes=r["est_minutes"])
        diff = (end - now).total_seconds() / 60
        if r.get("status") == "运行中":
            if diff >= 0:
                remaining = int(diff)
            else:
                overdue = int(-diff)
        elif r.get("status") == "待看结果":
            overdue = int(max(0, -diff))
    r["remaining_min"] = remaining
    r["overdue_min"] = overdue
    # 开始时间 === 创建时间（不落库、不需要手动点，读盘时派生）
    r["start_at"] = r.get("created_at") or r.get("start_at")
    # 实际耗时：创建时间 → 完成时间
    elapsed = None
    sa = parse_dt(r["start_at"])
    da = parse_dt(r.get("done_at"))
    if sa is not None and da is not None and da >= sa:
        elapsed = int((da - sa).total_seconds() / 60)
    r["elapsed_min"] = elapsed
    return r


def new_id(runs: list) -> str:
    """生成短 id：当前最大序号 +1，形如 r0007。"""
    max_n = 0
    for r in runs:
        m = re.fullmatch(r"r(\d+)", str(r.get("id", "")))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"r{max_n + 1:04d}"
