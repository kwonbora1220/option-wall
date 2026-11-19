import pandas as pd
import numpy as np
import requests
import os, re, glob, shutil
from datetime import datetime, timedelta, timezone

# KST 날짜 - 반드시 naive로 반환 (aware면 만기일 비교에서 TypeError -> 수집 0개 -> 리포트 2장)
KST = timezone(timedelta(hours=9))
def kst_now():
    return datetime.now(KST).replace(tzinfo=None)
def report_date_str():
    return kst_now().strftime('%Y-%m-%d')

TICKERS = ["DKNG","RKLB","WULF","SOXL","META","BE"]
DAYS_LIMIT = 365
WHALE_PREMIUM = 1000000
UNUSUAL_VOL_OI = 1.0
WALL_SHARE = 0.15
OI_SURGE_MIN = 500
OI_SURGE_PCT = 0.5
MIN_OI_FOR_UNUSUAL = 100
MIN_TOTAL_OI_FOR_WALL_CONC = 2000
MAX_SPREAD_PCT_FOR_WHALE = 0.30
MIN_PREMIUM_FOR_UNUSUAL = 200000
HEADERS = {"User-Agent":"Mozilla/5.0","Accept":"application/json"}

def safe_float(v,d=0):
    try: return float(v) if v not in (None,"") else d
    except: return d
def safe_int(v,d=0):
    try: return int(float(v)) if v not in (None,"") else d
    except: return d
def parse_occ_symbol(s,ticker):
    try:
        s=str(s).strip().upper()
        if s.startswith(ticker.upper()):
            rest=s[len(ticker):]
        else:
            m=re.search(r"(\d{6})([CP])(\d{8})",s)
            if not m: return None,None,None
            return f"20{m.group(1)[:2]}-{m.group(1)[2:4]}-{m.group(1)[4:6]}","CALL" if m.group(2)=="C" else "PUT",int(m.group(3))/1000.0
        if len(rest)<15: return None,None,None
        return f"20{rest[:2]}-{rest[2:4]}-{rest[4:6]}","CALL" if rest[6]=="C" else "PUT" if rest[6]=="P" else None,int(rest[7:15])/1000.0
    except: return None,None,None

def load_prev_day_data(ticker,today_str):
    cands=[]
    for f in sorted(glob.glob(f"data/*_{ticker}.csv")):
        b=os.path.basename(f)
        if "_summary" in b or "_alerts" in b or "_predict" in b or "_validation" in b: continue
        if b.startswith(today_str): continue
        cands.append(f)
    if not cands: return None
    try:
        df=pd.read_csv(cands[-1]); print(f"  {ticker} 전일 파일: {cands[-1]} ({len(df)}행)"); return df
    except Exception as e: print(f"  전일 읽기 실패: {e}"); return None

def add_oi_change(df,df_prev):
    df=df.copy(); has_prev=df_prev is not None and not df_prev.empty; df['has_prev']=has_prev
    if not has_prev:
        df['prev_OI']=np.nan; df['prev_exists']=False; df['OI_change']=0; df['OI_change_pct']=0.0; return df
    pm={(str(r.get('expiration')),str(r.get('type')),float(r.get('strike',0))):safe_int(r.get('openInterest',0)) for _,r in df_prev.iterrows()}
    po,pe=[],[]
    for _,r in df.iterrows():
        k=(str(r['expiration']),str(r['type']),float(r['strike']))
        if k in pm: po.append(pm[k]); pe.append(True)
        else: po.append(np.nan); pe.append(False)
    df['prev_OI']=po; df['prev_exists']=pe
    df['OI_change']=np.where(df['prev_exists'],df['openInterest']-df['prev_OI'].fillna(0),0)
    df['OI_change_pct']=np.where((df['prev_exists'])&(df['prev_OI']>0),df['OI_change']/df['prev_OI'],0.0)
    return df

