import yfinance as yf
import pandas as pd
import numpy as np
import os
from datetime import datetime, timedelta

# 네가 보고 싶은 종목들 - 여기서만 바꾸면 돼
TICKERS = ["DKNG", "RKLB", "WULF", "SOXL", "META", "BE"]
DAYS_LIMIT = 365  # 만기 1년 이내만

def get_all_options(ticker_symbol):
    """한 종목의 1년 이내 모든 만기 옵션 가져오기"""
    ticker = yf.Ticker(ticker_symbol)
    try:
        current_price = ticker.fast_info.get('lastPrice', 0)
        if not current_price:
            info = ticker.info
            current_price = info.get('currentPrice', 0) or info.get('regularMarketPrice', 0)
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

            # 콜
            calls = chain.calls.copy()
            calls['type'] = 'CALL'
            calls['expiration'] = exp_str
            calls['ticker'] = ticker_symbol
            calls['current_price'] = current_price

            # 풋
            puts = chain.puts.copy()
            puts['type'] = 'PUT'
            puts['expiration'] = exp_str
            puts['ticker'] = ticker_symbol
            puts['current_price'] = current_price

            all_rows.append(pd.concat([calls, puts]))
        except Exception as e:
            print(f"  {exp_str} 실패: {e}")
            continue

    if not all_rows:
        return None, current_price
    return pd.concat(all_rows), current_price

def calc_wall_and_pain(df_exp):
    """한 만기에 대한 콜월, 풋월, 맥스페인 계산"""
    df_exp = df_exp.fillna(0)
    calls = df_exp[df_exp['type'] == 'CALL'].copy()
    puts = df_exp[df_exp['type'] == 'PUT'].copy()

    if calls.empty or puts.empty:
        return None

    # 1. 콜월 / 풋월 = OI가 가장 큰 행사가
    call_wall_row = calls.loc[calls['openInterest'].idxmax()]
    put_wall_row = puts.loc[puts['openInterest'].idxmax()]

    # 2. 맥스페인 계산 (벡터화 - 빠름)
    # 모든 행사가를 만기 가격이라고 가정하고, 전체 옵션 매수자의 손실을 계산
    strikes = np.sort(df_exp['strike'].unique())
    call_strikes = calls['strike'].values
    call_oi = calls['openInterest'].values
    put_strikes = puts['strike'].values
    put_oi = puts['openInterest'].values

    # strikes (가격 후보) x 옵션 행사가 매트릭스
    # 콜 손실 = max(0, 가격 - 행사가) * OI
    call_pain = np.maximum(0, strikes[:, None] - call_strikes[None, :]) @ call_oi
    put_pain = np.maximum(0, put_strikes[None, :] - strikes[:, None]) @ put_oi
    total_pain = call_pain + put_pain

    max_pain_price = strikes[np.argmin(total_pain)]

    # 3. OI 상위 3개 행사가도 같이 저장 (월이 구간으로 보일 때 유용)
    top3_calls = calls.nlargest(3, 'openInterest')[['strike', 'openInterest']].values.tolist()
    top3_puts = puts.nlargest(3, 'openInterest')[['strike', 'openInterest']].values.tolist()

    return {
        'call_wall': float(call_wall_row['strike']),
        'call_wall_OI': int(call_wall_row['openInterest']),
        'call_wall_volume': int(call_wall_row.get('volume', 0)),
        'put_wall': float(put_wall_row['strike']),
        'put_wall_OI': int(put_wall_row['openInterest']),
        'put_wall_volume': int(put_wall_row.get('volume', 0)),
        'max_pain': float(max_pain_price),
        'top3_calls': str(top3_calls),
        'top3_puts': str(top3_puts),
    }

def main():
    os.makedirs('data', exist_ok=True)
    today_str = datetime.now().strftime('%Y-%m-%d')
    summary_rows = []

    for ticker_symbol in TICKERS:
        df, current_price = get_all_options(ticker_symbol)
        if df is None or df.empty:
            print(f"{ticker_symbol} 데이터 없음")
            continue

        # 전체 옵션표 저장
        cols = ['ticker', 'expiration', 'type', 'strike', 'lastPrice', 'bid', 'ask',
                'volume', 'openInterest', 'impliedVolatility', 'current_price']
        # 없는 컬럼이 있어도 에러 안 나게
        cols = [c for c in cols if c in df.columns]
        df[cols].to_csv(f'data/{today_str}_{ticker_symbol}.csv', index=False)

        # 만기별 요약 계산 - 가장 가까운 만기 5개만 (너무 많으면 보기 힘듦)
        expirations = sorted(df['expiration'].unique())[:5]
        for exp in expirations:
            result = calc_wall_and_pain(df[df['expiration'] == exp])
            if result:
                summary_rows.append({
                    'date': today_str,
                    'ticker': ticker_symbol,
                    'current_price': round(float(current_price), 2) if current_price else 0,
                    'expiration': exp,
                    'days_to_expiry': (datetime.strptime(exp, "%Y-%m-%d") - datetime.now()).days,
                    **result
                })
        print(f"{ticker_symbol} 완료")

    # 요약 파일 저장
    if summary_rows:
        summary_df = pd.DataFrame(summary_rows)
        summary_df.to_csv(f'data/{today_str}_summary.csv', index=False)

        # 깃허브에서 바로 볼 수 있는 summary.md 만들기
        with open('summary.md', 'w', encoding='utf-8') as f:
            f.write(f"# Option Wall Summary - {today_str}\n\n")
            f.write("콜월 = 콜 OI 최대 행사가 (저항), 풋월 = 풋 OI 최대 행사가 (지지), 맥스페인 = 만기시 옵션 매수자 손실 최소 가격\n\n")
            for ticker_symbol in TICKERS:
                sub = summary_df[summary_df['ticker'] == ticker_symbol]
                if sub.empty:
                    continue
                f.write(f"## {ticker_symbol} - 현재가 {sub.iloc[0]['current_price']}\n\n")
                f.write("| 만기 | D-day | 현재가 | 콜월 | 풋월 | 맥스페인 | 콜월 OI | 풋월 OI |\n")
                f.write("|---|---|---|---|---|---|---|---|\n")
                for _, r in sub.iterrows():
                    f.write(f"| {r['expiration']} | {r['days_to_expiry']} | {r['current_price']} | {r['call_wall']} | {r['put_wall']} | {r['max_pain']} | {r['call_wall_OI']} | {r['put_wall_OI']} |\n")
                f.write("\n")
        print("summary.md 생성 완료")

if __name__ == "__main__":
    main()
