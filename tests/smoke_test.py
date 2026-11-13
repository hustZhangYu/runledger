"""冒烟测试：不依赖任何第三方库。

用法：
    python tests/smoke_test.py

在临时目录中使用演示数据，不读写个人台账。
"""
import os
import shutil
import sys
import tempfile
import atexit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import helpers  # noqa: E402  必须在 sys.path 设置之后导入

sandbox = tempfile.TemporaryDirectory(prefix="runledger-smoke-")
atexit.register(sandbox.cleanup)
helpers.DATA_FILE = os.path.join(sandbox.name, "runs.json")
shutil.copy(os.path.join(ROOT, "runs.example.json"), helpers.DATA_FILE)

failures = []


def check(name, fn):
    try:
        fn()
        print(f"  ok  {name}")
    except AssertionError as e:
        failures.append(f"{name}: {e}")
        print(f"  !!  {name}: {e}")


def t_duration():
    assert helpers.parse_duration("30m") == 30
    assert helpers.parse_duration("2h") == 120
    assert helpers.parse_duration("1h30m") == 90
    assert helpers.parse_duration("90") == 90
    assert helpers.parse_duration("2.5h") == 150
    assert helpers.parse_duration("乱填") == 0
    assert helpers.parse_duration(None) == 0


def t_load():
    runs = helpers.load_runs()
    assert isinstance(runs, list)
    assert runs, "演示数据为空"


def t_recompute():
    runs = helpers.load_runs()
    helpers.recompute(runs)
    for r in runs:
        assert r.get("status") in helpers.STATUSES, f"非法状态 {r.get('status')}"


def t_enrich():
    runs = helpers.load_runs()
    for r in runs:
        e = helpers.enrich(r)
        assert "remaining_min" in e and "overdue_min" in e
        assert e["start_at"] == e.get("created_at"), "开始时间应恒等于创建时间"
        assert "elapsed_min" in e


def t_projects():
    runs = helpers.load_runs()
    p = helpers.collect_projects(runs)
    assert p, "没提取到项目名"
    assert len(p) == len(set(p)), "项目名有重复"


def t_new_id():
    runs = helpers.load_runs()
    nid = helpers.new_id(runs)
    assert nid not in {r["id"] for r in runs}, f"新 id 与已有冲突: {nid}"
    assert nid.startswith("r") and len(nid) == 5, f"id 格式不对: {nid}"


print(f"Python {sys.version.split()[0]} · 工作目录 {ROOT}")
print("开始冒烟测试：")
check("时长解析 parse_duration", t_duration)
check("台账读取 load_runs", t_load)
check("状态重算 recompute", t_recompute)
check("派生字段 enrich", t_enrich)
check("项目名动态汇总 collect_projects", t_projects)
check("新 id 生成 new_id", t_new_id)

if failures:
    print(f"\n失败 {len(failures)} 项：")
    for f in failures:
        print("  -", f)
    sys.exit(1)

print("\n全部通过")
