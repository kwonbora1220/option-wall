import yfinance as yf
import pandas as pd
import numpy as np
import os
import glob
from datetime import datetime, timedelta

TICKERS = ["DKNG", "RKLB", "WULF", "SOXL", "META", "BE"]
DAYS_LIMIT = 365

# --- 설정값 ---
WHALE_PREMIUM = 1000000  # 이 금액 이상 거래되면 고래로 판단 (달러)
UNUSUAL_VOL_OI = 1.0     # Volume / OI 비율
WALL_SHARE = 0.15        # 전체 OI 중 한 행사가가 차지하면 벽으로 판단

def get_all_options(ticker_symbol):
    ticker = yf.Ticker(ticker_symbol)
    try:
        current_price = ticker.fast_info.get('lastPrice', 0)
        if not current_price:
            current_price = ticker.info.get('currentPrice', 0) or 0
    except:
        current_price = 0

    today = datetime.now()
    limit_date = today + timedelta(days=DAYS_LIMIT)
    all_rows = []
    print(f"--- {ticker_symbol} 수집 시작 (현재가: {current_price}) ---")

    for exp_str in ticker.options:
        try:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
            if exp_date > limit_date:
                continue
            chain = ticker.option_chain(exp_str)
            for df_part, typ in [(chain.calls, 'CALL'), (chain.puts, 'PUT')]:
                d = df_part.copy()
                d['type'] = typ
                d['expiration'] = exp_str
                d['ticker'] = ticker_symbol
                d['current_price'] = current_price
                d['days_to_expiry'] = (exp_date - today).days
                # 프리미엄 추정 = 거래량 * 중간가 * 100 (옵션 1계약 = 100주)
                d['mid_price'] = (d['bid'].fillna(0) + d['ask'].fillna(0)) / 2
                d['premium_est'] = d['volume'].fillna(0) * d['mid_price'] * 100
                d['vol_oi_ratio'] = d['volume'].fillna(0) / d['openInterest'].replace(0, np.nan).fillna(0)
                all_rows.append(d)
        except Exception as e:
            print(f"  {exp_str} 실패: {e}")
            continue

    if not all_rows:
        return None, current_price
    return pd.concat(all_rows), current_price

