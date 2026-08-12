"""
Rain Alert Monitor - GitHub Actions版
================================================
Open-Meteo API + 気象庁API(警報)のダブルチェックで降水を検知し、
閾値を超えた場合にSlack Workflow Builder経由でチャンネルへ通知する。

実行環境  : GitHub Actions (ubuntu-latest)
スケジュール: 10分間隔 (07:00-20:00 JST) ← workflow.ymlで設定
通知      : Slack Workflow Builder Webhook
クールダウン: .cooldown_state ファイル（GitHub Actions キャッシュで管理）

【必要なGitHub Secrets】
  SLACK_WEBHOOK_URL : Slack Workflow Builder の Webhook URL
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
    "OEJE": {"lat": 35.46, "lon": 139.64, "area": "横浜市みなとみらい"},
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

# テスト用: 0.0 → 本番時は 10.0 に変更すること
HOURLY_RAIN_THRESHOLD = 0.0  # mm/h
COOLDOWN_MINUTES      = 25
COOLDOWN_FILE         = Path(".cooldown_state")

# ==============================================================
# クールダウン管理（ファイルベース）
# ==============================================================

def is_in_cooldown() -> bool:
    if not COOLDOWN_FILE.exists():
        return False
    try:
        data = json.loads(COOLDOWN_FILE.read_text())
        elapsed_min = (time.time() - data.get("timestamp", 0)) / 60
        return elapsed_min < COOLDOWN_MINUTES
    except Exception:
        return False


def update_cooldown():
    COOLDOWN_FILE.write_text(json.dumps({
        "timestamp": int(time.time()),
        "datetime": datetime.now(JST).isoformat()
    }))

# ==============================================================
# Open-Meteo 関連
# ==============================================================

def fetch_precipitation(lat: float, lon: float) -> dict:
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
        print(f"Open-Meteo APIエラー ({lat},{lon}): {e}")
        return {}


def check_rain_hourly(data: dict) -> tuple:
    if not data or "minutely_15" not in data:
        return False, 0.0, 0.0, "", "データ取得失敗"
    minutely = data["minutely_15"]
    times    = minutely.get("time", [])
    precips  = minutely.get("precipitation", [])
    if not precips:
        return False, 0.0, 0.0, "", "降水データなし"
    slots_1h     = precips[:4] if len(precips) >= 4 else precips
    hourly_total = sum(slots_1h) * (4 / len(slots_1h))
    max_15min    = max(precips)
    max_idx      = precips.index(max_15min)
    peak_time    = times[max_idx] if max_idx < len(times) else "不明"
    return hourly_total >= HOURLY_RAIN_THRESHOLD, hourly_total, max_15min, peak_time, "OK"

# ==============================================================
# 気象庁 警報・注意報 関連
# ==============================================================

def fetch_jma_warnings() -> dict:
    url = f"https://www.jma.go.jp/bosai/warning/data/warning/{JMA_REGION_CODE}.json"
    try:
        req = Request(url, headers={"User-Agent": "RainAlert/GitHub"})
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"気象庁警報APIエラー: {e}")
        return {}


def check_jma_warnings(data: dict) -> tuple:
    if not data:
        return False, [], []
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
        print(f"気象庁データ解析エラー: {e}")
        return False, [], []
    return len(warning_alerts) > 0, warning_alerts, advisory_alerts

# ==============================================================
# Slack 通知（Workflow Builder Webhook）
# ==============================================================

def send_slack(trigger_alerts: list, log_only_info: list) -> bool:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        print("SLACK_WEBHOOK_URL 未設定 — Slack通知スキップ")
        return False

    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    alert_lines = "\n".join(f"• {a}" for a in trigger_alerts)
    message = (
        f":rain_cloud: *【Rain Alert】強雨検知 {now}*\n"
        f"監視拠点: 神奈川9拠点（DEJ3/OEJE/DEJ6/DEJ9/DTK8/PEJ6/OEJW/OEJT/OEJU）\n"
        f"\n{alert_lines}\n"
        f"\n_Open-Meteo / 気象庁データに基づく自動通知_"
    )

    payload = {"Text": message}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = Request(webhook_url, data=body, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as resp:
            result = resp.read().decode()
        print(f"Slack送信完了: {result}")
        return True
    except Exception as e:
        print(f"Slack送信エラー: {e}")
        return False

# ==============================================================
# メイン処理
# ==============================================================

def main():
    now_jst = datetime.now(JST)
    print(f"Rain Alert 開始 | {now_jst.strftime('%Y-%m-%d %H:%M')} JST")

    # 稼働時間チェック（07:00-20:00 JST）
    if now_jst.hour < 7 or now_jst.hour >= 20:
        print("稼働時間外 — スキップ")
        return

    # クールダウンチェック
    if is_in_cooldown():
        print(f"クールダウン中（{COOLDOWN_MINUTES}分以内に通知済み）— スキップ")
        return

    trigger_alerts = []
    log_only_info  = []

    # チェック1: 気象庁 警報・注意報
    print("--- 気象庁 警報・注意報チェック ---")
    warning_data = fetch_jma_warnings()
    if warning_data:
        has_warning, warning_alerts, advisory_alerts = check_jma_warnings(warning_data)
        trigger_alerts.extend(warning_alerts)
        log_only_info.extend(advisory_alerts)
        if not has_warning and not advisory_alerts:
            print("警報・注意報: なし")
    time.sleep(0.5)

    # チェック2: Open-Meteo 降水チェック
    print(f"--- Open-Meteo 降水チェック (閾値: {HOURLY_RAIN_THRESHOLD}mm/h) ---")
    for station_name, station_info in STATIONS.items():
        data = fetch_precipitation(station_info["lat"], station_info["lon"])
        exceeds, hourly_mm, max_15min, peak_time, _ = check_rain_hourly(data)
        if exceeds:
            level = (
                "[猛烈な雨    ]" if hourly_mm >= 80 else
                "[非常に激しい雨]" if hourly_mm >= 50 else
                "[激しい雨    ]" if hourly_mm >= 30 else
                "[強い雨      ]" if hourly_mm >= 20 else
                "[やや強い雨  ]"
            )
            alert_msg = (
                f"{level} {station_name} ({station_info['area']}): "
                f"{hourly_mm:.1f}mm/h  最大15分値: {max_15min:.1f}mm  ピーク: {peak_time}"
            )
            trigger_alerts.append(alert_msg)
            print(f"検知: {alert_msg}")
        else:
            print(f"閾値未満: {station_name} {hourly_mm:.1f}mm/h")
            if hourly_mm > 0:
                log_only_info.append(f"{station_name}: {hourly_mm:.1f}mm/h")
        time.sleep(0.3)

    # 通知判定
    if trigger_alerts:
        if send_slack(trigger_alerts, log_only_info):
            update_cooldown()
    else:
        print("全拠点 閾値未満 & 警報なし — 通知なし")

    print("Rain Alert 完了")


if __name__ == "__main__":
    main()
