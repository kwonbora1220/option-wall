import pandas as pd
import numpy as np
import requests
import os
import time
from datetime import datetime, timedelta

# ============================================================
# 설정 - 여기서만 바꾸면 됨
# ============================================================
TICKERS = ["DKNG", "RKLB", "WULF", "SOXL", "META", "BE"]
DAYS_LIMIT = 365
WHALE_PREMIUM = 1000000
UNUSUAL_VOL_OI = 1.0
WALL_SHARE = 0.15

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
}

def get_all_options(ticker_symbol):
    """CBOE에서 1년 이내 모든 옵션 가져오기 - 야후 사용 안함"""
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

    # 현재가 = 가장 가까운 만기 ATM 근처 옵션의 underlying 가격으로 추정
    # CBOE 데이터에 underlying이 없으므로, 별도로 현재가 가져오기
    current_price = get_current_price(ticker_symbol)
    print(f"  {ticker_symbol} 현재가: {current_price}, 전체 옵션 수: {len(options)}")

    for o in options:
        try:
            # CBOE 날짜 형식: 2026-11-27T00:00:00
            exp_str = o.get("expiration_date", "")[:10]
            if not exp_str:
                continue
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
            if exp_date > limit_date:
                continue
            if exp_date < today - timedelta(days=1):
                continue

            opt_type = o.get("option_type", "").upper()  # CALL / PUT
            if opt_type not in ["CALL", "PUT"]:
                continue

            strike = float(o.get("strike_price", 0) or 0)
            bid = float(o.get("bid", 0) or 0)
            ask = float(o.get("ask", 0) or 0)
            vol = int(o.get("volume", 0) or 0)
            oi = int(o.get("open_interest", 0) or 0)
            last = float(o.get("last_trade_price", 0) or 0)
            iv = float(o.get("implied_volatility", 0) or 0)

            mid = (bid + ask) / 2
            rows.append({
                'ticker': ticker_symbol,
                'expiration': exp_str,
                'days_to_expiry': (exp_date - today).days,
                'type': opt_type,
                'strike': strike,
                'lastPrice': last,
                'bid': bid,
                'ask': ask,
                'volume': vol,
                'openInterest': oi,
                'impliedVolatility': iv,
                'mid_price': mid,
                'premium_est': vol * mid * 100,
                'vol_oi_ratio': vol / oi if oi > 0 else 0,
                'current_price': current_price,
            })
        except Exception:
            continue

    if not rows:
        return None, current_price
    return pd.DataFrame(rows), current_price

def get_current_price(ticker_symbol):
    """현재가 가져오기 - CBOE 주가 API (야후 안씀)"""
    # 1순위: CBOE 주가
    try:
        url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker_symbol}.json"
        # 주가용은 따로 stock API 사용
        url2 = f"https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_SPX.json"
        # 간단하게 stooq 무료 API 사용
        r = requests.get(f"https://stooq.com/q/l/?s={ticker_symbol.lower()}.us&f=sd2t2ohlcv&h&e=csv", headers=HEADERS, timeout=15)
        if r.status_code == 200 and "N/D" not in r.text:
            lines = r.text.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split(",")
                # stooq: Symbol,Date,Time,Open,High,Low,Close,Volume
                close = float(parts[6])
                if close > 0:
                    return close
    except Exception as e:
        print(f"  현재가 stooq 실패: {e}")

    # 2순위: Nasdaq 현재가
    try:
        r = requests.get(f"https://api.nasdaq.com/api/quote/{ticker_symbol}/info?assetclass=stocks", headers={**HEADERS, "Accept-Language": "en-US"}, timeout=15)
        if r.status_code == 200:
            price = r.json().get("data", {}).get("primaryData", {}).get("lastSalePrice", "")
            price = price.replace("$", "").replace(",", "")
            if price:
                return float(price)
    except Exception as e:
        print(f"  현재가 nasdaq 실패: {e}")
    return 0

