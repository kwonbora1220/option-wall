import pandas as pd
import numpy as np
import requests
import os
import re
import glob
from datetime import datetime, timedelta

TICKERS = ["DKNG", "RKLB", "WULF", "SOXL", "META", "BE"]
DAYS_LIMIT = 365
WHALE_PREMIUM = 1000000
UNUSUAL_VOL_OI = 1.0
WALL_SHARE = 0.15
OI_SURGE_MIN = 500
OI_SURGE_PCT = 0.5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
}

def safe_float(v, default=0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except:
        return default

def safe_int(v, default=0):
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except:
        return default

def parse_occ_symbol(option_str, ticker_symbol):
    try:
        s = str(option_str).strip().upper()
        if s.startswith(ticker_symbol.upper()):
            rest = s[len(ticker_symbol):]
        else:
            m = re.search(r"(\d{6})([CP])(\d{8})", s)
            if not m:
                return None, None, None
            yy, mm, dd = m.group(1)[:2], m.group(1)[2:4], m.group(1)[4:6]
            exp_str = f"20{yy}-{mm}-{dd}"
            opt_type = "CALL" if m.group(2) == "C" else "PUT"
            strike = int(m.group(3)) / 1000.0
            return exp_str, opt_type, strike
        if len(rest) < 15:
            return None, None, None
        date_part = rest[:6]
        type_char = rest[6]
        strike_part = rest[7:15]
        yy, mm, dd = date_part[:2], date_part[2:4], date_part[4:6]
        exp_str = f"20{yy}-{mm}-{dd}"
        opt_type = "CALL" if type_char == "C" else "PUT" if type_char == "P" else None
        strike = int(strike_part) / 1000.0
        return exp_str, opt_type, strike
    except Exception:
        return None, None, None

def load_prev_day_data(ticker_symbol, today_str):
    files = sorted(glob.glob(f"data/*_{ticker_symbol}.csv"))
    candidates = []
    for f in files:
        base = os.path.basename(f)
        if "_summary" in base or "_alerts" in base or "_predict" in base:
            continue
        if base.startswith(today_str):
            continue
        candidates.append(f)
    if not candidates:
        return None
    prev_file = candidates[-1]
    try:
        df = pd.read_csv(prev_file)
        print(f"  {ticker_symbol} 전일 비교 파일: {prev_file} ({len(df)}행)")
        return df
    except Exception as e:
        print(f"  {ticker_symbol} 전일 파일 읽기 실패: {e}")
        return None

def add_oi_change(df_today, df_prev):
    """FIX 1: 전일 데이터 없으면 오늘 OI를 증가량으로 찍지 않음"""
    df_today = df_today.copy()
    has_prev = df_prev is not None and not df_prev.empty
    df_today['has_prev'] = has_prev
    if not has_prev:
        df_today['prev_OI'] = np.nan
        df_today['prev_exists'] = False
        df_today['OI_change'] = 0
        df_today['OI_change_pct'] = 0.0
        return df_today
    prev_map = {}
    for _, r in df_prev.iterrows():
        key = (str(r.get('expiration')), str(r.get('type')), float(r.get('strike', 0)))
        prev_map[key] = safe_int(r.get('openInterest', 0))
    prev_ois, prev_exists = [], []
    for _, r in df_today.iterrows():
        key = (str(r['expiration']), str(r['type']), float(r['strike']))
        if key in prev_map:
            prev_ois.append(prev_map[key]); prev_exists.append(True)
        else:
            prev_ois.append(np.nan); prev_exists.append(False)
    df_today['prev_OI'] = prev_ois
    df_today['prev_exists'] = prev_exists
    df_today['OI_change'] = np.where(df_today['prev_exists'], df_today['openInterest'] - df_today['prev_OI'].fillna(0), 0)
    df_today['OI_change_pct'] = np.where((df_today['prev_exists']) & (df_today['prev_OI'] > 0), df_today['OI_change'] / df_today['prev_OI'], 0.0)
    return df_today

def get_all_options(ticker_symbol, today_str):
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker_symbol}.json"
    print(f"--- {ticker_symbol} CBOE에서 수집 시작 ---")
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"  {ticker_symbol} CBOE 응답 실패: {r.status_code}")
            return None, 0
        data = r.json()
    except Exception as e:
        print(f"  {ticker_symbol} CBOE 요청 실패: {e}")
        return None, 0
    options = data.get("data", {}).get("options", [])
    if not options:
        print(f"  {ticker_symbol} 옵션 데이터 없음")
        return None, 0
    today = datetime.now()
    limit_date = today + timedelta(days=DAYS_LIMIT)
    rows = []
    current_price = get_current_price(ticker_symbol)
    print(f"  {ticker_symbol} 현재가: {current_price}, 전체 옵션 수: {len(options)}")
    for o in options:
        try:
            exp_str, opt_type, strike = parse_occ_symbol(o.get("option", ""), ticker_symbol)
            if not exp_str or not opt_type or strike is None:
                continue
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
            if exp_date > limit_date:
                continue
            if exp_date < today - timedelta(days=1):
                continue
            if strike <= 0:
                continue
            bid = safe_float(o.get("bid", 0)); ask = safe_float(o.get("ask", 0))
            vol = safe_int(o.get("volume", 0)); oi = safe_int(o.get("open_interest", 0))
            last = safe_float(o.get("last_trade_price", 0)); iv = safe_float(o.get("iv", 0))
            mid = (bid + ask) / 2
            rows.append({'ticker': ticker_symbol,'expiration': exp_str,'days_to_expiry': (exp_date - today).days,'type': opt_type,'strike': strike,'lastPrice': last,'bid': bid,'ask': ask,'volume': vol,'openInterest': oi,'impliedVolatility': iv,'mid_price': mid,'premium_est': vol * mid * 100,'vol_oi_ratio': vol / oi if oi > 0 else 0,'current_price': current_price})
        except Exception:
            continue
    print(f"  {ticker_symbol} 파싱 결과: 성공 {len(rows)}개")
    if not rows:
        return None, current_price
    df = pd.DataFrame(rows)
    df_prev = load_prev_day_data(ticker_symbol, today_str)
    df = add_oi_change(df, df_prev)
    return df, current_price

