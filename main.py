import datetime
import json
import os
import time
import requests
import yfinance as yf

# =====================================================================
# 1. Telegram 憑證與常數設定 (從環境變數讀取保護隱私)
# =====================================================================
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
POS_FILE = "position.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

# 擴充版：涵蓋主流熱門股票期貨標的池
STOCK_FUTURES_WHITELIST = {
    # 半導體 / 晶圓 / 封測
    "2330": "台積電", "2454": "聯發科", "2303": "聯電", "3711": "日月光投控", 
    "2449": "京元電子", "3035": "智原", "3443": "創意", "3661": "世芯-KY",
    # AI 伺服器 / 代工
    "2317": "鴻海", "2382": "廣達", "3231": "緯創", "6669": "緯穎", 
    "2356": "英業達", "2376": "技嘉", "2357": "華碩", "2301": "光寶科",
    # 散熱 / 機殼 / 電源
    "3017": "奇鋐", "3324": "雙鴻", "2308": "台達電", "2421": "建準", 
    "3013": "晟銘電", "8210": "勤誠", "8996": "高力",
    # 網通 / 矽光子 / 光通訊
    "2345": "智邦", "3450": "聯鈞", "4977": "眾達-KY", "6451": "訊芯-KY",
    # 光學 / 鏡頭 / 被動元件
    "3008": "大立光", "3406": "玉晶光", "2327": "國巨", "2492": "華新科",
    # 重電 / 綠能
    "1519": "華城", "1503": "士電", "1513": "中興電", "1514": "亞力", "1609": "大亞",
    # PCB / CCL / 載板
    "2368": "金像電", "3037": "欣興", "8046": "南電", "3189": "景碩", "6274": "台燿", "2383": "台光電",
    # 車用 / 零組件 / 機器人
    "2359": "所羅門", "4566": "時碩工業", "1504": "東元",
    # 面板 / 記憶體
    "2409": "友達", "3481": "群創", "2408": "南亞科", "2344": "華邦電", "2337": "旺宏",
    # 航運 / 傳產高動能
    "2603": "長榮", "2609": "陽明", "2615": "萬海", "2618": "長榮航", "2610": "華航",
    # 主流金融與電子
    "2881": "富邦金", "2882": "國泰金", "2377": "微星", "2379": "瑞昱"
}

def send_tg_msg(message: str) -> bool:
    """發送 Telegram Markdown 格式推播"""
    if not TG_TOKEN or not CHAT_ID:
        print("未設定 TG_TOKEN 或 CHAT_ID 環境變數", flush=True)
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        resp = requests.post(url, data=payload, timeout=8)
        return resp.status_code == 200
    except Exception as e:
        print(f"推播失敗: {e}", flush=True)
        return False

# =====================================================================
# 2. 持倉狀態管理 (跨日持久化)
# =====================================================================
def load_position():
    if os.path.exists(POS_FILE):
        try:
            with open(POS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None

def save_position(pos_data):
    with open(POS_FILE, "w", encoding="utf-8") as f:
        json.dump(pos_data, f, ensure_ascii=False, indent=2)

def clear_position():
    if os.path.exists(POS_FILE):
        os.remove(POS_FILE)

# =====================================================================
# 3. 官方 TWSE MIS 毫秒級即時報價
# =====================================================================
def get_twse_live_price(code: str):
    timestamp = int(time.time() * 1000)
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{code}.tw&json=1&delay=0&_={timestamp}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=5)
        data = r.json()
        if "msgArray" in data and len(data["msgArray"]) > 0:
            item = data["msgArray"][0]
            z = item.get("z", "-")
            if z != "-" and float(z) > 0:
                return float(z)
            b = item.get("b", "_").split("_")[0]
            if b != "-" and b != "":
                return float(b)
    except Exception:
        pass
    return None

# =====================================================================
# 4. 結算日計算 (每月第三個星期三)
# =====================================================================
def get_settlement_info():
    today = datetime.date.today()
    wednesdays = []
    for d in range(1, 32):
        try:
            dt = today.replace(day=d)
            if dt.weekday() == 2:
                wednesdays.append(dt)
        except ValueError:
            break
    settle_day = wednesdays[2]
    days_left = (settle_day - today).days
    return settle_day, days_left