def calc_wall_and_pain(df_exp):
    df_exp = df_exp.fillna(0)
    calls = df_exp[df_exp['type'] == 'CALL'].copy()
    puts = df_exp[df_exp['type'] == 'PUT'].copy()
    if calls.empty or puts.empty:
        return None

    call_wall_row = calls.loc[calls['openInterest'].idxmax()]
    put_wall_row = puts.loc[puts['openInterest'].idxmax()]

    strikes = np.sort(df_exp['strike'].unique())
    call_pain = np.maximum(0, strikes[:, None] - calls['strike'].values[None, :]) @ calls['openInterest'].values
    put_pain = np.maximum(0, puts['strike'].values[None, :] - strikes[:, None]) @ puts['openInterest'].values
    max_pain_price = strikes[np.argmin(call_pain + put_pain)]

    total_call_oi = calls['openInterest'].sum()
    total_put_oi = puts['openInterest'].sum()
    pc_ratio = total_put_oi / total_call_oi if total_call_oi > 0 else 0

    return {
        'call_wall': float(call_wall_row['strike']),
        'call_wall_OI': int(call_wall_row['openInterest']),
        'put_wall': float(put_wall_row['strike']),
        'put_wall_OI': int(put_wall_row['openInterest']),
        'max_pain': float(max_pain_price),
        'total_call_OI': int(total_call_oi),
        'total_put_OI': int(total_put_oi),
        'put_call_ratio': round(float(pc_ratio), 2),
        'total_premium': float(df_exp['premium_est'].sum()),
    }

def detect_whale_and_unusual(df_exp, current_price):
    alerts = []
    df_exp = df_exp.fillna(0)

    whales = df_exp[df_exp['premium_est'] >= WHALE_PREMIUM].sort_values('premium_est', ascending=False)
    for _, r in whales.head(5).iterrows():
        alerts.append({
            'level': 'WHALE',
            'msg': f"{r['type']} {r['strike']} 행사가 프리미엄 ${r['premium_est']:,.0f} / 거래량 {int(r['volume'])} / OI {int(r['openInterest'])}",
            'strike': float(r['strike']),
            'premium': float(r['premium_est']),
            'type': r['type']
        })

    unusual = df_exp[df_exp['vol_oi_ratio'] >= UNUSUAL_VOL_OI].sort_values('vol_oi_ratio', ascending=False)
    for _, r in unusual.head(5).iterrows():
        if r['premium_est'] < 200000:
            continue
        alerts.append({
            'level': 'UNUSUAL',
            'msg': f"{r['type']} {r['strike']} Vol/OI {r['vol_oi_ratio']:.2f}배 / 프리미엄 ${r['premium_est']:,.0f}",
            'strike': float(r['strike']),
            'premium': float(r['premium_est']),
            'type': r['type']
        })

    total_oi = df_exp['openInterest'].sum()
    if total_oi > 0:
        grouped = df_exp.groupby(['type', 'strike'])['openInterest'].sum().reset_index()
        for _, r in grouped.iterrows():
            share = r['openInterest'] / total_oi
            if share >= WALL_SHARE:
                alerts.append({
                    'level': 'WALL_CONCENTRATION',
                    'msg': f"{r['type']} {r['strike']}에 전체 OI의 {share*100:.1f}% 집중 (고래 벽 의심)",
                    'strike': float(r['strike']),
                    'premium': 0,
                    'type': r['type']
                })
    return alerts

def predict_direction(summary_row, current_price):
    score = 0
    reasons = []
    cw = summary_row['call_wall']
    pw = summary_row['put_wall']
    mp = summary_row['max_pain']
    pcr = summary_row['put_call_ratio']

    dte = summary_row['days_to_expiry']
    weight = 1.5 if dte <= 7 else 1.0 if dte <= 30 else 0.5
    if current_price and current_price > 0:
        if mp > current_price:
            score += 25 * weight
            reasons.append(f"맥스페인({mp})이 현재가보다 위 -> 위로 끌어당김")
        elif mp < current_price:
            score -= 25 * weight
            reasons.append(f"맥스페인({mp})이 현재가보다 아래 -> 아래로 끌어당김")

        dist_call = (cw - current_price) / current_price * 100
        dist_put = (current_price - pw) / current_price * 100
        if dist_call < 2:
            score -= 20
            reasons.append(f"콜월({cw})까지 {dist_call:.1f}%밖에 안남음 -> 저항 강함")
        if dist_put < 2:
            score += 20
            reasons.append(f"풋월({pw})까지 {dist_put:.1f}%밖에 안남음 -> 지지 강함")

    if pcr > 1.2:
        score -= 15
        reasons.append(f"Put/Call {pcr}로 풋 쏠림 -> 공포 / 하락 베팅 많음")
    elif pcr < 0.7:
        score += 15
        reasons.append(f"Put/Call {pcr}로 콜 쏠림 -> 강세 베팅 많음")

    if current_price and current_price > cw:
        score += 10
        reasons.append("콜월 돌파 중 -> 상승 모멘텀")
    if current_price and current_price < pw:
        score -= 10
        reasons.append("풋월 이탈 중 -> 하락 모멘텀")

    score = max(-100, min(100, score))
    if score >= 40:
        verdict = "🟢 강세 우위"
    elif score >= 10:
        verdict = "🟢 약한 상승 우위"
    elif score <= -40:
        verdict = "🔴 약세 우위"
    elif score <= -10:
        verdict = "🔴 약한 하락 우위"
    else:
        verdict = "⚪ 중립 / 박스권"

    return score, verdict, reasons