def get_current_price(ticker_symbol):
    try:
        r = requests.get(f"https://stooq.com/q/l/?s={ticker_symbol.lower()}.us&f=sd2t2ohlcv&h&e=csv", headers=HEADERS, timeout=15)
        if r.status_code == 200 and "N/D" not in r.text:
            lines = r.text.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split(",")
                close = safe_float(parts[6] if len(parts) > 6 else 0)
                if close > 0:
                    return close
    except Exception as e:
        print(f"  현재가 stooq 실패: {e}")
    for asset in ["stocks", "etf"]:
        try:
            r = requests.get(f"https://api.nasdaq.com/api/quote/{ticker_symbol}/info?assetclass={asset}", headers={**HEADERS, "Accept-Language": "en-US"}, timeout=15)
            if r.status_code == 200:
                j = r.json()
                if j and isinstance(j, dict):
                    d = j.get("data") or {}; pd_data = d.get("primaryData") or {}
                    price = str(pd_data.get("lastSalePrice", "")).replace("$", "").replace(",", "")
                    if price:
                        return safe_float(price)
        except Exception as e:
            print(f"  현재가 nasdaq({asset}) 실패: {e}")
    return 0

def calc_wall_and_pain(df_exp, current_price=0):
    """FIX 4: 벽/맥스페인은 현재가 ±20% 안의 행사가만 사용"""
    df_exp = df_exp.fillna(0)
    if current_price and current_price > 0:
        df_near = df_exp[(df_exp['strike'] >= current_price * 0.8) & (df_exp['strike'] <= current_price * 1.2)].copy()
        if len(df_near) < 6:
            df_near = df_exp.copy()
    else:
        df_near = df_exp.copy()
    calls = df_near[df_near['type'] == 'CALL'].copy()
    puts = df_near[df_near['type'] == 'PUT'].copy()
    if calls.empty or puts.empty:
        calls = df_exp[df_exp['type'] == 'CALL'].copy(); puts = df_exp[df_exp['type'] == 'PUT'].copy()
        if calls.empty or puts.empty:
            return None
        df_near = df_exp.copy()
    call_wall_row = calls.loc[calls['openInterest'].idxmax()]
    put_wall_row = puts.loc[puts['openInterest'].idxmax()]
    strikes = np.sort(df_near['strike'].unique())
    call_pain = np.maximum(0, strikes[:, None] - calls['strike'].values[None, :]) @ calls['openInterest'].values
    put_pain = np.maximum(0, puts['strike'].values[None, :] - strikes[:, None]) @ puts['openInterest'].values
    max_pain_price = strikes[np.argmin(call_pain + put_pain)]
    total_call_oi = int(df_exp[df_exp['type'] == 'CALL']['openInterest'].sum())
    total_put_oi = int(df_exp[df_exp['type'] == 'PUT']['openInterest'].sum())
    pc_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else 0
    oi_net_change = int(df_exp['OI_change'].sum()) if 'OI_change' in df_exp.columns else 0
    has_prev = bool(df_exp['has_prev'].iloc[0]) if 'has_prev' in df_exp.columns else False
    return {'call_wall': float(call_wall_row['strike']),'call_wall_OI': int(call_wall_row['openInterest']),'call_wall_OI_change': int(call_wall_row.get('OI_change', 0)),'put_wall': float(put_wall_row['strike']),'put_wall_OI': int(put_wall_row['openInterest']),'put_wall_OI_change': int(put_wall_row.get('OI_change', 0)),'max_pain': float(max_pain_price),'total_call_OI': total_call_oi,'total_put_OI': total_put_oi,'put_call_ratio': round(float(pc_ratio), 2),'total_premium': float(df_exp['premium_est'].sum()),'OI_net_change': oi_net_change,'has_prev': has_prev}

