#!/usr/bin/python3
"""N5 衝刺模式：每日一則短文法卡＋3 題 Telegram 原生 quiz poll。

為什麼是 poll：網頁流程要點連結、長訊息流程要打字作答，兩條都在實際使用中斷掉。
這裡把內容放進訊息本身、作答只要點一下。題目只從既有題庫抽（grammar-quiz.html
的 grammarCards、vocab-quiz.html 的 vocabCards），**唯讀**解析，不改那兩個檔。

零 LLM 呼叫、純標準庫（/usr/bin/python3）：launchd 起的行程讀不到 keychain，
headless claude 在這條路徑上不可靠。

流程：
  1. 今天的章節＝grammar-daily-progress.md 完成日期為今天的列（17:30 排程剛上架）；
     沒有（17:30 失敗或還沒跑）就從已完成章節抽複習題，照樣發——不留空白日。
  2. 週日在訊息最前面加 3 行週報；最後一次作答超過 3 天就加一行點名。
  3. sendMessage（文法卡＋課程頁連結）→ 3 次 sendPoll（type=quiz、is_anonymous=false，
     否則 Telegram 不會送 poll_answer），每則 poll 寫一行進 quiz/sent.jsonl。
     作答由 tools/quiz_poller.py（tmux session `nihongo`）記進 quiz/answers.jsonl。

同一天只發一次（sent.jsonl 已有今天的非測試列就跳過），所以 launchd 每小時觸發、
由 push-config.json 的 push_hour 把關，改推送時間只要改設定檔一行。

Usage:
  quiz_push.py                    # 預設 dry-run：印出要發的內容，不發送、不寫檔
  quiz_push.py --send             # 正式發送（僅在 push_hour 之後、今天尚未發過時）
  quiz_push.py --date 2026-09-28  # 以指定日期模擬（測 fresh／fallback／週日路徑）
  quiz_push.py --send --test      # 發一題標「（測試）」的 poll，不受時間與每日一次限制
"""
import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUIZ = ROOT / "quiz"
CONFIG = QUIZ / "push-config.json"
SENT = QUIZ / "sent.jsonl"
ANSWERS = QUIZ / "answers.jsonl"
SITE = "https://didiowen.github.io/nihongo"
EXAM = dt.date(2026, 12, 6)


# ── 題庫解析（JS 物件常值，一行一張卡）─────────────────────
def _parse_js(s, i=0):
    """解析單一 JS 值，回傳 (value, next_index)。只支援題庫用到的子集。"""
    while s[i].isspace():
        i += 1
    c = s[i]
    if c == "{":
        obj, i = {}, i + 1
        while True:
            while s[i].isspace() or s[i] == ",":
                i += 1
            if s[i] == "}":
                return obj, i + 1
            m = re.match(r"[A-Za-z_]\w*", s[i:])
            key = m.group(0)
            i += len(key)
            while s[i].isspace():
                i += 1
            assert s[i] == ":", f"expected ':' at {i}: {s[i:i+20]!r}"
            obj[key], i = _parse_js(s, i + 1)
    if c == "[":
        arr, i = [], i + 1
        while True:
            while s[i].isspace() or s[i] == ",":
                i += 1
            if s[i] == "]":
                return arr, i + 1
            v, i = _parse_js(s, i)
            arr.append(v)
    if c in "'\"":
        out, i = [], i + 1
        while s[i] != c:
            if s[i] == "\\":
                i += 1
                out.append({"n": "\n", "t": "\t"}.get(s[i], s[i]))
            else:
                out.append(s[i])
            i += 1
        return "".join(out), i + 1
    m = re.match(r"-?\d+(\.\d+)?|true|false|null", s[i:])
    tok = m.group(0)
    val = {"true": True, "false": False, "null": None}.get(tok)
    if val is None and tok != "null":
        val = float(tok) if "." in tok else int(tok)
    return val, i + len(tok)


def load_cards(path, const_name, first_key):
    lines = path.read_text(encoding="utf-8").split("\n")
    start = next(n for n, l in enumerate(lines) if l.startswith(f"const {const_name} = ["))
    cards = []
    for l in lines[start + 1:]:
        if l.startswith("];"):
            break
        if l.lstrip().startswith("{ " + first_key + ":"):
            cards.append(_parse_js(l.strip())[0])
    return cards


