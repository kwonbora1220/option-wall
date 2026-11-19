import os
import glob
import time
import requests

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BASE = f"https://api.telegram.org/bot{TOKEN}"
MAX_LEN = 3800  # 텔레그램 제한 4096, 여유 두고 3800

def split_by_lines(text, max_len=MAX_LEN):
    """줄 단위로 쪼개기 - 표 중간에서 안 잘림"""
    chunks = []
    cur = ""
    for line in text.splitlines(True):
        # 한 줄 자체가 너무 길면 그 줄만 쪼갬
        while len(line) > max_len:
            chunks.append(line[:max_len])
            line = line[max_len:]
        if len(cur) + len(line) > max_len:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        chunks.append(cur)
    return chunks

def send_text(text):
    r = requests.post(
        f"{BASE}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    print("sendMessage:", r.status_code, r.text[:300])
    if r.status_code != 200:
        raise SystemExit(f"Telegram send failed: {r.text[:500]}")
    time.sleep(0.7)

def main():
    # 1. main.py가 만들어둔 part 파일들을 순서대로 전부 보냄
    part_files = sorted(glob.glob("data/telegram/part_*.txt"))
    if not part_files:
        # part 파일이 없으면 summary.md를 직접 쪼개서 보냄 (안전장치)
        print("part files not found, fallback to summary.md")
        with open("summary.md", "r", encoding="utf-8") as f:
            text = f.read()
        chunks = split_by_lines(text)
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            send_text(f"📊 오늘 옵션월 리포트 ({i+1}/{total})\n\n{chunk}")
        return

    # part 파일 -> 각각 다시 줄 단위로 쪼개서 전체 메시지 목록 만들기
    all_chunks = []
    for pf in part_files:
        with open(pf, "r", encoding="utf-8") as f:
            all_chunks.extend(split_by_lines(f.read()))

    total = len(all_chunks)
    print(f"Telegram: {len(part_files)} part files -> {total} messages, will send ALL (no truncation)")
    for i, chunk in enumerate(all_chunks):
        send_text(f"📊 오늘 옵션월 리포트 ({i+1}/{total})\n\n{chunk}")

    # 2. 마지막에 summary.md 원본을 문서로도 한 번 더 보냄 (잘려도 파일로 전부 확인 가능)
    if os.path.exists("summary.md"):
        with open("summary.md", "rb") as f:
            r = requests.post(
                f"{BASE}/sendDocument",
                data={"chat_id": CHAT_ID, "caption": "📄 전체 리포트 원본 (summary.md)"},
                files={"document": ("summary.md", f)},
                timeout=60,
            )
        print("sendDocument:", r.status_code, r.text[:300])
        if r.status_code != 200:
            print("WARNING: sendDocument failed, but text messages already sent")

if __name__ == "__main__":
    main()