def get_price_with_check(ticker):
    stooq_p,nasdaq_p=0,0
    try:
        r=requests.get(f"https://stooq.com/q/l/?s={ticker.lower()}.us&f=sd2t2ohlcv&h&e=csv",headers=HEADERS,timeout=15)
        if r.status_code==200 and "N/D" not in r.text:
            lines=r.text.strip().split("\n")
            if len(lines)>=2: stooq_p=safe_float(lines[1].split(",")[6] if len(lines[1].split(","))>6 else 0)
    except Exception as e: print(f"  stooq 실패: {e}")
    for asset in ["stocks","etf"]:
        try:
            r=requests.get(f"https://api.nasdaq.com/api/quote/{ticker}/info?assetclass={asset}",headers={**HEADERS,"Accept-Language":"en-US"},timeout=15)
            if r.status_code==200:
                j=r.json(); d=(j.get("data") or {}); pd_data=d.get("primaryData") or {}
                price=str(pd_data.get("lastSalePrice","")).replace("$","").replace(",","")
                if price: nasdaq_p=safe_float(price); break
        except Exception as e: print(f"  nasdaq 실패: {e}")
    price=stooq_p if stooq_p>0 else nasdaq_p
    source="stooq(종가)" if stooq_p>0 else ("nasdaq" if nasdaq_p>0 else "없음")
    diff=abs(stooq_p-nasdaq_p)/stooq_p*100 if stooq_p>0 and nasdaq_p>0 else 0.0
    flag="✅" if diff<1.0 else "⚠ 소스간 차이 1%↑"
    print(f"  {ticker} 가격 stooq={stooq_p} nasdaq={nasdaq_p} 사용={price} ({source}) 차이={diff:.2f}% {flag}")
    return price,source,round(float(stooq_p),2),round(float(nasdaq_p),2),round(float(diff),2),flag

def get_all_options(ticker,today_str):
    url=f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker}.json"
    print(f"--- {ticker} 수집 시작 ---")
    # CBOE는 짧은 간격으로 여러 번 치면 403/빈응답을 줄 때가 있다.
    # 그래서 3번 재시도하고, 그래도 안 되면 None이 아니라 실패 원인을 남긴다.
    data = None
    for _attempt in range(3):
        try:
            r=requests.get(url,headers=HEADERS,timeout=30)
            print(f"  CBOE 시도 {_attempt+1}/3: status={r.status_code} len={len(r.text)}")
            if r.status_code==200:
                data=r.json(); break
            print(f"  CBOE 응답 실패: {r.status_code}")
        except Exception as e: print(f"  CBOE 요청 실패: {e}")
        import time as _time; _time.sleep(3)
    if data is None:
        print(f"  CBOE 3회 실패 -> {ticker} 이번 수집 건너뜀")
        return None,0,{'cboe_failed': True}
    options=data.get("data",{}).get("options",[])
    if not options: print(f"  옵션 데이터 없음"); return None,0,{}
    today=kst_now(); limit_date=today+timedelta(days=DAYS_LIMIT)
    rows=[]; price,src,sp,np_,diff,flag=get_price_with_check(ticker)
    info={'price_source':src,'stooq_price':sp,'nasdaq_price':np_,'price_diff_pct':diff,'price_flag':flag}
    print(f"  {ticker} 현재가 {price} ({src}), 전체 옵션 {len(options)}")
    for o in options:
        try:
            exp_str,typ,strike=parse_occ_symbol(o.get("option",""),ticker)
            if not exp_str or not typ or strike is None: continue
            exp_date=datetime.strptime(exp_str,"%Y-%m-%d")  # naive, today도 naive라 비교 가능
            if exp_date>limit_date: continue
            if exp_date<today-timedelta(days=1): continue
            if strike<=0: continue
            bid=safe_float(o.get("bid",0)); ask=safe_float(o.get("ask",0))
            vol=safe_int(o.get("volume",0)); oi=safe_int(o.get("open_interest",0))
            mid=(bid+ask)/2; spread=(ask-bid)/mid if mid>0 else 1.0
            rows.append({'ticker':ticker,'expiration':exp_str,'days_to_expiry':(exp_date-today).days,'type':typ,'strike':strike,'lastPrice':safe_float(o.get("last_trade_price",0)),'bid':bid,'ask':ask,'volume':vol,'openInterest':oi,'impliedVolatility':safe_float(o.get("iv",0)),'mid_price':mid,'spread_pct':spread,'premium_est':vol*mid*100,'vol_oi_ratio':vol/oi if oi>0 else 0,'current_price':price})
        except Exception as e:
            print(f"  옵션 파싱 실패 (무시): {e}"); continue
    print(f"  {ticker} 파싱 결과: 성공 {len(rows)}개")
    if not rows: return None,price,info
    df=pd.DataFrame(rows); df=add_oi_change(df,load_prev_day_data(ticker,today_str)); return df,price,info