# =====================================================================
# 5. 主流程：海選動態引擎與順位遞補
# =====================================================================
def run_engine():
    pos = load_position()

    # 情況 A：已有既有持倉，進入跨日監控模式
    if pos:
        pos["hold_days"] += 1
        save_position(pos)
        send_tg_msg(
            f"🛡️ *【持倉續抱監控中】*\n"
            f"標的：*{pos['name']} ({pos['code']})*\n"
            f"當前持倉天數：*第 {pos['hold_days']} 天*\n"
            f"防守停損價：`{pos['sl_price']:.1f}` | 目標 2：`{pos['t2_price']:.1f}`"
        )
        monitor_live_trade(pos)
        return

    print("=" * 60, flush=True)
    print("⚖️ 【折衷式股票期貨量化雷達・引擎啟動】", flush=True)
    print("=" * 60, flush=True)

    send_tg_msg(f"🛰️ *【折衷式量化雷達】* 系統開機，正在掃描 {len(STOCK_FUTURES_WHITELIST)} 檔活躍股票期貨池...")

    candidates = []
    for code, name in STOCK_FUTURES_WHITELIST.items():
        sym = f"{code}.TW"
        try:
            ticker = yf.Ticker(sym)
            df = ticker.history(period="1mo", interval="1d")
            if df.empty or len(df) < 20:
                continue

            c_prev = df["Close"].iloc[-1]
            h_prev2 = df["High"].iloc[-2]
            ma5 = df["Close"].rolling(5).mean().iloc[-1]
            ma20 = df["Close"].rolling(20).mean().iloc[-1]

            # 14 ATR 計算
            df["tr"] = df[["High", "Close"]].max(axis=1) - df[["Low", "Close"]].min(axis=1)
            atr14 = df["tr"].rolling(14).mean().iloc[-1]

            # 均線多頭排列且過昨高
            if (c_prev > ma20) and (c_prev > ma5) and (c_prev > h_prev2):
                score = (c_prev / ma5) * 10
                candidates.append({
                    "code": code,
                    "name": name,
                    "sym": sym,
                    "score": score,
                    "c_prev": c_prev,
                    "atr": atr14,
                })
        except Exception:
            continue

    if not candidates:
        send_tg_msg("ℹ️ *【雷達回報】* 全池標的無符合突破多頭動能條件，今日暫不開倉。")
        print("今日無符合標的，程式結束。", flush=True)
        return

    # 排序動能前 3 名
    candidates.sort(key=lambda x: x["score"], reverse=True)
    top_candidates = candidates[:3]

    selected = None
    curr_p = None

    for rank, candidate in enumerate(top_candidates):
        c_code = candidate["code"]
        c_name = candidate["name"]
        c_prev = candidate["c_prev"]

        live_p = get_twse_live_price(c_code) or c_prev
        gap_pct = (live_p - c_prev) / c_prev

        if gap_pct > -0.005:
            selected = candidate
            curr_p = live_p
            print(f"第 {rank+1} 順位【{c_name}】跳空 {gap_pct*100:.2f}% 通過風控！決定開倉。", flush=True)
            break
        else:
            send_tg_msg(f"⚠️ *【順位遞補跳過】* 第 {rank+1} 名 {c_name} 開盤跳空 `{gap_pct*100:.2f}%` 破風控，依序遞補下一名！")

    if not selected:
        send_tg_msg("🛑 *【風控完全攔截】* 今日排名前 3 名強勢標的晨間跳空皆破 -0.5%，全數放棄進場以保資金安全。")
        print("所有候選皆未通過風控，結束。", flush=True)
        return

    code = selected["code"]
    name = selected["name"]
    atr = selected["atr"]
    e_price = curr_p

    settle_day, days_left = get_settlement_info()
    settle_tip = ""
    if 0 <= days_left <= 2:
        settle_tip = f"\n⚠️ *【結算日提醒】* 距結算僅剩 {days_left} 天，元大請選【次月份合約】！"

    sl_price = e_price - (0.8 * atr)
    t1_price = e_price + (1.0 * atr)
    t2_price = e_price + (1.6 * atr)
    contract_type = "小型期貨 (100股)" if e_price >= 500 else "標準期貨 (2000股)"

    entry_msg = (
        f"🚨 *【折衷版・進場買進訊號】*\n"
        f"標的：*{name} ({code})*\n"
        f"動能分數：`{selected['score']:.2f}`\n"
        f"規格：*{contract_type}*{settle_tip}\n"
        f"動作：**市價買進 2 口期貨**\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💰 建議進場價：`{e_price:.1f}`\n"
        f"🛑 初始停損 (-0.8 ATR)：`{sl_price:.1f}`\n"
        f"🎯 第一目標 (+1.0 ATR)：`{t1_price:.1f}` (平1口並推升保本)\n"
        f"🏁 第二目標 (+1.6 ATR)：`{t2_price:.1f}` (出清部位)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📲 請打開元大投資先生掛好：\n"
        f"1. 停損 2 口 (`{sl_price:.1f}`)\n"
        f"2. 目標 1 限價賣出 1 口 (`{t1_price:.1f}`)"
    )
    send_tg_msg(entry_msg)

    pos = {
        "code": code,
        "name": name,
        "e_price": e_price,
        "sl_price": sl_price,
        "t1_price": t1_price,
        "t2_price": t2_price,
        "pos_stage": 1,
        "hold_days": 1,
    }
    save_position(pos)
    monitor_live_trade(pos)

