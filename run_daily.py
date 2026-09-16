# -*- coding: utf-8 -*-
"""本机闹钟：每交易日 14:05 由 Windows 计划任务（FundWatch）调用。

流程：加载 .claude/settings.json 里的 AI/推送密钥（与手动运行同一份配置）
→ 抓数据 → AI 报告 → 推微信 → 把当日报告提交回仓库。
云端定时任务看到仓库里"当日报告已存在"会自动跳过，所以不会重复推送。
电脑关机时任务不执行，云端会照常发详细兜底版，不误事。
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
LOG = ROOT / "run_daily.log"


def log(msg):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}\n")


def load_env():
    """从 .claude/settings.json 读取 env 配置（密钥不进代码）"""
    cfg_path = ROOT / ".claude" / "settings.json"
    if not cfg_path.exists():
        log("ERROR 未找到 .claude/settings.json")
        sys.exit(1)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for key, value in (cfg.get("env") or {}).items():
        if value:
            os.environ[key] = str(value)


def run(cmd):
    try:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", timeout=120)
    except subprocess.TimeoutExpired:
        return None


def sync_repo():
    """以本机AI版报告为准同步仓库：先提交→软重置到远端最新→重做提交→推送（带重试）。
    不用 rebase 是因为"当日报告"云端兜底版与本机AI版必然冲突，软重置可避免。"""
    if run(["git", "fetch", "origin"]) is None or run(["git", "fetch", "origin"]).returncode != 0:
        return False
    run(["git", "add", "reports/", "data/"])
    run(["git", "commit", "-m", f"daily report {datetime.now(TZ):%Y-%m-%d}"])
    r = run(["git", "reset", "--soft", "FETCH_HEAD"])
    if r is None or r.returncode != 0:
        return False
    run(["git", "commit", "-m", f"daily report {datetime.now(TZ):%Y-%m-%d}（本机AI版）"])
    for _ in range(3):
        r = run(["git", "push", "origin", "main"])
        if r is not None and r.returncode == 0:
            return True
    return False


def main():
    if "--check" in sys.argv:          # 只验证密钥加载，不跑任何流程
        load_env()
        keys = [k for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                            "ANTHROPIC_AUTH_TOKEN", "PUSHPLUS_TOKEN") if os.environ.get(k)]
        print("已加载密钥字段：", "、".join(keys))
        sys.exit(0)

    load_env()
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if (ROOT / "reports" / f"{today}.md").exists():
        log(f"{today} 报告已存在，跳过（防止重复推送）")
        sys.exit(0)

    python = sys.executable
    r = run([python, "scripts/fetch_data.py"])
    log("fetch: " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else f"exit={r.returncode}"))
    if r.returncode != 0:
        log("ERROR fetch 失败，本次结束")
        sys.exit(1)
    r = run([python, "scripts/analyze.py"])
    log("analyze: " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else f"exit={r.returncode}"))
    if sync_repo():
        log("git: 同步成功")
    else:
        log("WARN git: 推送失败(重试3次)，云端可能再发一条兜底版")
    log("本次结束")


if __name__ == "__main__":
    main()
