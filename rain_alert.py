"""
Rain Alert Monitor - GitHub Actions版 (差分通知対応)
================================================
Open-Meteo API + 気象庁API のダブルチェックで降水を検知し、
前回チェックからの「変化」があった拠点のみSlackへ通知する。

実行環境  : GitHub Actions (ubuntu-latest)
スケジュール: 10分間隔 (07:00-20:00 JST) ← workflow.ymlで設定
通知      : Slack Workflow Builder Webhook
差分通知  : .rain_state ファイル（GitHub Actions キャッシュで管理）

【必要なGitHub Secrets】
  SLACK_WEBHOOK_URL  : Slack Workflow Builder の Webhook URL
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request

JST = timezone(timedelta(hours=9))

# ==============================================================
# 監視設定（9拠点）
# ==============================================================

STATIONS = {
    "DEJ3": {"lat": 35.51, "lon": 139.68, "area": "横浜市鶴見区"},
    "OEJE": {"lat": 35.46, "lon": 139.64, "area": "横浜みなとみらい"},
    "DEJ6": {"lat": 35.54, "lon": 139.57, "area": "横浜市都筑区"},
    "DEJ9": {"lat": 35.40, "lon": 139.53, "area": "横浜市戸塚区"},
    "DTK8": {"lat": 35.33, "lon": 139.35, "area": "平塚市"},
    "PEJ6": {"lat": 35.26, "lon": 139.15, "area": "小田原市"},
    "OEJW": {"lat": 35.28, "lon": 139.67, "area": "横須賀市横須賀中央"},
    "OEJT": {"lat": 35.18, "lon": 139.61, "area": "横須賀市長井"},
    "OEJU": {"lat": 35.36, "lon": 139.65, "area": "横浜市金沢区福浦"},
}

JMA_REGION_CODE = "140000"

JMA_WARNING_CODES = {
    "02": "暴風雪警報", "03": "大雨警報", "04": "洪水警報",
    "05": "暴風警報", "06": "大雪警報", "07": "波浪警報",
    "08": "高潮警報", "10": "大雨特別警報", "11": "暴風特別警報",
    "12": "暴風雪特別警報", "13": "大雪特別警報", "14": "波浪特別警報",
    "15": "雷注意報", "16": "強風注意報", "17": "風雪注意報",
    "18": "大雪注意報", "19": "波浪注意報", "20": "洪水注意報",
    "21": "高潮注意報", "22": "大雨注意報", "23": "濃霧注意報",
}

JMA_WARNING_TRIGGER_CODES = {"03", "04", "05", "10", "11", "12"}
JMA_ADVISORY_CODES        = {"15", "20", "22"}

HOURLY_RAIN_THRESHOLD = 5.0    # mm/h（傘が必要になる雨・バイク配送に支障が出始めるレベル）
COOLDOWN_MINUTES      = 5
STATE_FILE            = Path(".rain_state")

# ==============================================================
# 雨量ヘルパー
# ==============================================================

def get_rain_level(mm: float) -> str:
    if mm >= 80: return "猛烈な雨"
    if mm >= 50: return "非常に激しい雨"
    if mm >= 30: return "激しい雨"
    if mm >= 20: return "強い雨"
    if mm >= 10: return "やや強い雨"
    if mm > 0:   return "小雨"
    return "雨なし"

def get_rain_emoji(mm: float) -> str:
    if mm >= 30: return "🚨"
    if mm >= 20: return "⛈⛈"
    if mm >= 10: return "⛈"
    if mm >= 5:  return "🌧🌧"
    if mm > 0:   return "🌧"
    return "☁️"

def get_rain_bar(mm: float) -> str:
    """雨量を5マスのカラーバーで表示（最大50mm/h基準）"""
    filled = min(5, round(mm / 50 * 5))
    if mm >= 50:   block = "🟥"
    elif mm >= 30: block = "🟧"
    elif mm >= 20: block = "🟨"
    elif mm >= 5:  block = "🟦"
    else:          block = "🟩"
    return block * filled + "▫" * (5 - filled)

# ==============================================================
# 状態管理（ファイルベース）
# ==============================================================

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_notified": 0, "prev_rain": {}, "prev_jma": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_notified": 0, "prev_rain": {}, "prev_jma": []}


def save_state(last_notified: float, rain_results: dict, jma_active: list):
    STATE_FILE.write_text(json.dumps({
        "last_notified": last_notified,
        "last_run":      time.time(),
        "last_run_dt":   datetime.now(JST).isoformat(),
        "prev_rain":     rain_results,
        "prev_jma":      jma_active,
    }, ensure_ascii=False, indent=2))


def is_in_cooldown(last_notified: float) -> bool:
    return (time.time() - last_notified) / 60 < COOLDOWN_MINUTES

# ==============================================================
# 差分検出
# ==============================================================

def _diff_stations(prev: dict, curr: dict) -> tuple:
    """拠点状態の差分を (new, intensified, weakened) で返す"""
    new_alerts  = []
    intensified = []
    weakened    = []
    for name, c in curr.items():
        p = prev.get(name, {})
        if c.get("exceeds") and not p.get("exceeds"):
            new_alerts.append((name, c))
        elif c.get("exceeds") and p.get("exceeds"):
            if c.get("level") != p.get("level"):
                intensified.append((name, c, p))
        elif not c.get("exceeds") and p.get("exceeds"):
            weakened.append((name, p))
    return new_alerts, intensified, weakened


def get_diff(prev_rain: dict, curr_rain: dict,
             prev_jma: list, curr_jma: list) -> dict:
    rain_new, rain_up, rain_down = _diff_stations(prev_rain, curr_rain)

    prev_jma_set = set(prev_jma)
    curr_jma_set = set(curr_jma)
    new_warnings = sorted(curr_jma_set - prev_jma_set)
    lifted       = sorted(prev_jma_set - curr_jma_set)

    has_changes = bool(rain_new or rain_up or rain_down or new_warnings or lifted)
    return {
        "rain_new":     rain_new,
        "rain_up":      rain_up,
        "rain_down":    rain_down,
        "new_warnings": new_warnings,
        "lifted":       lifted,
        "has_changes":  has_changes,
    }

# ==============================================================
# データ取得
# ==============================================================

def fetch_weather_data(lat: float, lon: float) -> dict:
    """Open-Meteo から降水量を取得"""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&minutely_15=precipitation"
        f"&timezone=Asia/Tokyo"
        f"&forecast_minutely_15=8"
    )
    try:
        req = Request(url, headers={"User-Agent": "RainAlert/GitHub"})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  Open-Meteo APIエラー ({lat},{lon}): {e}")
        return {}


def check_rain(data: dict) -> dict:
    if not data or "minutely_15" not in data:
        return {"exceeds": False, "mm": 0.0, "max_15min": 0.0, "peak_time": "", "level": "取得失敗"}
    minutely = data["minutely_15"]
    times    = minutely.get("time", [])
    precips  = minutely.get("precipitation", [])
    if not precips:
        return {"exceeds": False, "mm": 0.0, "max_15min": 0.0, "peak_time": "", "level": "データなし"}
    slots_1h     = precips[:4] if len(precips) >= 4 else precips
    hourly_total = sum(slots_1h) * (4 / len(slots_1h))
    max_15min    = max(precips)
    max_idx      = precips.index(max_15min)
    peak_time    = times[max_idx] if max_idx < len(times) else "不明"
    return {
        "exceeds":   hourly_total >= HOURLY_RAIN_THRESHOLD,
        "mm":        round(hourly_total, 1),
        "max_15min": round(max_15min, 1),
        "peak_time": peak_time,
        "level":     get_rain_level(hourly_total),
    }

# ==============================================================
# 気象庁 警報・注意報
# ==============================================================

def fetch_jma_warnings() -> dict:
    url = f"https://www.jma.go.jp/bosai/warning/data/warning/{JMA_REGION_CODE}.json"
    try:
        req = Request(url, headers={"User-Agent": "RainAlert/GitHub"})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  気象庁警報APIエラー: {e}")
        return {}


def check_jma_warnings(data: dict) -> tuple:
    if not data:
        return [], []
    warning_alerts  = []
    advisory_alerts = []
    try:
        for area_data in data.get("areaTypes", [{}])[0].get("areas", []):
            area_name = {"140010": "東部", "140020": "西部"}.get(
                area_data.get("code", ""), area_data.get("code", ""))
            for w in area_data.get("warnings", []):
                if w.get("status") != "発表":
                    continue
                w_code = w.get("code", "")
                w_name = JMA_WARNING_CODES.get(w_code, f"コード{w_code}")
                if w_code in JMA_WARNING_TRIGGER_CODES:
                    warning_alerts.append(f"[警報|{area_name}] {w_name} 発表中")
                elif w_code in JMA_ADVISORY_CODES:
                    advisory_alerts.append(f"[注意報|{area_name}] {w_name} 発表中")
    except Exception as e:
        print(f"  気象庁データ解析エラー: {e}")
        return [], []
    return warning_alerts, advisory_alerts

# ==============================================================
# Slack 送信
# ==============================================================

def build_slack_message(diff: dict, all_rain: dict,
                        curr_jma_active: list, now_str: str) -> str:

    max_rain_mm = max((v["mm"] for v in all_rain.values() if v["exceeds"]), default=0)
    header_emoji = get_rain_emoji(max_rain_mm)
    lines = [f"{header_emoji} *【Rain Alert — 状況変化】{now_str}*"]

    # 気象庁警報
    if diff["new_warnings"]:
        lines += ["", "🚨 *気象庁 警報 新規発表:*"]
        for w in diff["new_warnings"]:
            lines.append(f"• {w}")
    if diff["lifted"]:
        lines += ["", "✅ *気象庁 警報 解除:*"]
        for w in diff["lifted"]:
            lines.append(f"• {w}")

    # 雨セクション
    if diff["rain_new"]:
        lines += ["", "🆕 *雨 新規検知:*"]
        for name, info in diff["rain_new"]:
            bar = get_rain_bar(info["mm"])
            lines.append(
                f"{bar}  *{name}* ({STATIONS[name]['area']}): "
                f"{info['mm']}mm/h  _{info['level']}_"
            )
    if diff["rain_up"]:
        lines += ["", "⬆️ *雨が強まった:*"]
        for name, curr, prev in diff["rain_up"]:
            bar = get_rain_bar(curr["mm"])
            d = "⬆" if curr["mm"] > prev.get("mm", 0) else "⬇"
            lines.append(
                f"{bar}  *{name}* ({STATIONS[name]['area']}): "
                f"{prev.get('mm','?')}mm/h {d} {curr['mm']}mm/h  _{curr['level']}_"
            )
    if diff["rain_down"]:
        lines += ["", "🌤 *雨が弱まりました:*"]
        for name, prev in diff["rain_down"]:
            lines.append(
                f"• *{name}* ({STATIONS[name]['area']}): 閾値以下"
                f"  （前回: {prev.get('mm','?')}mm/h）"
            )

    # サマリー
    rain_active = [n for n, v in all_rain.items() if v["exceeds"]]
    if rain_active:
        lines += ["", f"🌧 *雨アクティブ {len(rain_active)}拠点:* {' / '.join(rain_active)}"]
    else:
        lines += ["", "📋 *現在: 全拠点 閾値以下*"]

    lines += ["", "_Open-Meteo / 気象庁データに基づく自動通知_"]
    return "\n".join(lines)


def send_slack(message: str) -> bool:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        print("  SLACK_WEBHOOK_URL 未設定 — Slack通知スキップ")
        return False
    payload = {"Text": message}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = Request(webhook_url, data=body,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as resp:
            result = resp.read().decode()
        print(f"  Slack送信完了: {result}")
        return True
    except Exception as e:
        print(f"  Slack送信エラー: {e}")
        return False

# ==============================================================
# メイン処理
# ==============================================================

def main():
    now_jst = datetime.now(JST)
    print(f"Rain Alert 開始 | {now_jst.strftime('%Y-%m-%d %H:%M')} JST")

    if now_jst.hour < 7 or now_jst.hour >= 20:
        print("稼働時間外 — スキップ")
        return

    # 前回状態を読み込む
    state         = load_state()
    last_notified = state.get("last_notified", 0)
    prev_rain     = state.get("prev_rain", {})
    prev_jma      = state.get("prev_jma", [])

    # ----------------------------------------------------------
    # 気象庁 警報・注意報
    # ----------------------------------------------------------
    print("--- 気象庁 警報・注意報チェック ---")
    warning_data = fetch_jma_warnings()
    curr_jma_active, _ = check_jma_warnings(warning_data)
    if curr_jma_active:
        for w in curr_jma_active:
            print(f"  警報検知: {w}")
    else:
        print("  警報・注意報: なし")
    time.sleep(0.5)

    # ----------------------------------------------------------
    # 各拠点 降水チェック
    # ----------------------------------------------------------
    print(f"--- 拠点チェック (雨閾値:{HOURLY_RAIN_THRESHOLD}mm/h) ---")
    curr_rain = {}
    for name, info in STATIONS.items():
        data = fetch_weather_data(info["lat"], info["lon"])
        rain = check_rain(data)
        curr_rain[name] = rain
        status = "雨検知" if rain["exceeds"] else "雨なし"
        print(f"  {name}: {status}({rain['mm']}mm/h)")
        time.sleep(0.3)

    # ----------------------------------------------------------
    # 差分検出
    # ----------------------------------------------------------
    diff = get_diff(prev_rain, curr_rain, prev_jma, curr_jma_active)
    print(f"--- 差分: 変化あり={diff['has_changes']} ---")
    if diff["rain_new"]:  print(f"  雨新規: {[n for n,_ in diff['rain_new']]}")
    if diff["rain_up"]:   print(f"  雨強化: {[n for n,_,_ in diff['rain_up']]}")
    if diff["rain_down"]: print(f"  雨弱化: {[n for n,_ in diff['rain_down']]}")

    # ----------------------------------------------------------
    # 通知判定
    # ----------------------------------------------------------
    if not diff["has_changes"]:
        rain_cnt = sum(1 for v in curr_rain.values() if v["exceeds"])
        print(f"変化なし — スキップ（雨:{rain_cnt}拠点）")
    elif is_in_cooldown(last_notified):
        print(f"クールダウン中（{COOLDOWN_MINUTES}分以内に通知済み）— スキップ")
    else:
        now_str = now_jst.strftime("%m/%d %H:%M")
        message = build_slack_message(diff, curr_rain, curr_jma_active, now_str)
        print("--- 送信メッセージ ---")
        print(message)
        print("---------------------")
        if send_slack(message):
            last_notified = time.time()

    save_state(last_notified, curr_rain, curr_jma_active)
    print("完了")


if __name__ == "__main__":
    main()