# ── 進度表／章節 ─────────────────────────────────────────
def progress_rows():
    """{項目編號: 完成日期字串或 ''}，只收 ✅ 的列。"""
    rows = {}
    for l in (ROOT / "grammar-daily-progress.md").read_text(encoding="utf-8").split("\n"):
        m = re.match(r"\|\s*(\d+)\s*\|[^|]*\|[^|]*\|\s*✅\s*\|\s*([\d-]*)\s*\|", l)
        if m:
            rows[int(m.group(1))] = m.group(2)
    return rows


def grammar_card_lines(title, limit=5):
    """grammar.md 該章（## 標題一字不差）的前 limit 行：去掉小標、表格、圍欄與粗體。"""
    text = (ROOT / "grammar.md").read_text(encoding="utf-8").split("\n")
    try:
        start = text.index(f"## {title}")
    except ValueError:
        return []
    out = []
    for l in text[start + 1:]:
        if l.startswith("## "):
            break
        s = l.strip()
        if not s or s.startswith(("#", "|", "```", "---", ">")):
            continue
        s = re.sub(r"^[-*] |^\d+\. ", "・", s).replace("**", "").replace("`", "")
        if len(s) > 60:  # 長段落只留第一句，免得五行在手機上攤成二十行
            cut = s.find("。")
            s = s[:cut + 1] if 0 < cut < 60 else s[:59] + "…"
        out.append(s)
        if len(out) == limit:
            break
    return out


# ── 題目 → poll ─────────────────────────────────────────
def _clip(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def grammar_poll(card, chapter_cards):
    if card["type"] == "mc":
        question = card["q"]
        wrong = [c for c in card["choices"] if c != card["answer"]]
    else:
        question = card["q"] + ("\n" + card["zh"] if card.get("zh") else "")
        accepted = set(card.get("accepted", [])) | {card["answer"]}
        # 干擾項先取 explain 裡用 × 標出的真實錯誤，不夠再借同章其他填空的答案
        wrong = re.findall(r"×([^\s，。、；：）)」,]+)", card.get("explain", ""))
        wrong += [c["answer"] for c in chapter_cards if c["type"] == "cloze"]
        wrong = [w for w in dict.fromkeys(wrong) if w not in accepted]
        random.shuffle(wrong)
        wrong = wrong[:3]
    wrong = [w for w in dict.fromkeys(wrong) if len(w) <= 100][:9]
    if len(wrong) < 2 or len(card["answer"]) > 100:
        return None
    return {"ref": card["id"], "question": _clip(f"第 {card['ch']} 章｜{question}", 300),
            "answer": card["answer"], "wrong": wrong, "explain": card.get("explain", "")}


def vocab_poll(card, pool):
    others = [c["meaning"] for c in pool if c["meaning"] != card["meaning"]]
    others = list(dict.fromkeys(others))
    random.shuffle(others)
    if len(others) < 2:
        return None
    word = card["display"] + (f"（{card['kanji']}）" if card.get("kanji") else "")
    return {"ref": f"v{card['batch']}:{card['display']}", "question": f"單字｜「{word}」是什麼意思？",
            "answer": card["meaning"], "wrong": [_clip(o, 100) for o in others[:3]],
            "explain": f"{word} {card.get('reading', '')}＝{card['meaning']}"}


def finalize(p):
    opts = [p["answer"]] + p["wrong"]
    random.shuffle(opts)
    return {"ref": p["ref"], "question": p["question"], "options": opts,
            "correct_option_id": opts.index(p["answer"]),
            "explanation": _clip(p["explain"], 200)}


# ── 帳本 ────────────────────────────────────────────────
def read_jsonl(path):
    if not path.exists():
        return []
    rows = []
    for l in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(l))
        except json.JSONDecodeError:
            pass
    return rows


