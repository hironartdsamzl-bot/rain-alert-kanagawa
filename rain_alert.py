"""
Rain Alert Monitor - GitHub Actions版 (差分通知対応)
================================================
Open-Meteo API + 気象庁API(警報)のダブルチェックで降水を検知し、
前回チェックからの「変化」があった拠点のみSlackへ通知する。

実行環境  : GitHub Actions (ubuntu-latest)
スケジュール: 10分間隔 (07:00-20:00 JST) ← workflow.ymlで設定
通知      : Slack Workflow Builder Webhook
差分通知  : .rain_state ファイル（GitHub Actions キャッシュで管理）

【必要なGitHub Secrets】
  SLACK_WEBHOOK_URL  : Slack Workflow Builder の Webhook URL
  GMAIL_USER         : (メール通知用・現在未使用)
  GMAIL_APP_PASSWORD : (メール通知用・現在未使用)
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

HOURLY_RAIN_THRESHOLD = 00.0  # mm/h（本番値）
COOLDOWN_MINUTES      = 10    # 差分なしでも連続投稿を防ぐ最低間隔（差分検知が主制御）
STATE_FILE            = Path(".rain_state")  # 状態ファイル（クールダウン + 前回拠点状態）

# ==============================================================
# 雨量レベルヘルパー
# ==============================================================

def get_rain_level(mm: float) -> str:
    """気象庁基準の雨量カテゴリ名を返す"""
    if mm >= 80: return "猛烈な雨"
    if mm >= 50: return "非常に激しい雨"
    if mm >= 30: return "激しい雨"
    if mm >= 20: return "強い雨"
    if mm >= 10: return "やや強い雨"
    if mm > 0:   return "小雨"
    return "雨なし"

def get_level_emoji(mm: float) -> str:
    """雨量に応じた絵文字を返す"""
    if mm >= 30: return "🚨"
    if mm >= 20: return "⛈⛈"
    if mm >= 10: return "⛈"
    if mm >= 5:  return "🌧🌧"
    if mm > 0:   return "🌧"
    return "☁️"

# ==============================================================
# 状態管理（ファイルベース）
# ==============================================================

def load_state() -> dict:
    """前回チェック時の状態を読み込む"""
    if not STATE_FILE.exists():
        return {"last_notified": 0, "prev_stations": {}, "prev_jma": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_notified": 0, "prev_stations": {}, "prev_jma": []}


def save_state(last_notified: float, station_results: dict, jma_active: list):
    """現在の状態を保存（通知有無に関わらず毎回実行）"""
    STATE_FILE.write_text(json.dumps({
        "last_notified": last_notified,
        "last_run": time.time(),
        "last_run_dt": datetime.now(JST).isoformat(),
        "prev_stations": station_results,
        "prev_jma": jma_active,
    }, ensure_ascii=False, indent=2))


def is_in_cooldown(last_notified: float) -> bool:
    elapsed_min = (time.time() - last_notified) / 60
    return elapsed_min < COOLDOWN_MINUTES

# ==============================================================
# 差分検出
# ==============================================================

def get_diff(prev_stations: dict, curr_stations: dict,
             prev_jma: list, curr_jma: list) -> dict:
    """
    前回状態と今回状態を比較し、変化を分類して返す。

    戻り値:
        new_alerts   : 新たに閾値超えになった拠点 [(name, curr_info), ...]
        intensified  : レベルが上がった拠点        [(name, curr_info, prev_info), ...]
        weakened     : 閾値を下回った拠点          [(name, prev_info), ...]
        new_warnings : 新たに発表された気象庁警報  [str, ...]
        lifted       : 解除された気象庁警報        [str, ...]
        has_changes  : 何らかの変化があったか (bool)
    """
    new_alerts  = []
    intensified = []
    weakened    = []

    for name, curr in curr_stations.items():
        prev = prev_stations.get(name, {})
        curr_exceeds = curr.get("exceeds", False)
        prev_exceeds = prev.get("exceeds", False)

        if curr_exceeds and not prev_exceeds:
            # 新規検知
            new_alerts.append((name, curr))
        elif curr_exceeds and prev_exceeds:
            # 両方検知中 → レベル変化をチェック
            if curr.get("level") != prev.get("level"):
                intensified.append((name, curr, prev))
        elif not curr_exceeds and prev_exceeds:
            # 閾値を下回った（雨が弱まった）
            weakened.append((name, prev))

    prev_jma_set = set(prev_jma)
    curr_jma_set = set(curr_jma)
    new_warnings = sorted(curr_jma_set - prev_jma_set)
    lifted       = sorted(prev_jma_set - curr_jma_set)

    has_changes = bool(new_alerts or intensified or weakened or new_warnings or lifted)
    return {
        "new_alerts":  new_alerts,
        "intensified": intensified,
        "weakened":    weakened,
        "new_warnings": new_warnings,
        "lifted":       lifted,
        "has_changes":  has_changes,
    }

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


def check_rain_hourly(data: dict) -> dict:
    """降水チェック。拠点状態辞書を返す"""
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
    """(警報アクティブリスト, 注意報リスト) を返す"""
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
        print(f"気象庁データ解析エラー: {e}")
        return [], []
    return warning_alerts, advisory_alerts

# ==============================================================
# Slack 送信
# ==============================================================

def build_slack_message(diff: dict, all_active: dict,
                        curr_jma_active: list, now_str: str) -> str:
    """差分情報をもとにSlackメッセージを組み立てる"""

    # ヘッダー絵文字: アクティブ拠点の最大雨量から決定
    max_mm = max((v["mm"] for v in all_active.values() if v.get("exceeds")), default=0)
    header_emoji = get_level_emoji(max_mm)

    lines = [f"{header_emoji} *【Rain Alert — 状況変化】{now_str}*"]

    # 気象庁警報（新規）
    if diff["new_warnings"]:
        lines.append("")
        lines.append("🚨 *気象庁 警報 新規発表:*")
        for w in diff["new_warnings"]:
            lines.append(f"• {w}")

    # 気象庁警報（解除）
    if diff["lifted"]:
        lines.append("")
        lines.append("✅ *気象庁 警報 解除:*")
        for w in diff["lifted"]:
            lines.append(f"• {w}")

    # 新規検知拠点
    if diff["new_alerts"]:
        lines.append("")
        lines.append("🆕 *新規検知:*")
        for name, info in diff["new_alerts"]:
            area = STATIONS[name]["area"]
            emoji = get_level_emoji(info["mm"])
            lines.append(
                f"{emoji} *{name}* ({area}): {info['mm']}mm/h"
                f"  _{info['level']}_ （最大15分値: {info['max_15min']}mm）"
            )

    # レベル変化拠点
    if diff["intensified"]:
        lines.append("")
        lines.append("⬆️ *雨が強まった:*")
        for name, curr, prev in diff["intensified"]:
            area = STATIONS[name]["area"]
            emoji = get_level_emoji(curr["mm"])
            direction = "⬆" if curr["mm"] > prev.get("mm", 0) else "⬇"
            lines.append(
                f"{emoji} *{name}* ({area}): {prev.get('mm', '?')}mm/h"
                f" {direction} {curr['mm']}mm/h  _{curr['level']}_"
            )

    # 弱まった拠点
    if diff["weakened"]:
        lines.append("")
        lines.append("🌤 *雨が弱まりました:*")
        for name, prev in diff["weakened"]:
            area = STATIONS[name]["area"]
            lines.append(
                f"• *{name}* ({area}): 閾値以下"
                f" （前回: {prev.get('mm', '?')}mm/h）"
            )

    # 現在アクティブ拠点サマリー
    active_names = [n for n, v in all_active.items() if v.get("exceeds")]
    if active_names:
        lines.append("")
        lines.append(f"📋 *現在アクティブ {len(active_names)}拠点:* {' / '.join(active_names)}")
    else:
        lines.append("")
        lines.append("📋 *現在: 全拠点 閾値以下*")

    lines.append("")
    lines.append("_Open-Meteo / 気象庁データに基づく自動通知_")

    return "\n".join(lines)


def send_slack(message: str) -> bool:
    """Slack Workflow Builder Webhook で通知を送る"""
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not webhook_url:
        print("SLACK_WEBHOOK_URL 未設定 — Slack通知スキップ")
        return False

    payload = {"Text": message}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        req = Request(webhook_url, data=body,
                      headers={"Content-Type": "application/json"})
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

    # 前回状態を読み込む
    state = load_state()
    last_notified  = state.get("last_notified", 0)
    prev_stations  = state.get("prev_stations", {})
    prev_jma       = state.get("prev_jma", [])

    # ----------------------------------------------------------
    # チェック1: 気象庁 警報・注意報
    # ----------------------------------------------------------
    print("--- 気象庁 警報・注意報チェック ---")
    warning_data  = fetch_jma_warnings()
    curr_jma_active, curr_jma_advisory = check_jma_warnings(warning_data)
    if curr_jma_active:
        for w in curr_jma_active:
            print(f"  警報検知: {w}")
    else:
        print("  警報・注意報: なし")
    time.sleep(0.5)

    # ----------------------------------------------------------
    # チェック2: Open-Meteo 降水チェック
    # ----------------------------------------------------------
    print(f"--- Open-Meteo 降水チェック (閾値: {HOURLY_RAIN_THRESHOLD}mm/h) ---")
    curr_stations = {}
    for station_name, station_info in STATIONS.items():
        data   = fetch_precipitation(station_info["lat"], station_info["lon"])
        result = check_rain_hourly(data)
        curr_stations[station_name] = result
        status = "検知" if result["exceeds"] else "閾値未満"
        print(f"  {status}: {station_name} {result['mm']:.1f}mm/h [{result['level']}]")
        time.sleep(0.3)

    # ----------------------------------------------------------
    # 差分検出
    # ----------------------------------------------------------
    all_jma_active = curr_jma_active  # 警報は注意報を含まない（通知対象のみ）
    diff = get_diff(prev_stations, curr_stations, prev_jma, all_jma_active)

    print(f"--- 差分チェック: 変化あり={diff['has_changes']} ---")
    if diff["new_alerts"]:
        print(f"  新規検知: {[n for n, _ in diff['new_alerts']]}")
    if diff["intensified"]:
        print(f"  レベル変化: {[n for n, _, _ in diff['intensified']]}")
    if diff["weakened"]:
        print(f"  弱まった: {[n for n, _ in diff['weakened']]}")
    if diff["new_warnings"]:
        print(f"  新規警報: {diff['new_warnings']}")
    if diff["lifted"]:
        print(f"  解除警報: {diff['lifted']}")

    # ----------------------------------------------------------
    # 通知判定
    # ----------------------------------------------------------
    should_notify = diff["has_changes"] and not is_in_cooldown(last_notified)

    if not diff["has_changes"]:
        active_count = sum(1 for v in curr_stations.values() if v["exceeds"])
        print(f"変化なし — 通知スキップ（現在アクティブ: {active_count}拠点）")
    elif is_in_cooldown(last_notified):
        print(f"クールダウン中（{COOLDOWN_MINUTES}分以内に通知済み）— スキップ")
    else:
        now_str = now_jst.strftime("%m/%d %H:%M")
        message = build_slack_message(diff, curr_stations, all_jma_active, now_str)
        print("--- 送信メッセージ ---")
        print(message)
        print("---------------------")
        if send_slack(message):
            last_notified = time.time()

    # ----------------------------------------------------------
    # 状態を保存（毎回。差分検出の基準として次回実行で使用）
    # ----------------------------------------------------------
    save_state(last_notified, curr_stations, all_jma_active)
    print("Rain Alert 完了")


if __name__ == "__main__":
    main()