def calc_wall_and_pain(df_exp):
    df_exp = df_exp.fillna(0)
    calls = df_exp[df_exp['type'] == 'CALL'].copy()
    puts = df_exp[df_exp['type'] == 'PUT'].copy()
    if calls.empty or puts.empty:
        return None

    call_wall_row = calls.loc[calls['openInterest'].idxmax()]
    put_wall_row = puts.loc[puts['openInterest'].idxmax()]

    # 맥스페인
    strikes = np.sort(df_exp['strike'].unique())
    call_pain = np.maximum(0, strikes[:, None] - calls['strike'].values[None, :]) @ calls['openInterest'].values
    put_pain = np.maximum(0, puts['strike'].values[None, :] - strikes[:, None]) @ puts['openInterest'].values
    max_pain_price = strikes[np.argmin(call_pain + put_pain)]

    # 감마 노출 근사치 (ATM 근처 OI가 많을수록 딜러 헤지 압력 큼)
    # 여기선 간단히 전체 OI 합으로 대체
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
    """이상거래 / 고래 탐지 - 한 만기 기준"""
    alerts = []
    df_exp = df_exp.fillna(0)

    # 1. 고래 프리미엄 거래
    whales = df_exp[df_exp['premium_est'] >= WHALE_PREMIUM].sort_values('premium_est', ascending=False)
    for _, r in whales.head(5).iterrows():
        alerts.append({
            'level': 'WHALE',
            'msg': f"{r['type']} {r['strike']} 행사가 프리미엄 ${r['premium_est']:,.0f} / 거래량 {int(r['volume'])} / OI {int(r['openInterest'])}",
            'strike': float(r['strike']),
            'premium': float(r['premium_est']),
            'type': r['type']
        })

    # 2. Volume/OI 이상
    unusual = df_exp[df_exp['vol_oi_ratio'] >= UNUSUAL_VOL_OI].sort_values('vol_oi_ratio', ascending=False)
    for _, r in unusual.head(5).iterrows():
        if r['premium_est'] < 200000:  # 너무 작은 금액은 노이즈라 제외
            continue
        alerts.append({
            'level': 'UNUSUAL',
            'msg': f"{r['type']} {r['strike']} Vol/OI {r['vol_oi_ratio']:.2f}배 / 프리미엄 ${r['premium_est']:,.0f}",
            'strike': float(r['strike']),
            'premium': float(r['premium_est']),
            'type': r['type']
        })

    # 3. OI 집중도 - 한 행사가에 몰린 벽
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
    """예측 점수 -100(강한 하락) ~ +100(강한 상승)"""
    score = 0
    reasons = []
    cw = summary_row['call_wall']
    pw = summary_row['put_wall']
    mp = summary_row['max_pain']
    pcr = summary_row['put_call_ratio']

    # 1. 맥스페인 끌어당김 (만기 가까울수록 가중치 UP)
    dte = summary_row['days_to_expiry']
    weight = 1.5 if dte <= 7 else 1.0 if dte <= 30 else 0.5
    if mp > current_price:
        score += 25 * weight
        reasons.append(f"맥스페인({mp})이 현재가보다 위 -> 위로 끌어당김")
    elif mp < current_price:
        score -= 25 * weight
        reasons.append(f"맥스페인({mp})이 현재가보다 아래 -> 아래로 끌어당김")

    # 2. 벽까지 거리
    dist_call = (cw - current_price) / current_price * 100 if current_price else 0
    dist_put = (current_price - pw) / current_price * 100 if current_price else 0
    if dist_call < 2:
        score -= 20
        reasons.append(f"콜월({cw})까지 {dist_call:.1f}%밖에 안남음 -> 저항 강함")
    if dist_put < 2:
        score += 20
        reasons.append(f"풋월({pw})까지 {dist_put:.1f}%밖에 안남음 -> 지지 강함")

    # 3. Put/Call 비율
    if pcr > 1.2:
        score -= 15
        reasons.append(f"Put/Call {pcr}로 풋 쏠림 -> 공포 / 하락 베팅 많음 (역발상으론 바닥 신호일 수도)")
    elif pcr < 0.7:
        score += 15
        reasons.append(f"Put/Call {pcr}로 콜 쏠림 -> 강세 베팅 많음")

    # 4. 위치
    if current_price > cw:
        score += 10
        reasons.append("콜월 돌파 중 -> 상승 모멘텀")
    if current_price < pw:
        score -= 10
        reasons.append("풋월 이탈 중 -> 하락 모멘텀")

    score = max(-100, min(100, score))
    if score >= 40:
        verdict = "🟢 강세 우위"
    elif score >= 10:
        verdict = "🟢 약한 상승 우위"
    elif score <= -40:
        verdict = "🔴 강세 하락 우위" if False else "🔴 약세 우위"
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
        df, current_price = get_all_options(ticker_symbol)
        if df is None or df.empty:
            continue

        cols = ['ticker','expiration','days_to_expiry','type','strike','lastPrice','bid','ask',
                'volume','openInterest','impliedVolatility','mid_price','premium_est','vol_oi_ratio','current_price']
        cols = [c for c in cols if c in df.columns]
        df[cols].to_csv(f'data/{today_str}_{ticker_symbol}.csv', index=False)

        # 만기 1년 이내 전부 요약 (전부 다!)
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

            # 이상거래 / 고래 탐지 (D-day 45일 이내 만기만 집중 - 너무 먼 만기는 노이즈)
            if dte <= 45:
                alerts = detect_whale_and_unusual(df_exp, current_price)
                for a in alerts:
                    alert_rows.append({'date': today_str, 'ticker': ticker_symbol, 'expiration': exp, 'days_to_expiry': dte, **a})

            # 예측은 가장 가까운 만기 3개만
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

    # summary.md 만들기 - 깃허브에서 바로 보기
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