def ledger_stats(today):
    sent = [r for r in read_jsonl(SENT) if not r.get("test")]
    answers = read_jsonl(ANSWERS)
    by_poll = {r["poll_id"]: r for r in sent}
    week_start = today - dt.timedelta(days=6)
    answered = correct = 0
    seen = set()
    for a in answers:
        s = by_poll.get(a["poll_id"])
        if not s or a["poll_id"] in seen or not a.get("option_ids"):
            continue
        if dt.date.fromisoformat(s["ts"][:10]) < week_start:
            continue
        seen.add(a["poll_id"])
        answered += 1
        correct += a["option_ids"][0] == s["correct_option_id"]
    sent_week = sum(1 for r in sent if dt.date.fromisoformat(r["ts"][:10]) >= week_start)
    last_answer = max((a["ts"][:10] for a in answers), default=None)
    first_sent = min((r["ts"][:10] for r in sent), default=None)
    return sent, sent_week, answered, correct, last_answer, first_sent


def recently_sent_refs(sent, today, days=7):
    cutoff = today - dt.timedelta(days=days)
    return {r["ref"] for r in sent if dt.date.fromisoformat(r["ts"][:10]) >= cutoff}


# ── Telegram ────────────────────────────────────────────
def read_token():
    for l in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if l.startswith("TELEGRAM_BOT_TOKEN="):
            return l.split("=", 1)[1].strip().strip("'\"")
    sys.exit("讀不到 TELEGRAM_BOT_TOKEN（.env）")


def tg(token, method, payload):
    data = urllib.parse.urlencode(
        {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict, bool)) else v
         for k, v in payload.items()}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["result"]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")


