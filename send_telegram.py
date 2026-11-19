import os, glob, time, re, requests
TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
BASE = f'https://api.telegram.org/bot{TOKEN}'
MAX_LEN = 3800
def get_report_date():
    # summary.md 첫 줄 "# ... - YYYY-MM-DD"에서 날짜를 뽑는다. 없으면 오늘(KST) 날짜.
    try:
        with open('summary.md', encoding='utf-8') as f:
            first = f.readline()
        m = re.search(r'(\d{4}-\d{2}-\d{2})', first)
        if m: return m.group(1)
    except Exception: pass
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d')
def send_text(text):
    r = requests.post(f'{BASE}/sendMessage', json={'chat_id': CHAT_ID, 'text': text, 'disable_web_page_preview': True}, timeout=30)
    print('sendMessage:', r.status_code, r.text[:300])
    if r.status_code != 200: raise SystemExit(f'Telegram send failed: {r.text[:500]}')
    time.sleep(0.7)
def split_by_lines(text):
    chunks, cur = [], ''
    for line in text.splitlines(True):
        while len(line) > MAX_LEN:
            chunks.append(line[:MAX_LEN]); line = line[MAX_LEN:]
        if len(cur) + len(line) > MAX_LEN:
            chunks.append(cur); cur = ''
        cur += line
    if cur.strip(): chunks.append(cur)
    return chunks
def main():
    report_date = get_report_date()
    files = sorted(glob.glob('data/telegram/part_*.txt'))
    if not files:
        print('텔레그램 섹션 파일 없음 - main.py가 먼저 실행돼야 함'); return
    all_chunks = []
    for fp in files:
        with open(fp, encoding='utf-8') as f: all_chunks.extend(split_by_lines(f.read()))
    total = len(all_chunks)
    print(f'[{report_date}] 총 {total}개 메시지로 전송 (파일 {len(files)}개)')
    for i, chunk in enumerate(all_chunks, 1):
        # 날짜를 매 메시지 헤더에 포함 - 이게 이번 요청 사항
        send_text(f'📊 {report_date} 옵션월 리포트 ({i}/{total})\n\n{chunk}')
    with open('summary.md', 'rb') as f:
        r = requests.post(f'{BASE}/sendDocument', data={'chat_id': CHAT_ID, 'caption': f'📄 전체 리포트 원본 ({report_date} / summary.md)'}, files={'document': (f'summary-{report_date}.md', f)}, timeout=60)
    print('sendDocument:', r.status_code, r.text[:300])
if __name__ == '__main__':
    main()
