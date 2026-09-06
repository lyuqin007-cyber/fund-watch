# -*- coding: utf-8 -*-
"""基金盯盘 · 数据抓取层
从免费公开接口抓取：基金净值/历史、大盘与板块ETF实时行情、指数PE估值分位，
预计算好摘要指标后写入 data/data.json 供 analyze.py 使用。

设计原则（多源容灾）：
- 每个数据源独立 try/except，一个源挂了绝不拖垮其他源；
- 每个源都有 fallback 链，全部失败时该基金/栏目标注"数据暂缺"，报告照常生成；
- 所有异常与降级情况记入 data.json 的 warnings 字段，最终会体现在报告里。
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# ---------------- 基础配置 ----------------
TZ_SH = ZoneInfo("Asia/Shanghai")          # 北京时间（GitHub 服务器是 UTC，必须显式指定）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ROOT = Path(__file__).resolve().parent.parent
FUNDS_CFG_PATH = ROOT / "funds.json"
OUT_PATH = ROOT / "data" / "data.json"

BOARD_SECIDS = ["1.000001", "1.000300", "0.399006"]  # 上证指数 / 沪深300 / 创业板指
HISTORY_DAYS = 60                                     # 拉取历史净值条数

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8")


def eva_level(pct):
    """估值分位 → 5 档分级（FinClaw 标准：PE 分位 = 低于当前值的交易日数 / 有效总数）"""
    if pct < 0.2:
        return "极度低估"
    if pct < 0.4:
        return "偏低估"
    if pct < 0.6:
        return "合理"
    if pct < 0.8:
        return "偏高估"
    return "极度高估"


# ---------------- 通用 HTTP（带重试） ----------------
def http_get(url, referer=None, params=None, retries=2, timeout=(5, 15)):
    """GET 请求，带浏览器 UA + 可选 Referer，失败退避重试，最终失败返回 None"""
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp
        except requests.RequestException:
            pass
        if attempt < retries:
            time.sleep(1 + attempt)
    return None


# ---------------- 基金净值：主源 东方财富 f10 ----------------
def fetch_nav_em(code):
    """东方财富历史净值接口（主源，每页实际约20条，需翻页）。
    返回 [{date,nav,acc,pct}] 按日期降序；失败返回 None"""
    hist, seen = [], set()
    for page in range(1, 9):   # 最多翻8页，约160条，够算近60日趋势
        resp = http_get("https://api.fund.eastmoney.com/f10/lsjz",
                        referer="http://fundf10.eastmoney.com/",
                        params={"fundCode": code, "pageIndex": page, "pageSize": 20})
        if resp is None:
            break
        try:
            lst = resp.json().get("Data", {}).get("LSJZList") or []
        except (ValueError, AttributeError):
            break
        if not lst:
            break
        for item in lst:
            try:
                d = item["FSRQ"]
                if d not in seen:
                    seen.add(d)
                    hist.append({
                        "date": d,
                        "nav": float(item["DWJZ"]),
                        "acc": float(item["LJJZ"]),
                        "pct": float(item.get("JZZZL") or 0),
                    })
            except (KeyError, ValueError, TypeError):
                continue
        if len(hist) >= 130 or len(lst) < 20:
            break
    hist.sort(key=lambda x: x["date"], reverse=True)
    return hist or None


# ---------------- 基金净值：备源 东财 pingzhongdata（也用于取基金名称） ----------------
def fetch_pingzhong(code):
    """pingzhongdata.js 备源。返回 {"name": str, "hist": [...]}；失败返回 None"""
    resp = http_get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                    referer="https://fund.eastmoney.com/")
    if resp is None:
        return None
    text = resp.text
    m = re.search(r'Data_netWorthTrend\s*=\s*(\[.*?\]);', text, re.S)
    if not m:
        return None
    try:
        trend = json.loads(m.group(1))
    except ValueError:
        return None
    hist = []
    for item in trend:
        try:
            hist.append({
                "date": datetime.fromtimestamp(int(item["x"]) / 1000, tz=TZ_SH).strftime("%Y-%m-%d"),
                "nav": float(item["y"]),
                "acc": None,
                "pct": float(item.get("equityReturn") or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue
    hist.sort(key=lambda x: x["date"], reverse=True)
    if not hist:
        return None
    name = None
    nm = re.search(r'fS_name\s*=\s*"([^"]+)"', text)
    if nm:
        name = nm.group(1)
    return {"name": name, "hist": hist}


# ---------------- 基金净值：备源 腾讯（GBK 编码） ----------------
def fetch_tencent(code):
    """腾讯 jj 行情备源（GBK）。返回 {"name", "date", "nav", "acc"}；失败返回 None"""
    resp = http_get(f"https://qt.gtimg.cn/q=jj{code}", referer="https://gu.qq.com/")
    if resp is None:
        return None
    resp.encoding = "gbk"
    m = re.search(r'="([^"]*)"', resp.text)
    if not m:
        return None
    parts = m.group(1).split("|")
    if len(parts) < 3:
        return None
    # 腾讯格式字段不固定，防御性解析：名称在首位，取前两个浮点数为净值/累计净值，日期字段形如 YYYY-MM-DD
    result = {"name": parts[0].strip(), "date": None, "nav": None, "acc": None}
    nums = []
    for p in parts[1:]:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.strip()):
            result["date"] = p.strip()
        else:
            try:
                nums.append(float(p))
            except ValueError:
                continue
        if result["date"] and len(nums) >= 2:
            break
    if nums:
        result["nav"] = nums[0]
    if len(nums) >= 2:
        result["acc"] = nums[1]
    return result if result["nav"] is not None else None


def fetch_nav(code, warnings):
    """按 fallback 链抓取一只基金：东财 f10 → pingzhongdata → 腾讯。
    返回 {"name","latest_date","latest_nav","latest_acc","hist","source"}；全挂返回 None"""
    hist = fetch_nav_em(code)
    if hist:
        name = None
        pz = fetch_pingzhong(code)          # 只用它补基金名称（主数据来自 f10）
        if pz and pz.get("name"):
            name = pz["name"]
        return {
            "name": name, "latest_date": hist[0]["date"],
            "latest_nav": hist[0]["nav"], "latest_acc": hist[0]["acc"],
            "hist": hist, "source": "东方财富",
        }
    pz = fetch_pingzhong(code)
    if pz:
        return {
            "name": pz["name"], "latest_date": pz["hist"][0]["date"],
            "latest_nav": pz["hist"][0]["nav"], "latest_acc": None,
            "hist": pz["hist"], "source": "东方财富pingzhongdata",
        }
    tx = fetch_tencent(code)
    if tx:
        warnings.append(f"基金{code}：净值历史不可用，仅取得最新净值（腾讯源）")
        return {
            "name": tx["name"], "latest_date": tx["date"],
            "latest_nav": tx["nav"], "latest_acc": tx["acc"],
            "hist": [], "source": "腾讯",
        }
    warnings.append(f"基金{code}：所有净值数据源均失败")
    return None


# ---------------- 大盘/板块 ETF 实时行情：东财 push2 ----------------
def fetch_board(secids, warnings):
    """返回 {secid: {"name","price","pct","ts"}}；失败返回 {} 并记 warning"""
    resp = http_get("https://push2.eastmoney.com/api/qt/ulist.np/get",
                    referer="https://quote.eastmoney.com/",
                    params={
                        "fltt": "2", "np": "1",
                        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
                        "invt": "2", "dect": "1",
                        "secids": ",".join(secids),
                        "fields": "f2,f3,f4,f12,f13,f14,f124",
                    })
    board = {}
    if resp is None:
        warnings.append("大盘行情获取失败")
        return board
    try:
        diff = resp.json()["data"]["diff"]
    except (ValueError, KeyError, TypeError):
        warnings.append("大盘行情解析失败")
        return board
    if isinstance(diff, dict):
        diff = [diff]
    for item in diff:
        try:
            secid = f"{item['f13']}.{item['f12']}"
            board[secid] = {
                "name": item["f14"],
                "price": item["f2"],
                "pct": item["f3"],
                "ts": item.get("f124"),  # 行情时间戳（秒），用于休市判断
            }
        except (KeyError, TypeError):
            continue
    return board


# ---------------- 指数 PE 估值分位：蛋卷 ----------------
def fetch_eva(warnings):
    """返回 {index_code: {"name","pe","pe_percentile","pb","pb_percentile"}}；失败返回 None"""
    resp = http_get("https://danjuanfunds.com/djapi/index_eva/dj",
                    referer="https://danjuanfunds.com/")
    if resp is None:
        warnings.append("指数估值分位获取失败（蛋卷接口不可用），今日补仓规则将按保守口径执行")
        return None
    try:
        items = resp.json()["data"]["items"]
    except (ValueError, KeyError, TypeError):
        warnings.append("指数估值分位解析失败，今日补仓规则将按保守口径执行")
        return None
    eva = {}
    for it in items:
        try:
            eva[it["index_code"]] = {
                "name": it.get("name"),
                "pe": it.get("pe"),
                "pe_percentile": it.get("pe_percentile"),
                "pb": it.get("pb"),
                "pb_percentile": it.get("pb_percentile"),
            }
        except (KeyError, TypeError):
            continue
    return eva or None


# ---------------- 休市判断 ----------------
def last_trading_day(today):
    """上一个自然日（跳过周末）；法定节假日无法从日历推，由行情时间戳兜底判断"""
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def determine_open(board, funds_data, force):
    """判断今日是否开市。主判据：行情时间戳（f86）是否为今天；备判据：最新净值日期"""
    today = datetime.now(TZ_SH).date()
    if force:
        return True, "强制生成(忽略休市判断)"
    if today.weekday() >= 5:
        return False, "周末休市"
    ts = None
    for b in board.values():
        if b.get("ts"):
            ts = b["ts"]
            break
    if ts:
        qdate = datetime.fromtimestamp(int(ts), tz=TZ_SH).date()
        if qdate < today:
            return False, "今日无行情（法定节假日休市）"
        return True, "开市"
    # 行情时间戳缺失：用各基金最新净值日期兜底
    latest_dates = [f["latest_date"] for f in funds_data if f.get("latest_date")]
    if latest_dates:
        max_date = max(d for d in latest_dates if d)
        if max_date < last_trading_day(today).strftime("%Y-%m-%d"):
            return False, "净值未更新，疑似休市"
    return True, "开市（行情时间戳缺失，按工作日处理）"


# ---------------- 预计算（省 token：只把摘要给 AI） ----------------
def calc_trend(hist, days):
    """近 N 日累计涨跌%（用净值算，含分红因素）"""
    if not hist or len(hist) < days + 1:
        return None
    try:
        return round((hist[0]["nav"] / hist[days]["nav"] - 1) * 100, 2)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def build_fund_entry(f, board, eva, warnings):
    """组装一只基金的完整摘要"""
    code = f["code"]
    entry = {"code": code}
    nav_data = fetch_nav(code, warnings)
    if nav_data is None:
        entry["missing"] = True
        return entry
    entry["name"] = f.get("name") or nav_data["name"] or code
    entry["latest_nav"] = nav_data["latest_nav"]
    entry["latest_acc"] = nav_data["latest_acc"]
    entry["latest_date"] = nav_data["latest_date"]
    entry["source"] = nav_data["source"]
    hist = nav_data["hist"]

    invested = f.get("invested")
    shares = f.get("shares")
    cost_nav = f.get("cost_nav")
    if shares:
        cost_nav_eff = invested / shares if invested else None
    elif invested and cost_nav:
        shares = invested / cost_nav
        cost_nav_eff = cost_nav
    else:
        shares, cost_nav_eff = None, None

    entry["shares"] = round(shares, 2) if shares else None
    entry["cost_nav"] = round(cost_nav_eff, 4) if cost_nav_eff else None
    if invested is not None and shares:
        value = shares * nav_data["latest_nav"]
        entry["invested"] = invested
        entry["market_value"] = round(value, 2)
        entry["profit_pct"] = round((value - invested) / invested * 100, 2)
        # 距回本还需上涨多少（按最新净值口径）
        entry["to_breakeven_pct"] = round((cost_nav_eff / nav_data["latest_nav"] - 1) * 100, 2)
        # 含分红口径（累计净值），用于"是否已回本"判断
        if nav_data["latest_acc"]:
            acc_value = shares * nav_data["latest_acc"]
            entry["profit_acc_pct"] = round((acc_value - invested) / invested * 100, 2)
    elif invested is not None:
        warnings.append(f"基金{code}：配置了总投入但缺份额/成本净值，仅显示涨跌幅")

    # 今日估算涨跌：用对应板块 ETF 实时涨跌近似
    secid = f.get("board_etf_secid")
    if secid and secid in board:
        entry["est_pct"] = board[secid]["pct"]
        entry["board_name"] = board[secid]["name"]
        entry["est_method"] = "板块ETF实时涨跌"
    else:
        entry["est_pct"] = None
        if secid:
            warnings.append(f"基金{code}：板块ETF({secid})行情缺失，今日估算涨跌不可用")

    # 近 N 日趋势
    entry["trend_5d"] = calc_trend(hist, 5)
    entry["trend_20d"] = calc_trend(hist, 20)
    entry["trend_60d"] = calc_trend(hist, 60)

    # 估值分位
    dj = f.get("danjuan_index")
    if dj and eva and dj in eva:
        pe = eva[dj]["pe_percentile"]
        entry["eva"] = {
            "index": dj,
            "index_name": eva[dj]["name"],
            "pe_percentile": pe,
            "pb_percentile": eva[dj]["pb_percentile"],
            "level": eva_level(pe) if pe is not None else None,
        }
    elif dj:
        entry["eva"] = None
        warnings.append(f"基金{code}：蛋卷接口无指数 {dj} 的估值数据")
    return entry


# ---------------- 主流程 ----------------
def main():
    force = (sys.argv[1] == "--force" if len(sys.argv) > 1 else False) \
        or os.environ.get("FORCE") == "1"
    warnings = []
    cfg = json.loads(FUNDS_CFG_PATH.read_text(encoding="utf-8"))

    all_secids = list(BOARD_SECIDS)
    for f in cfg.get("funds", []):
        if f.get("board_etf_secid") and f["board_etf_secid"] not in all_secids:
            all_secids.append(f["board_etf_secid"])
    board = fetch_board(all_secids, warnings)
    eva = fetch_eva(warnings)

    funds_data = [build_fund_entry(f, board, eva, warnings) for f in cfg.get("funds", [])]

    open_flag, open_reason = determine_open(board, funds_data, force)
    now = datetime.now(TZ_SH)
    data = {
        "meta": {
            "date": now.strftime("%Y-%m-%d"),
            "fetch_time": now.strftime("%H:%M:%S"),
            "market_open": open_flag,
            "open_reason": open_reason,
            "force": force,
        },
        "reserve_fund": cfg.get("reserve_fund"),
        "goal": cfg.get("goal"),
        "board": board,
        "funds": funds_data,
        "warnings": warnings,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"数据抓取完成：{len(funds_data)} 只基金，开市状态={open_reason}")
    for w in warnings:
        print(f"[警告] {w}")
    if not open_flag and not force:
        print("今日休市，analyze.py 将跳过推送。")


if __name__ == "__main__":
    main()