def active_mode(cfg, today):
    for name, m in cfg.get("modes", {}).items():
        if m.get("from") and m.get("to") and m["from"] <= today.isoformat() <= m["to"]:
            return name
    return "normal"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="真的發送（預設 dry-run）")
    ap.add_argument("--test", action="store_true", help="只發一題標（測試）的 poll")
    ap.add_argument("--date", help="以指定日期模擬 YYYY-MM-DD")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    now = dt.datetime.now()
    today = dt.date.fromisoformat(args.date) if args.date else now.date()
    mode = active_mode(cfg, today)
    if mode != "normal":
        print(f"WARN: mode '{mode}' 尚未實作，照 normal 發送")
    n_questions = cfg["modes"]["normal"].get("questions", 3)

    sent, sent_week, answered, correct, last_answer, first_sent = ledger_stats(today)
    if args.send and not args.test and not args.date:
        if now.hour < int(cfg["push_hour"]):
            print(f"還沒到 push_hour {cfg['push_hour']}，不發送")
            return 0
        if any(r["ts"][:10] == today.isoformat() for r in sent):
            print("今天已經發過，不重發")
            return 0

    chapters = json.loads((ROOT / "grammar" / "chapters.json").read_text(encoding="utf-8"))
    pairings = json.loads((ROOT / "grammar" / "pairings.json").read_text(encoding="utf-8"))
    gcards = load_cards(ROOT / "grammar-quiz.html", "grammarCards", "id")
    vcards = load_cards(ROOT / "vocab-quiz.html", "vocabCards", "meaning")
    rows = progress_rows()
    done = sorted(n for n, d in rows.items() if d and d <= today.isoformat() and str(n) in chapters)
    fresh = next((n for n, d in rows.items() if d == today.isoformat() and str(n) in chapters), None)
    recent = recently_sent_refs(sent, today)
    random.seed(f"{today}-{args.test}")

    def pick_grammar(pool_chapters, k):
        pool = [c for c in gcards if c["ch"] in pool_chapters and c["id"] not in recent]
        random.shuffle(pool)
        polls = []
        for c in sorted(pool, key=lambda c: c["type"] != "mc"):  # mc 優先
            p = grammar_poll(c, [x for x in gcards if x["ch"] == c["ch"]])
            if p:
                polls.append(p)
            if len(polls) == k:
                break
        return polls

    def chapter_polls(ch, k):  # 該章文法題＋1 題配對單字；不夠就從前面章節補
        batches = pairings.get(str(ch))
        batches = batches if isinstance(batches, list) else ([batches] if batches else [])
        vpool = [c for c in vcards if c.get("batch") in batches]
        polls = pick_grammar([ch], k - (1 if vpool else 0))
        if vpool:
            vp = vocab_poll(random.choice(vpool), vpool if len(vpool) >= 4 else vcards)
            if vp:
                polls.append(vp)
        if len(polls) < k:
            polls += pick_grammar([n for n in done if n != ch], k - len(polls))
        return polls

    # 複習期（push-config.json 的 review）：[from, start) 間完成的章節依序每天推 per_day 章，
    # 推完一輪就回到一般模式（17:30 的 pause_new_chapters_until 應設成複習最後一天）。
    rv = cfg.get("review") or {}
    review_chs = []
    if rv.get("from") and rv.get("start") and today.isoformat() >= rv["start"]:
        seq = sorted(n for n, d in rows.items()
                     if d and rv["from"] <= d < rv["start"] and str(n) in chapters)
        per = int(rv.get("per_day", 1))
        day = (today - dt.date.fromisoformat(rv["start"])).days
        review_chs = seq[day * per:(day + 1) * per]
        total_days = -(-len(seq) // per)

    # ── 題目 ──
    if review_chs:
        polls = []
        for c in review_chs:
            polls += chapter_polls(c, n_questions)
        ch = review_chs[0]
        header = f"複習 第 {day + 1}／{total_days} 天"
    elif fresh:
        ch = fresh
        polls = chapter_polls(ch, n_questions)
        header = f"第 {ch} 章：{chapters[str(ch)]}"
    else:
        ch = random.choice(done[-10:])  # 複習日：從最近十章挑一章當文法卡
        polls = pick_grammar(done, n_questions)
        header = f"複習｜第 {ch} 章：{chapters[str(ch)]}"
    if args.test:
        polls = polls[:1]
        for p in polls:
            p["question"] = _clip("（測試）" + p["question"], 300)
    polls = [finalize(p) for p in polls]

    # ── 訊息 ──
    lines = [f"每日日文 {today.isoformat()}"]
    if today.weekday() == 6 and not args.test:
        rate = f"{correct / answered:.0%}" if answered else "—"
        lines += ["📊 本週週報",
                  f"作答 {answered}／{sent_week} 題，答對率 {rate}",
                  f"距離 N5 考試（{EXAM.isoformat()}）還有 {(EXAM - today).days} 天"]
    ref_day = last_answer or first_sent
    if ref_day and (today - dt.date.fromisoformat(ref_day)).days > 3 and not args.test:
        lines.append("📣 點名：已經超過 3 天沒作答了——要繼續、調整，還是暫停？回覆一聲就好。")
    if review_chs:
        lines += ["", header]
        for c in review_chs:
            lines += ["", f"第 {c} 章：{chapters[str(c)]}"] + grammar_card_lines(chapters[str(c)])
        lines += [""] + [f"課程頁 {SITE}/grammar/{c:02d}.html" for c in review_chs]
    else:
        lines += ["", header] + grammar_card_lines(chapters[str(ch)])
        lines += ["", f"課程頁 {SITE}/grammar/{ch:02d}.html"]
    lines.append(f"下面 {len(polls)} 題，點一下作答 👇")
    if args.test:
        lines = ["（測試）N5 衝刺模式 poll 測試，請忽略。"]
    text = "\n".join(lines)

    if not args.send:
        print(f"[dry-run] mode={mode} fresh={fresh} chapter={ch} "
              f"cards: grammar={len(gcards)} vocab={len(vcards)}")
        print("----- message -----\n" + text + "\n----- polls -----")
        for p in polls:
            print(json.dumps(p, ensure_ascii=False))
        return 0

    token = read_token()
    tg(token, "sendMessage", {"chat_id": cfg["chat_id"], "text": text,
                              "link_preview_options": {"is_disabled": True}})
    QUIZ.mkdir(exist_ok=True)
    for p in polls:
        msg = tg(token, "sendPoll", {
            "chat_id": cfg["chat_id"], "question": p["question"],
            "options": [{"text": o} for o in p["options"]],
            "type": "quiz", "is_anonymous": False,
            "correct_option_id": p["correct_option_id"], "explanation": p["explanation"]})
        rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "poll_id": msg["poll"]["id"],
               "ref": p["ref"], "correct_option_id": p["correct_option_id"]}
        if args.test:
            rec["test"] = True
        with SENT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"sent poll {rec['poll_id']} ({p['ref']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