def calc_wall_and_pain(df_exp,price=0):
    df_exp=df_exp.fillna(0)
    if price and price>0:
        df_near=df_exp[(df_exp['strike']>=price*0.8)&(df_exp['strike']<=price*1.2)].copy()
        if len(df_near)<6: df_near=df_exp.copy()
    else: df_near=df_exp.copy()
    calls=df_near[df_near['type']=='CALL'].copy(); puts=df_near[df_near['type']=='PUT'].copy()
    if calls.empty or puts.empty:
        calls=df_exp[df_exp['type']=='CALL'].copy(); puts=df_exp[df_exp['type']=='PUT'].copy()
        if calls.empty or puts.empty: return None
        df_near=df_exp.copy()
    ca=calls[calls['strike']>=price] if price else calls; pb=puts[puts['strike']<=price] if price else puts
    cw=(ca if not ca.empty else calls).loc[(ca if not ca.empty else calls)['openInterest'].idxmax()]
    pw=(pb if not pb.empty else puts).loc[(pb if not pb.empty else puts)['openInterest'].idxmax()]
    strikes=np.sort(df_near['strike'].unique())
    cp=np.maximum(0,strikes[:,None]-calls['strike'].values[None,:])@calls['openInterest'].values
    pp=np.maximum(0,puts['strike'].values[None,:]-strikes[:,None])@puts['openInterest'].values
    mp=strikes[np.argmin(cp+pp)]
    tc=int(df_exp[df_exp['type']=='CALL']['openInterest'].sum()); tp=int(df_exp[df_exp['type']=='PUT']['openInterest'].sum())
    return {'call_wall':float(cw['strike']),'call_wall_OI':int(cw['openInterest']),'call_wall_OI_change':int(cw.get('OI_change',0)),'put_wall':float(pw['strike']),'put_wall_OI':int(pw['openInterest']),'put_wall_OI_change':int(pw.get('OI_change',0)),'max_pain':float(mp),'total_call_OI':tc,'total_put_OI':tp,'put_call_ratio':round(float(tp/tc if tc>0 else 0),2),'total_premium':float(df_exp['premium_est'].sum()),'OI_net_change':int(df_exp['OI_change'].sum()) if 'OI_change' in df_exp.columns else 0,'has_prev':bool(df_exp['has_prev'].iloc[0]) if 'has_prev' in df_exp.columns else False}