def detect_whale_and_unusual(df_exp, current_price):
    alerts = []
    df_exp = df_exp.fillna(0)
    has_prev = bool(df_exp['has_prev'].iloc[0]) if 'has_prev' in df_exp.columns else False
    # FIX 2: 전일 데이터 없으면 OI_SURGE / OI_DROP 스킵
    if has_prev and 'OI_change' in df_exp.columns:
        base = df_exp[df_exp.get('prev_exists', True) == True] if 'prev_exists' in df_exp.columns else df_exp
        surge = base[(base['OI_change'] >= OI_SURGE_MIN) & (base['OI_change_pct'] >= OI_SURGE_PCT)].sort_values('OI_change', ascending=False)
        for _, r in surge.head(8).iterrows():
            alerts.append({'level': 'OI_SURGE','msg': f"{r['type']} {r['strike']} OI {int(r['prev_OI'])} -> {int(r['openInterest'])} (+{int(r['OI_change'])}, +{r['OI_change_pct']*100:.0f}%) [기존 포지션 대폭 증가] / 거래량 {int(r['volume'])}",'strike': float(r['strike']),'premium': float(r['premium_est']),'type': r['type']})
        drop = base[base['OI_change'] <= -OI_SURGE_MIN].sort_values('OI_change').head(5)
        for _, r in drop.iterrows():
            alerts.append({'level': 'OI_DROP','msg': f"{r['type']} {r['strike']} OI {int(r['prev_OI'])} -> {int(r['openInterest'])} ({int(r['OI_change'])}) [고래 이탈 / 청산 의심]",'strike': float(r['strike']),'premium': float(r['premium_est']),'type': r['type']})
    whales = df_exp[df_exp['premium_est'] >= WHALE_PREMIUM].sort_values('premium_est', ascending=False)
    for _, r in whales.head(5).iterrows():
        prev_txt = f" (전일 OI {int(r['prev_OI'])})" if has_prev and pd.notna(r.get('prev_OI', np.nan)) else " (전일 OI - 기준 수집 중)"
        alerts.append({'level': 'WHALE','msg': f"{r['type']} {r['strike']} 행사가 프리미엄 ${r['premium_est']:,.0f} / 거래량 {int(r['volume'])} / OI {int(r['openInterest'])}{prev_txt}",'strike': float(r['strike']),'premium': float(r['premium_est']),'type': r['type']})
    unusual = df_exp[df_exp['vol_oi_ratio'] >= UNUSUAL_VOL_OI].sort_values('vol_oi_ratio', ascending=False)
    for _, r in unusual.head(5).iterrows():
        if r['premium_est'] < 200000:
            continue
        alerts.append({'level': 'UNUSUAL','msg': f"{r['type']} {r['strike']} Vol/OI {r['vol_oi_ratio']:.2f}배 / 프리미엄 ${r['premium_est']:,.0f}",'strike': float(r['strike']),'premium': float(r['premium_est']),'type': r['type']})
    total_oi = df_exp['openInterest'].sum()
    if total_oi > 0:
        grouped = df_exp.groupby(['type', 'strike'])['openInterest'].sum().reset_index()
        for _, r in grouped.iterrows():
            share = r['openInterest'] / total_oi
            if share >= WALL_SHARE:
                alerts.append({'level': 'WALL_CONCENTRATION','msg': f"{r['type']} {r['strike']}에 전체 OI의 {share*100:.1f}% 집중 (고래 벽 의심)",'strike': float(r['strike']),'premium': 0,'type': r['type']})
    return alerts