def main():
    os.makedirs('data', exist_ok=True)
    today_str = datetime.now().strftime('%Y-%m-%d')
    summary_rows = []
    alert_rows = []
    predict_rows = []

    for ticker_symbol in TICKERS:
        try:
            df, current_price = get_all_options(ticker_symbol)
        except Exception as e:
            print(f"!!! {ticker_symbol} 전체 실패 - 건너뜀: {e}")
            continue
        if df is None or df.empty:
            print(f"{ticker_symbol} 데이터 없음 - 건너뜀")
            continue

        cols = ['ticker','expiration','days_to_expiry','type','strike','lastPrice','bid','ask',
                'volume','openInterest','impliedVolatility','mid_price','premium_est','vol_oi_ratio','current_price']
        cols = [c for c in cols if c in df.columns]
        df[cols].to_csv(f'data/{today_str}_{ticker_symbol}.csv', index=False)

        for exp in sorted(df['expiration'].unique()):
            df_exp = df[df['expiration'] == exp]
            result = calc_wall_and_pain(df_exp)
            if not result:
                continue
            dte = int(df_exp.iloc[0]['days_to_expiry'])
            row = {
                'date': today_str, 'ticker': ticker_symbol,
                'current_price': round(float(current_price), 2) if current_price else 0,
                'expiration': exp, 'days_to_expiry': dte, **result
            }
            summary_rows.append(row)

            if dte <= 45:
                alerts = detect_whale_and_unusual(df_exp, current_price)
                for a in alerts:
                    alert_rows.append({'date': today_str, 'ticker': ticker_symbol, 'expiration': exp, 'days_to_expiry': dte, **a})

            if exp in sorted(df['expiration'].unique())[:3]:
                score, verdict, reasons = predict_direction(row, current_price)
                predict_rows.append({
                    'date': today_str, 'ticker': ticker_symbol, 'expiration': exp,
                    'days_to_expiry': dte, 'current_price': row['current_price'],
                    'call_wall': row['call_wall'], 'put_wall': row['put_wall'], 'max_pain': row['max_pain'],
                    'score': score, 'verdict': verdict, 'reasons': " | ".join(reasons)
                })
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
        f.write(f"# Option Wall + Whale + 예측 - {today_str}\n\n")
        f.write("> ⚠️ 이 예측은 옵션 포지셔닝 기반 참고용입니다. 확정적인 방향이 아닙니다. 현물 거래의 보조 지표로만 사용하세요.\n\n")

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
            f.write("## 🐋 고래 / 이상거래 알림 (D-day 45일 이내)\n\n")
            for ticker_symbol in TICKERS:
                sub = alert_df[alert_df['ticker'] == ticker_symbol]
                if sub.empty:
                    continue
                f.write(f"### {ticker_symbol}\n\n")
                for _, r in sub.iterrows():
                    emoji = "🐋" if r['level'] == 'WHALE' else "⚠️" if r['level'] == 'UNUSUAL' else "🧱"
                    f.write(f"- {emoji} [{r['expiration']}] {r['msg']}\n")
                f.write("\n")
        else:
            f.write("## 🐋 고래 / 이상거래 알림\n\n- 오늘은 특이한 고래 흔적 없음\n\n")

        if not summary_df.empty:
            f.write("## 📊 전체 만기 요약 (1년 이내 전부)\n\n")
            for ticker_symbol in TICKERS:
                sub = summary_df[summary_df['ticker'] == ticker_symbol]
                if sub.empty:
                    continue
                f.write(f"### {ticker_symbol} - 현재가 {sub.iloc[0]['current_price']}\n\n")
                f.write("| 만기 | D-day | 콜월 | 풋월 | 맥스페인 | P/C | 전체 프리미엄 |\n|---|---|---|---|---|---|---|\n")
                for _, r in sub.iterrows():
                    f.write(f"| {r['expiration']} | {r['days_to_expiry']} | {r['call_wall']} | {r['put_wall']} | {r['max_pain']} | {r['put_call_ratio']} | ${r['total_premium']:,.0f} |\n")
                f.write("\n")
    print("완료 - summary.md 생성")

if __name__ == "__main__":
    main()