# =====================================================================
# 6. 盤中即時洗價與停損停利監控 (觸價即提醒機制)
# =====================================================================
def monitor_live_trade(pos):
    code = pos["code"]
    name = pos["name"]
    sl_p = pos["sl_price"]
    t1_p = pos["t1_price"]
    t2_p = pos["t2_price"]
    e_p = pos["e_price"]
    stage = pos.get("pos_stage", 1)

    print(f"啟動 {name} 盤中即時洗價監控中...", flush=True)

    while True:
        now = datetime.datetime.now()
        # 13:25 尾盤檢核：第 5 天期滿平倉
        if now.hour == 13 and now.minute >= 25:
            if pos.get("hold_days", 1) >= 5:
                send_tg_msg(f"⏳ *【持倉滿 5 天期滿】* {name} 已達持有期限，請於收盤前市價全數平倉離場！")
                clear_position()
            break

        p = get_twse_live_price(code)
        if p and p > 0:
            # 1. 第一目標達成 (觸碰即提醒，推升保本)
            if stage == 1 and p >= t1_p:
                send_tg_msg(
                    f"🎉 *【目標 1 達成】*\n{name} 現價 `{p:.1f}` 已達標 `{t1_p:.1f}`！\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"1. 請在元大平倉 1 口鎖定利潤。\n"
                    f"2. 剩餘 1 口防守線移至進場成本價 `{e_p:.1f}` (啟動保本)。\n"
                    f"3. 掛出目標 2 限價單 `{t2_p:.1f}`。"
                )
                stage = 2
                pos["pos_stage"] = 2
                pos["sl_price"] = e_p
                sl_p = e_p
                save_position(pos)
                time.sleep(2)
                continue

            # 2. 第二目標達成 (全數獲利了結)
            if stage == 2 and p >= t2_p:
                send_tg_msg(
                    f"🏆 *【目標 2 達成】*\n{name} 現價 `{p:.1f}` 已達標 `{t2_p:.1f}`！\n"
                    f"剩餘部位全數獲利了結！"
                )
                clear_position()
                break

            # 3. 停損檢核 (跌破防守線)
            if p <= sl_p:
                send_tg_msg(
                    f"⚠️ *【停損觸發】*\n{name} 現價 `{p:.1f}` 跌破防守線 `{sl_p:.1f}`！\n"
                    f"請立即市價砍倉離場。"
                )
                clear_position()
                break

        time.sleep(3)

if __name__ == "__main__":
    run_engine()
