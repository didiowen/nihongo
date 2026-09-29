#!/usr/bin/python3
"""@ines_jpy_bot 唯一的 getUpdates 消費者：只收 poll_answer，逐筆寫進 quiz/answers.jsonl。

跑在 tmux session `nihongo`（X/scripts/bots-autostart.sh），取代原本閒置的 ctb 對話
bot。同一支 token 只能有一個 poller——第二個會讓兩邊互踢 409——所以這裡遇到 409
只記 log、等 30 秒再試，不自行退出造成 watchdog 反覆重啟。

offset 存在 quiz/.poller-offset：已寫進 JSONL 的更新才確認，重啟不重複也不遺漏。
poll_answer 只會送來非匿名 poll 的作答（quiz_push.py 發的都是 is_anonymous=false）。

Usage:
  quiz_poller.py          # 預設 dry-run：取一次更新、印出會記什麼，不推進 offset、不寫檔
  quiz_poller.py --run    # 常駐迴圈
"""
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANSWERS = ROOT / "quiz" / "answers.jsonl"
OFFSET = ROOT / "quiz" / ".poller-offset"


def log(msg):
    print(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def read_token():
    for l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if l.startswith("TELEGRAM_BOT_TOKEN="):
            return l.split("=", 1)[1].strip().strip("'\"")
    sys.exit("讀不到 TELEGRAM_BOT_TOKEN（.env）")


def get_updates(token, offset, timeout):
    q = urllib.parse.urlencode({"offset": offset, "timeout": timeout,
                                "allowed_updates": json.dumps(["poll_answer"])})
    url = f"https://api.telegram.org/bot{token}/getUpdates?{q}"
    with urllib.request.urlopen(url, timeout=timeout + 10) as r:
        return json.load(r)["result"]


def main():
    run = "--run" in sys.argv
    token = read_token()
    offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
    log(f"quiz_poller start (run={run}, offset={offset})")
    while True:
        try:
            updates = get_updates(token, offset, 50 if run else 0)
        except urllib.error.HTTPError as e:
            log(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
            if not run:
                return 1
            time.sleep(30)
            continue
        except Exception as e:  # noqa: BLE001 — 網路抖動：稍等再試
            log(f"error: {e}")
            if not run:
                return 1
            time.sleep(5)
            continue
        for u in updates:
            pa = u.get("poll_answer")
            if pa:
                rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"),
                       "poll_id": pa["poll_id"], "option_ids": pa.get("option_ids", [])}
                if run:
                    ANSWERS.parent.mkdir(exist_ok=True)
                    with ANSWERS.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(rec) + "\n")
                log(("recorded " if run else "[dry-run] would record ") + json.dumps(rec))
            offset = u["update_id"] + 1
        if not run:
            log(f"[dry-run] {len(updates)} update(s); offset not advanced")
            return 0
        if updates:
            OFFSET.write_text(str(offset))


if __name__ == "__main__":
    sys.exit(main())