def detect_whale(df_exp):
    alerts=[]; df_exp=df_exp.fillna(0); has_prev=bool(df_exp['has_prev'].iloc[0]) if 'has_prev' in df_exp.columns else False
    if has_prev and 'OI_change' in df_exp.columns:
        base=df_exp[df_exp.get('prev_exists',True)==True] if 'prev_exists' in df_exp.columns else df_exp
        for _,r in base[(base['OI_change']>=OI_SURGE_MIN)&(base['OI_change_pct']>=OI_SURGE_PCT)].sort_values('OI_change',ascending=False).head(8).iterrows():
            alerts.append({'level':'OI_SURGE','msg':f"{r['type']} {r['strike']} OI {int(r['prev_OI'])} -> {int(r['openInterest'])} (+{int(r['OI_change'])}, +{r['OI_change_pct']*100:.0f}%) / 거래량 {int(r['volume'])}",'strike':float(r['strike']),'premium':float(r['premium_est']),'type':r['type']})
        for _,r in base[base['OI_change']<=-OI_SURGE_MIN].sort_values('OI_change').head(5).iterrows():
            alerts.append({'level':'OI_DROP','msg':f"{r['type']} {r['strike']} OI {int(r['prev_OI'])} -> {int(r['openInterest'])} ({int(r['OI_change'])}) [이탈/청산 의심]",'strike':float(r['strike']),'premium':float(r['premium_est']),'type':r['type']})
    seen=set()
    for _,r in df_exp[(df_exp['premium_est']>=WHALE_PREMIUM)&(df_exp['spread_pct']<=MAX_SPREAD_PCT_FOR_WHALE)].sort_values('premium_est',ascending=False).head(5).iterrows():
        pt=f" (전일 OI {int(r['prev_OI'])})" if has_prev and pd.notna(r.get('prev_OI',np.nan)) else " (전일 OI - 기준 수집 중)"
        alerts.append({'level':'WHALE','msg':f"{r['type']} {r['strike']} 프리미엄 ${r['premium_est']:,.0f} / 거래량 {int(r['volume'])} / OI {int(r['openInterest'])}{pt}",'strike':float(r['strike']),'premium':float(r['premium_est']),'type':r['type']}); seen.add((r['type'],float(r['strike'])))
    for _,r in df_exp[(df_exp['vol_oi_ratio']>=UNUSUAL_VOL_OI)&(df_exp['openInterest']>=MIN_OI_FOR_UNUSUAL)].sort_values('vol_oi_ratio',ascending=False).head(5).iterrows():
        if r['premium_est']<MIN_PREMIUM_FOR_UNUSUAL: continue
        if (r['type'],float(r['strike'])) in seen: continue
        alerts.append({'level':'UNUSUAL','msg':f"{r['type']} {r['strike']} Vol/OI {r['vol_oi_ratio']:.2f}배 / 프리미엄 ${r['premium_est']:,.0f}",'strike':float(r['strike']),'premium':float(r['premium_est']),'type':r['type']})
    total=df_exp['openInterest'].sum()
    if total>=MIN_TOTAL_OI_FOR_WALL_CONC:
        g=df_exp.groupby(['type','strike'])['openInterest'].sum().reset_index()
        for typ,gg in g.groupby('type'):
            gg=gg.copy(); gg['share']=gg['openInterest']/total; top=gg.sort_values('share',ascending=False).iloc[0]
            if top['share']>=WALL_SHARE: alerts.append({'level':'WALL_CONCENTRATION','msg':f"{typ} {top['strike']}에 전체 OI의 {top['share']*100:.1f}% 집중",'strike':float(top['strike']),'premium':0,'type':typ})
    return alerts

def predict(row,price):
    score=0; reasons=[]; cw=row['call_wall']; pw=row['put_wall']; mp=row['max_pain']; pcr=row['put_call_ratio']; dte=row['days_to_expiry']; has_prev=row.get('has_prev',True); w=1.5 if dte<=7 else 1.0 if dte<=30 else 0.5
    if price and price>0:
        d=abs(mp-price)/price*100
        if d<0.5: reasons.append(f"맥스페인({mp}) 현재가 근처(±0.5% 이내) -> 중립")
        elif mp>price: score+=25*w; reasons.append(f"맥스페인({mp}) 위 -> 위로 끌어당김")
        else: score-=25*w; reasons.append(f"맥스페인({mp}) 아래 -> 아래로 끌어당김")
        dc=(cw-price)/price*100; dp=(price-pw)/price*100
        if 0<=dc<2: score-=20; reasons.append(f"콜월({cw})까지 {dc:.1f}% -> 저항")
        elif dc<0: score+=10; reasons.append(f"콜월({cw}) 돌파 중")
        if 0<=dp<2: score+=20; reasons.append(f"풋월({pw})까지 {dp:.1f}% -> 지지")
        elif dp<0: score-=10; reasons.append(f"풋월({pw}) 이탈 중")
    if pcr>1.2: score-=15; reasons.append(f"Put/Call {pcr} 풋 쏠림")
    elif pcr<0.7: score+=15; reasons.append(f"Put/Call {pcr} 콜 쏠림")
    if has_prev:
        oi=row.get('OI_net_change',0)
        if oi>5000: reasons.append(f"OI 어제보다 {oi} 증가")
        elif oi<-5000: reasons.append(f"OI 어제보다 {abs(oi)} 감소")
    else: reasons.append("OI 전일 대비: 기준 데이터 수집 중 (내일부터 비교 가능)")
    score=max(-100,min(100,score))
    verdict="🟢 강세 우위" if score>=40 else "🟢 약한 상승 우위" if score>=10 else "🔴 약세 우위" if score<=-40 else "🔴 약한 하락 우위" if score<=-10 else "⚪ 중립 / 박스권"
    return score,verdict,reasons

