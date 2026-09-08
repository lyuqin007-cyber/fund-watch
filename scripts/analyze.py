# -*- coding: utf-8 -*-
"""基金盯盘 · 分析与推送层
读取 data/data.json → 调 Claude Sonnet 5 生成教学式报告 → PushPlus 推微信 → 存档 reports/。

测试参数：
  --dry-run   只组装并打印提示词，不调 AI、不发微信、不花钱
  --no-push   调 AI 并生成报告存档，但不发微信
  --force     忽略休市判断（配合手动测试）
密钥来源：环境变量 ANTHROPIC_API_KEY / PUSHPLUS_TOKEN

容灾链：调 AI 失败 → 纯规则兜底报告（标注"AI暂不可用"）→ 照常推送，用户每天都能收到。
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_SH = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "data.json"
TEMPLATE_PATH = ROOT / "prompt_template.md"
FUNDS_CFG_PATH = ROOT / "funds.json"
TOPICS_PATH = ROOT / "data" / "taught_topics.json"
REPORTS_DIR = ROOT / "reports"

MODEL = "claude-sonnet-5"

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


# ---------------- 提示词组装 ----------------
def load_taught_topics():
    if TOPICS_PATH.exists():
        try:
            return json.loads(TOPICS_PATH.read_text(encoding="utf-8")).get("taught", [])
        except (ValueError, OSError):
            return []
    return []


def save_taught_topics(taught):
    TOPICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOPICS_PATH.write_text(json.dumps({"taught": taught}, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def build_system_prompt(data):
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    taught = load_taught_topics()
    taught_text = "、".join(taught) if taught else "（无，所有主题均可选）"
    replacements = {
        "{TAUGHT_TOPICS}": taught_text,
        "{GOAL}": str(data.get("goal") or "回本退场"),
        "{RESERVE_FUND}": str(data.get("reserve_fund") or "未配置"),
        "{DATE}": data["meta"]["date"],
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def build_user_prompt(data):
    """把 data.json 摘要成紧凑的每日数据块（省 token 的关键）"""
    lines = [f"今天是 {data['meta']['date']} {data['meta']['fetch_time']}（北京时间），"
             f"开市状态：{data['meta']['open_reason']}。"]
    # 大盘
    lines.append("\n【大盘与板块行情】")
    board = data.get("board", {})
    if board:
        for secid, b in board.items():
            sign = "+" if (b.get("pct") or 0) >= 0 else ""
            lines.append(f"- {b['name']}（{secid}）：{b['price']}，{sign}{b['pct']}%")
    else:
        lines.append("- 行情数据缺失")
    # 持仓
    lines.append("\n【持仓数据】（估算涨跌=对应板块ETF实时涨跌的近似值）")
    for f in data.get("funds", []):
        if f.get("missing"):
            lines.append(f"- {f['code']}：数据暂缺")
            continue
        parts = [f"{f['name']}（{f['code']}）",
                 f"最新净值 {f['latest_nav']}（{f['latest_date']}）"]
        if f.get("est_pct") is not None:
            parts.append(f"今日估算涨跌 {f['est_pct']}%（据{f.get('board_name')}）")
        else:
            parts.append("今日估算涨跌：缺失")
        if f.get("profit_pct") is not None:
            parts.append(f"累计收益率 {f['profit_pct']}%")
            parts.append(f"距回本还需涨 {f['to_breakeven_pct']}%")
            if f.get("profit_acc_pct") is not None:
                parts.append(f"含分红口径收益率 {f['profit_acc_pct']}%")
        else:
            parts.append("投入/份额未配置，无收益计算")
        trends = [t for t, v in [("近5日", f.get("trend_5d")), ("近20日", f.get("trend_20d")),
                                 ("近60日", f.get("trend_60d"))] if v is not None]
        if trends:
            parts.append("；".join(f"{t} {v}%" for t, v in
                                   zip(["近5日", "近20日", "近60日"],
                                       [f.get("trend_5d"), f.get("trend_20d"), f.get("trend_60d")])
                                   if v is not None))
        eva = f.get("eva")
        if eva and eva.get("pe_percentile") is not None:
            parts.append(f"对应指数 {eva['index_name']}（{eva['index']}）"
                         f"PE分位 {round(eva['pe_percentile'] * 100, 1)}%（{eva['level']}）")
        elif eva is None and f.get("danjuan_index"):
            parts.append("对应指数估值：缺失")
        # 距离成本跌幅（规则1用）
        if f.get("profit_pct") is not None:
            drop = -f["profit_pct"]
            parts.append(f"距成本跌幅 {round(drop, 2)}%")
        lines.append("- " + "｜".join(parts))
    # 配置信息
    lines.append(f"\n【用户配置】目标：{data.get('goal')}；预留补仓资金：{data.get('reserve_fund')} 元。")
    # 数据警告
    if data.get("warnings"):
        lines.append("\n【数据警告（可在报告中提及）】")
        for w in data["warnings"]:
            lines.append(f"- {w}")
    lines.append("\n请严格按照系统提示中的硬规则、策略规则和报告模板，生成今天的基金盯盘报告。")
    return "\n".join(lines)


# ---------------- AI 调用 ----------------
def call_ai(system, user):
    """调用 Claude Sonnet 5。注意：不传 temperature、不用 prefill（会报400）"""
    import anthropic
    client = anthropic.Anthropic(timeout=60.0, max_retries=3)
    last_text = ""
    for attempt in range(4):          # 代理偶发断连/空内容：整体重试4次，逐次延长等待
        for max_tokens in (2500, 4000):   # 若截断，加大上限再试一次
            try:
                resp = client.messages.create(
                    model=MODEL,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_config={"effort": "medium"},
                )
            except Exception:
                time.sleep(2)
                continue              # 连接错误等：不直接放弃，换下一轮重试
            text = "".join(block.text for block in resp.content if block.type == "text")
            if text.strip():
                last_text = text
            if resp.stop_reason != "max_tokens":
                if text.strip():
                    return text
                break                # 空内容：等待后整体重试
        time.sleep(2 + 3 * attempt)
    if last_text.strip():
        return last_text
    raise RuntimeError("AI 返回空内容（已重试4次）")


def generate_report(data, dry_run=False):
    """主链路：AI 生成报告；失败时返回纯规则兜底报告。dry_run 只返回组装好的提示词"""
    system = build_system_prompt(data)
    user = build_user_prompt(data)
    if dry_run:
        print("=" * 60)
        print("【系统提示词（已替换占位符）】")
        print(system)
        print("=" * 60)
        print("【用户消息（每日数据）】")
        print(user)
        print("=" * 60)
        print(f"提示词总长度约 {len(system) + len(user)} 字符，dry-run 结束（未调AI、未推送）。")
        return None, None
    try:
        return call_ai(system, user), None
    except Exception as exc:  # 任何 AI 异常都走兜底，用户每天都能收到报告
        return None, f"AI调用失败：{type(exc).__name__}: {exc}"


# ---------------- 纯规则兜底报告 ----------------
# AI 不可用时兜底小课堂的预置讲义（按日期轮换，AI 恢复后自动恢复现场教学）
FALLBACK_LESSONS = [
    "什么是基金净值？净值就是每份基金值多少钱（基金总资产÷总份额）。你持有的钱=份额×最新净值，它每天收盘后才更新，白天看到的\"估算涨跌\"只是近似值。好比一筐苹果：筐里苹果总价÷苹果个数=每个苹果的单价，单价变了你的总钱数才变。",
    "什么是PE历史分位？PE（市盈率）是股票价格÷每股盈利，衡量\"贵不贵\"。分位60%的意思是：过去十年里只有40%的时间比现在更贵。它说的是\"价值\"，不是明天的涨跌——西瓜贵不代表明天没人买，只说明你现在进货不划算。",
    "为什么用金字塔补仓？金字塔补仓=越跌越买、每跌一档买一份，而不是一次把钱打光。因为没人知道底在哪，分批买能让你的平均成本一路摊低，即使继续跌，损失也被控制住。好比打折季：5折买一件、4折再买一件，比第一天就把钱花光聪明。",
    "为什么要分批止盈？一次性全卖可能卖在启动点，分批卖则是\"涨了卖一点、再涨再卖一点\"，既锁定利润又不错过后续上涨。好比煮饺子：尝一个熟了捞一个，而不是把整锅全捞出来等它凉。",
    "为什么半导体基金波动这么大？半导体行业受\"芯片周期\"影响：下游需求火爆时涨价扩产，供过于求时降价减产，业绩像过山车。所以你看到的±30%波动，是这个品种的\"性格\"，不是系统坏了。好比冲浪板：浪越大越刺激，但也要抱得稳。",
]


def fallback_report(data, ai_error):
    """AI 不可用时的兜底：直接用策略规则算出信号，附免责声明"""
    date_str = data["meta"]["date"]
    lines = [
        f"# 📈 基金盯盘 · {date_str}（简易版）",
        "",
        "> 本报告由程序自动生成，仅供参考，不构成投资建议。"
        "若您在15:00后才收到本报告，操作建议作废，请勿据此交易。",
        f"> ⚠️ 今日 AI 分析服务暂不可用（{ai_error}），以下为按规则自动计算的信号，"
        "小课堂为预置讲义。AI 恢复后自动恢复正常报告。",
        "",
        "## 持仓一览",
    ]
    for f in data.get("funds", []):
        if f.get("missing"):
            lines.append(f"- {f['code']}：数据暂缺")
            continue
        name = f.get("name") or f["code"]
        est = f.get("est_pct")
        est_txt = f"{est}%（估算）" if est is not None else "缺失"
        lines.append(f"- **{name}**（{f['code']}）：今日估算 {est_txt}，最新净值 {f['latest_nav']}")
    lines += ["", "## 规则信号"]
    total = 0
    reserve = data.get("reserve_fund") or 0
    layer_money = round(reserve / 5, 2) if reserve else 0
    for f in data.get("funds", []):
        if f.get("missing"):
            continue
        name = f.get("name") or f["code"]
        signal, money = rule_signal(f, layer_money)
        line = f"- **{name}** → {signal}"
        if money:
            line += f"，建议金额 {money} 元"
        elif f.get("market_value") and signal.startswith(("止盈提醒", "已回本")):
            line += f"（当前市值约 {round(f['market_value'])} 元，若止盈每批约 {round(f['market_value'] / 3)} 元）"
        lines.append(line)
        total += money
    if total:
        lines.append(f"\n今日建议动用资金合计：{total} 元（来自预留补仓资金的规则分层）")
    idx = datetime.now(TZ_SH).timetuple().tm_yday % len(FALLBACK_LESSONS)
    lines += [
        "",
        "## 今日小课堂",
        FALLBACK_LESSONS[idx],
        "",
        "---",
        "本报告由程序自动生成，仅供参考，不构成投资建议。",
    ]
    return "\n".join(lines)


def rule_signal(f, layer_money):
    """与 prompt_template.md 的策略规则保持一致的纯规则判断，返回 (信号文本, 建议金额)。
    口径（用户拍板）：回本不自动减仓，+5%才提醒止盈；PE>80%且深亏不喊减仓（不割肉）。"""
    profit_acc = f.get("profit_acc_pct")
    drop = -f.get("profit_pct") if f.get("profit_pct") is not None else None
    pe = (f.get("eva") or {}).get("pe_percentile")
    if profit_acc is not None and profit_acc >= 5:
        return f"止盈提醒：含分红口径收益已达 +{round(profit_acc, 1)}%，可考虑分批止盈（分3次、每次约1/3）", 0
    if profit_acc is not None and profit_acc >= 0:
        return "已回本：按你的选择继续持有；若涨幅达到 +5%，再提醒考虑分批止盈", 0
    if drop is not None:
        threshold = 15 if pe is not None else 20     # 估值缺失时保守化
        if drop >= threshold and (pe is None or pe < 0.3):
            layer = min(5, int((drop - 15) // 5) + 1)
            return f"补仓：跌幅 {round(drop, 1)}% 达到规则阈值，建议执行第{layer}层", layer_money
        if drop >= 15 and pe is not None and pe >= 0.3:
            return "暂缓补仓：跌幅到位但估值分位仍偏高；深亏不割肉，等估值回落", 0
    return "持有：未触发规则，等待信号", 0


# ---------------- 推送与存档 ----------------
def extract_topic(report):
    """提取小课堂主题标记并删除标记行，返回 (清理后的报告, 主题名)"""
    m = re.search(r"<!--TOPIC:(.*?)-->", report, re.S)
    topic = m.group(1).strip() if m else ""
    if not topic:
        # 兜底：AI 偶尔漏掉标记，从小课堂标题里提取，保证主题轮换不失效
        m2 = re.search(r"今日小课堂[：:]\s*(.+)", report)
        if m2:
            topic = m2.group(1).strip().split("\n")[0][:30]
    cleaned = re.sub(r"<!--TOPIC:(.*?)-->", "", report, flags=re.S).strip()
    return cleaned, topic


def push_wechat(title, content):
    """PushPlus 推送到个人微信，失败重试2次，返回是否成功"""
    token = os.environ.get("PUSHPLUS_TOKEN")
    if not token:
        print("[推送] 未检测到 PUSHPLUS_TOKEN 环境变量，跳过推送")
        return False
    payload = {"token": token, "title": title, "content": content, "template": "markdown"}
    for attempt in range(3):
        try:
            import requests
            resp = requests.post("https://www.pushplus.plus/send", json=payload, timeout=15)
            if resp.json().get("code") == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只组装提示词，不调AI不推送")
    parser.add_argument("--no-push", action="store_true", help="调AI但不推送微信")
    parser.add_argument("--force", action="store_true", help="忽略休市判断")
    args = parser.parse_args()

    if not DATA_PATH.exists():
        print("data.json 不存在，请先运行 fetch_data.py")
        sys.exit(1)
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    meta = data["meta"]
    today = datetime.now(TZ_SH).strftime("%Y-%m-%d")

    force_flag = args.force or bool(meta.get("force"))

    # 防重复：云端运行且当天报告已存在 → 跳过（定时排队/补跑时避免重复推送、重复花 AI 费用）
    if os.environ.get("GITHUB_ACTIONS") and not force_flag \
            and (REPORTS_DIR / f"{today}.md").exists():
        print(f"今日报告已存在（{today}.md），云端自动跳过，避免重复推送。")
        sys.exit(0)

    if not meta.get("market_open") and not force_flag:
        print(f"今日休市（{meta.get('open_reason')}），跳过报告。"
              "如需强制生成：analyze.py --force")
        sys.exit(0)

    missing_all = all(f.get("missing") for f in data.get("funds", []))
    if missing_all:
        push_wechat("⚠️ 基金盯盘：数据抓取失败",
                    f"今日（{today}）所有基金数据抓取失败，请检查数据源。\n"
                    f"原因线索：{'; '.join(data.get('warnings', [])) or '未知'}")
        print("所有基金数据缺失，已推送失败告警。")
        sys.exit(1)

    report, ai_error = generate_report(data, dry_run=args.dry_run)
    if args.dry_run:
        return

    if report is None:
        report = fallback_report(data, ai_error)
    else:
        report, topic = extract_topic(report)
        if topic:
            taught = load_taught_topics()
            if topic not in taught:
                taught.append(topic)
                save_taught_topics(taught)
                print(f"[小课堂] 新主题已记录：{topic}")

    # 存档
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{today}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[存档] 报告已写入 {report_path}")

    # 推送
    if args.no_push:
        print("[推送] --no-push 模式，未推送微信。")
        return
    title = f"📈 基金盯盘 {meta['date'][5:].replace('-', '月')}日"
    if not os.environ.get("PUSHPLUS_TOKEN"):
        print("[推送] 本机未配置 PUSHPLUS_TOKEN，跳过推送（云端由 GitHub Secrets 提供）。")
        return
    if push_wechat(title, report):
        print("[推送] 微信推送成功")
    else:
        print("[推送] 微信推送失败！请检查 PUSHPLUS_TOKEN。")
        sys.exit(2)


if __name__ == "__main__":
    main()