def predict_direction(summary_row, current_price):
    """FIX 3: 돌파/이탈 분기 분리 + 중복 제거 + 첫날 OI 문구 제외"""
    score = 0; reasons = []
    cw = summary_row['call_wall']; pw = summary_row['put_wall']; mp = summary_row['max_pain']
    pcr = summary_row['put_call_ratio']; dte = summary_row['days_to_expiry']
    has_prev = summary_row.get('has_prev', True)
    weight = 1.5 if dte <= 7 else 1.0 if dte <= 30 else 0.5
    if current_price and current_price > 0:
        if mp > current_price:
            score += 25 * weight; reasons.append(f"맥스페인({mp})이 현재가보다 위 -> 위로 끌어당김")
        elif mp < current_price:
            score -= 25 * weight; reasons.append(f"맥스페인({mp})이 현재가보다 아래 -> 아래로 끌어당김")
        dist_call = (cw - current_price) / current_price * 100
        dist_put = (current_price - pw) / current_price * 100
        if 0 <= dist_call < 2:
            score -= 20; reasons.append(f"콜월({cw})까지 {dist_call:.1f}%밖에 안남음 -> 저항 강함")
        elif dist_call < 0:
            score += 10; reasons.append(f"콜월({cw}) 돌파 중 -> 상승 모멘텀")
        if 0 <= dist_put < 2:
            score += 20; reasons.append(f"풋월({pw})까지 {dist_put:.1f}%밖에 안남음 -> 지지 강함")
        elif dist_put < 0:
            score -= 10; reasons.append(f"풋월({pw}) 이탈 중 -> 하락 모멘텀")
    if pcr > 1.2:
        score -= 15; reasons.append(f"Put/Call {pcr}로 풋 쏠림 -> 공포 / 하락 베팅 많음")
    elif pcr < 0.7:
        score += 15; reasons.append(f"Put/Call {pcr}로 콜 쏠림 -> 강세 베팅 많음")
    if has_prev:
        oi_net = summary_row.get('OI_net_change', 0)
        if oi_net > 5000:
            reasons.append(f"전체 OI가 어제보다 {oi_net} 증가 -> 새로운 돈 유입 중")
        elif oi_net < -5000:
            reasons.append(f"전체 OI가 어제보다 {abs(oi_net)} 감소 -> 포지션 정리 중")
    else:
        reasons.append("OI 전일 대비: 기준 데이터 수집 중 (내일부터 비교 가능)")
    score = max(-100, min(100, score))
    if score >= 40: verdict = "🟢 강세 우위"
    elif score >= 10: verdict = "🟢 약한 상승 우위"
    elif score <= -40: verdict = "🔴 약세 우위"
    elif score <= -10: verdict = "🔴 약한 하락 우위"
    else: verdict = "⚪ 중립 / 박스권"
    return score, verdict, reasons