def write_parts(text):
    d="data/telegram"
    if os.path.exists(d): shutil.rmtree(d)
    os.makedirs(d,exist_ok=True); secs=[]; cur=""
    for line in text.splitlines(True):
        if line.startswith("## ") and cur.strip(): secs.append(cur); cur=line
        else: cur+=line
    if cur.strip(): secs.append(cur)
    for i,s in enumerate(secs,1):
        open(f"{d}/part_{i:02d}.txt","w",encoding="utf-8").write(s)
    print(f"telegram parts: {len(secs)}개")

def main():
    os.makedirs('data',exist_ok=True); today_str=report_date_str(); collected=kst_now().strftime('%Y-%m-%d %H:%M KST')
    print(f"리포트 날짜(KST): {today_str} / 수집: {collected}")
    srows=[]; arows=[]; prows=[]; vrows=[]
    for t in TICKERS:
        try: df,price,info=get_all_options(t,today_str)
        except Exception as e: print(f"!!! {t} 실패: {e}"); continue
        if df is None or df.empty:
            print(f"{t} 데이터 없음 - 건너뜀 (CBOE 실패 가능)")
            vrows.append({'date':today_str,'collected_at':collected,'ticker':t,'used_price':0,'price_source':'CBOE 실패','stooq_price':0,'nasdaq_price':0,'price_diff_pct':-1,'price_flag':'❌ CBOE 3회 실패 / 잠시 뒤 재실행'}) if 'vrows' in dir() else None
            continue
        vrows.append({'date':today_str,'collected_at':collected,'ticker':t,'used_price':round(float(price),2) if price else 0,**info})
        cols=['ticker','expiration','days_to_expiry','type','strike','lastPrice','bid','ask','volume','openInterest','prev_OI','OI_change','OI_change_pct','impliedVolatility','mid_price','premium_est','vol_oi_ratio','current_price','has_prev','prev_exists']
        cols=[c for c in cols if c in df.columns]; df[cols].to_csv(f'data/{today_str}_{t}.csv',index=False)
        for exp in sorted(df['expiration'].unique()):
            de=df[df['expiration']==exp]; res=calc_wall_and_pain(de,price)
            if not res: continue
            dte=int(de.iloc[0]['days_to_expiry']); row={'date':today_str,'ticker':t,'current_price':round(float(price),2) if price else 0,'expiration':exp,'days_to_expiry':dte,**res}; srows.append(row)
            if dte<=45:
                for a in detect_whale(de): arows.append({'date':today_str,'ticker':t,'expiration':exp,'days_to_expiry':dte,**a})
            if exp in sorted(df['expiration'].unique())[:3]:
                sc,ve,rs=predict(row,price); prows.append({'date':today_str,'ticker':t,'expiration':exp,'days_to_expiry':dte,'current_price':row['current_price'],'call_wall':row['call_wall'],'put_wall':row['put_wall'],'max_pain':row['max_pain'],'score':sc,'verdict':ve,'reasons':" | ".join(rs)})
        print(f"{t} 완료")
    sdf=pd.DataFrame(srows)
    if not sdf.empty: sdf.to_csv(f'data/{today_str}_summary.csv',index=False)
    adf=pd.DataFrame(arows)
    if not adf.empty: adf.to_csv(f'data/{today_str}_alerts.csv',index=False)
    else: pd.DataFrame(columns=['ticker','expiration','level','msg']).to_csv(f'data/{today_str}_alerts.csv',index=False)
    pdf=pd.DataFrame(prows)
    if not pdf.empty: pdf.to_csv(f'data/{today_str}_predict.csv',index=False)
    vdf=pd.DataFrame(vrows)
    if not vdf.empty: vdf.to_csv(f'data/{today_str}_validation.csv',index=False)
    lines=[f"# Option Wall + Whale + OI변화 + 예측 - {today_str}\n\n",f"수집 시각: {collected} / 기준 날짜(KST): {today_str}\n\n","> ⚠ 옵션 포지셔닝 기반 참고용입니다. 확정적인 방향이 아닙니다.\n\n"]
    if not vdf.empty:
        lines.append("## ✅ 가격 검증 (수집가 vs 외부 소스)\n\n"); lines.append("| 종목 | 사용한 가격 | stooq(종가) | nasdaq | 차이 | 판정 |\n|---|---|---|---|---|---|\n")
        for _,r in vdf.iterrows(): lines.append(f"| {r['ticker']} | {r['used_price']} | {r['stooq_price']} | {r['nasdaq_price']} | {r['price_diff_pct']}% | {r['price_flag']} |\n")
        lines.append("\n> 가격은 수집 시점 스냅샷입니다. 두 소스가 1% 이상 벌어지면 ⚠가 뜹니다.\n\n")
    if not pdf.empty:
        lines.append("## 🎯 방향 예측 (가까운 만기 3개 기준)\n\n"); lines.append("| 종목 | 만기 | D-day | 현재가 | 콜월 | 풋월 | 맥스페인 | 점수 | 판단 |\n|---|---|---|---|---|---|---|---|---|\n")
        for _,r in pdf.iterrows(): lines.append(f"| {r['ticker']} | {r['expiration']} | {r['days_to_expiry']} | {r['current_price']} | {r['call_wall']} | {r['put_wall']} | {r['max_pain']} | {r['score']} | {r['verdict']} |\n")
        lines.append("\n### 판단 근거\n\n")
        for _,r in pdf.iterrows(): lines.append(f"- **{r['ticker']} {r['expiration']} ({r['verdict']})**: {r['reasons']}\n")
        lines.append("\n")
    if not adf.empty:
        for lvl,title in [('OI_SURGE',"## 🔥 OI 급증 - 고래 신규 진입 (전일 대비)\n\n"),('OI_DROP',"## 📉 OI 급감 - 고래 이탈 / 청산\n\n")]:
            sub_all=adf[adf['level']==lvl]
            if not sub_all.empty:
                lines.append(title)
                for t in TICKERS:
                    sub=sub_all[sub_all['ticker']==t]
                    if sub.empty: continue
                    lines.append(f"### {t}\n\n")
                    for _,r in sub.iterrows(): lines.append(f"- {'🔥' if lvl=='OI_SURGE' else '📉'} [{r['expiration']}] {r['msg']}\n")
                    lines.append("\n")
    if not adf.empty:
        lines.append("## 🐋 고래 / 이상거래 알림 (D-day 45일 이내)\n\n")
        for t in TICKERS:
            sub=adf[adf['ticker']==t]
            if sub.empty: continue
            lines.append(f"### {t}\n\n")
            for _,r in sub.iterrows():
                em={"OI_SURGE":"🔥","OI_DROP":"📉","WHALE":"🐋","UNUSUAL":"⚠"}.get(r['level'],"🧱"); lines.append(f"- {em} [{r['expiration']}] {r['msg']}\n")
            lines.append("\n")
    else: lines.append("## 🐋 고래 / 이상거래 알림\n\n- 오늘은 특이한 고래 흔적 없음\n\n")
    if not sdf.empty:
        lines.append("## 📊 전체 만기 요약 (1년 이내 전부)\n\n")
        for t in TICKERS:
            sub=sdf[sdf['ticker']==t]
            if sub.empty: continue
            lines.append(f"### {t} - 현재가 {sub.iloc[0]['current_price']}\n\n"); lines.append("| 만기 | D-day | 콜월 | 콜월 OI변화 | 풋월 | 풋월 OI변화 | 맥스페인 | P/C | OI순증가 |\n|---|---|---|---|---|---|---|---|---|\n")
            for _,r in sub.iterrows():
                oi=0 if not r.get('has_prev',True) else r.get('OI_net_change',0); cw=0 if not r.get('has_prev',True) else r.get('call_wall_OI_change',0); pwc=0 if not r.get('has_prev',True) else r.get('put_wall_OI_change',0)
                lines.append(f"| {r['expiration']} | {r['days_to_expiry']} | {r['call_wall']} | {cw:+} | {r['put_wall']} | {pwc:+} | {r['max_pain']} | {r['put_call_ratio']} | {oi:+} |\n")
            lines.append("\n")
    if not srows:
        lines.append("## ❌ 수집 실패\n\n- 오늘 수집된 옵션 데이터가 0개입니다. Actions 로그에서 `파싱 결과: 성공 0개` / `CBOE 응답 실패`를 확인하세요.\n\n")
    full="".join(lines)
    open('summary.md','w',encoding='utf-8').write(full); write_parts(full); print("완료")
if __name__=="__main__": main()