def main():
    os.makedirs('data', exist_ok=True)
    today_str = datetime.now().strftime('%Y-%m-%d')
    summary_rows = []; alert_rows = []; predict_rows = []
    for ticker_symbol in TICKERS:
        try:
            df, current_price = get_all_options(ticker_symbol, today_str)
        except Exception as e:
            print(f"!!! {ticker_symbol} 전체 실패 - 건너뜀: {e}"); continue
        if df is None or df.empty:
            print(f"{ticker_symbol} 데이터 없음 - 건너뜀"); continue
        cols = ['ticker','expiration','days_to_expiry','type','strike','lastPrice','bid','ask','volume','openInterest','prev_OI','OI_change','OI_change_pct','impliedVolatility','mid_price','premium_est','vol_oi_ratio','current_price','has_prev','prev_exists']
        cols = [c for c in cols if c in df.columns]
        df[cols].to_csv(f'data/{today_str}_{ticker_symbol}.csv', index=False)
        for exp in sorted(df['expiration'].unique()):
            df_exp = df[df['expiration'] == exp]
            result = calc_wall_and_pain(df_exp, current_price)
            if not result: continue
            dte = int(df_exp.iloc[0]['days_to_expiry'])
            row = {'date': today_str, 'ticker': ticker_symbol,'current_price': round(float(current_price), 2) if current_price else 0,'expiration': exp, 'days_to_expiry': dte, **result}
            summary_rows.append(row)
            if dte <= 45:
                alerts = detect_whale_and_unusual(df_exp, current_price)
                for a in alerts:
                    alert_rows.append({'date': today_str, 'ticker': ticker_symbol, 'expiration': exp, 'days_to_expiry': dte, **a})
            if exp in sorted(df['expiration'].unique())[:3]:
                score, verdict, reasons = predict_direction(row, current_price)
                predict_rows.append({'date': today_str, 'ticker': ticker_symbol, 'expiration': exp,'days_to_expiry': dte, 'current_price': row['current_price'],'call_wall': row['call_wall'], 'put_wall': row['put_wall'], 'max_pain': row['max_pain'],'score': score, 'verdict': verdict, 'reasons': " | ".join(reasons)})
        print(f"{ticker_symbol} 완료")
    summary_df = pd.DataFrame(summary_rows)
    if not summary_df.empty:
        summary_df.to_csv(f'data/{today_str}_summary.csv', index=False)
    alert_df = pd.DataFrame(alert_rows)
    if not alert_df.empty:
        alert_df.to_csv(f'data/{today_str}_alerts.csv', index=False)
    else:
        pd.DataFrame(columns=['ticker','expiration','level','msg']).to_csv(f'data/{today_str}_alerts.csv', index=False)
    predict_df = pd.DataFrame(predict_rows)
    if not predict_df.empty:
        predict_df.to_csv(f'data/{today_str}_predict.csv', index=False)
    with open('summary.md', 'w', encoding='utf-8') as f:
        f.write(f"# Option Wall + Whale + OI변화 + 예측 - {today_str}\n\n")
        f.write("> ⚠ 이 예측은 옵션 포지셔닝 기반 참고용입니다. 확정적인 방향이 아닙니다. 현물 거래의 보조 지표로만 사용하세요.\n\n")
        if not predict_df.empty:
            f.write("## 🎯 방향 예측 (가까운 만기 3개 기준)\n\n")
            f.write("| 종목 | 만기 | D-day | 현재가 | 콜월 | 풋월 | 맥스페인 | 점수 | 판단 |\n|---|---|---|---|---|---|---|---|---|\n")
            for _, r in predict_df.iterrows():
                f.write(f"| {r['ticker']} | {r['expiration']} | {r['days_to_expiry']} | {r['current_price']} | {r['call_wall']} | {r['put_wall']} | {r['max_pain']} | {r['score']} | {r['verdict']} |\n")
            f.write("\n### 판단 근거\n\n")
            for _, r in predict_df.iterrows():
                f.write(f"- **{r['ticker']} {r['expiration']} ({r['verdict']})**: {r['reasons']}\n")
            f.write("\n")
        if not alert_df.empty:
            oi_surge = alert_df[alert_df['level'] == 'OI_SURGE']
            if not oi_surge.empty:
                f.write("## 🔥 OI 급증 - 고래 신규 진입 (전일 대비)\n\n")
                for ticker_symbol in TICKERS:
                    sub = oi_surge[oi_surge['ticker'] == ticker_symbol]
                    if sub.empty: continue
                    f.write(f"### {ticker_symbol}\n\n")
                    for _, r in sub.iterrows():
                        f.write(f"- 🔥 [{r['expiration']}] {r['msg']}\n")
                    f.write("\n")
            oi_drop = alert_df[alert_df['level'] == 'OI_DROP']
            if not oi_drop.empty:
                f.write("## 📉 OI 급감 - 고래 이탈 / 청산\n\n")
                for ticker_symbol in TICKERS:
                    sub = oi_drop[oi_drop['ticker'] == ticker_symbol]
                    if sub.empty: continue
                    f.write(f"### {ticker_symbol}\n\n")
                    for _, r in sub.iterrows():
                        f.write(f"- 📉 [{r['expiration']}] {r['msg']}\n")
                    f.write("\n")
        if not alert_df.empty:
            f.write("## 🐋 고래 / 이상거래 알림 (D-day 45일 이내)\n\n")
            for ticker_symbol in TICKERS:
                sub = alert_df[alert_df['ticker'] == ticker_symbol]
                if sub.empty: continue
                f.write(f"### {ticker_symbol}\n\n")
                for _, r in sub.iterrows():
                    emoji = {"OI_SURGE":"🔥","OI_DROP":"📉","WHALE":"🐋","UNUSUAL":"⚠"}.get(r['level'], "🧱")
                    f.write(f"- {emoji} [{r['expiration']}] {r['msg']}\n")
                f.write("\n")
        else:
            f.write("## 🐋 고래 / 이상거래 알림\n\n- 오늘은 특이한 고래 흔적 없음\n\n")
        if not summary_df.empty:
            f.write("## 📊 전체 만기 요약 (1년 이내 전부)\n\n")
            for ticker_symbol in TICKERS:
                sub = summary_df[summary_df['ticker'] == ticker_symbol]
                if sub.empty: continue
                f.write(f"### {ticker_symbol} - 현재가 {sub.iloc[0]['current_price']}\n\n")
                f.write("| 만기 | D-day | 콜월 | 콜월 OI변화 | 풋월 | 풋월 OI변화 | 맥스페인 | P/C | OI순증가 |\n|---|---|---|---|---|---|---|---|---|\n")
                for _, r in sub.iterrows():
                    oi_net = 0 if not r.get('has_prev', True) else r.get('OI_net_change', 0)
                    cw_change = 0 if not r.get('has_prev', True) else r.get('call_wall_OI_change', 0)
                    pw_change = 0 if not r.get('has_prev', True) else r.get('put_wall_OI_change', 0)
                    f.write(f"| {r['expiration']} | {r['days_to_expiry']} | {r['call_wall']} | {cw_change:+} | {r['put_wall']} | {pw_change:+} | {r['max_pain']} | {r['put_call_ratio']} | {oi_net:+} |\n")
                f.write("\n")
    print("완료 - summary.md 생성")

if __name__ == "__main__":
    main()
