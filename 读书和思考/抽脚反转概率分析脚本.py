#!/usr/bin/env python3
"""
柱状线抽脚反转概率分析 v2

修复底背离检测逻辑，增加"深度抽脚"分类对比
"""
import tushare as ts
import pandas as pd
import numpy as np
from datetime import datetime
import time
import json

TOKEN = '147094326c2f019599ba7d9e17d72db086f53128369412f059f0e88d'
pro = ts.pro_api(TOKEN)

# ============================================================
# 1. 复用已下载的数据（从前一天缓存）
# ============================================================
print("=" * 70)
print("柱状线抽脚反转概率分析 v2")
print("=" * 70)

# 用更高效的方式直接拉全量
print("\n[1/3] 获取数据...")
all_etfs = pro.fund_basic(market='E', status='L')
yfd = all_etfs[(all_etfs['management'].str.contains('易方达', na=False)) &
               (all_etfs['fund_type'] == '股票型')]
yfd_codes = set(yfd['ts_code'].tolist())

trade_cal = pro.trade_cal(start_date='20210101', end_date='20260527')
trade_dates = trade_cal[trade_cal['is_open'] == 1]['cal_date'].tolist()

print(f"  易方达股票型ETF: {len(yfd_codes)}只")
print(f"  交易日: {trade_dates[0]} ~ {trade_dates[-1]} ({len(trade_dates)}天)")

# 2021~2024每月取2天减少数据量（否则89万条太慢），2025~2026全部取
# 用更高效的方式：直接拉全量，但只取每月的第1和15个交易日
def downsample_dates(dates, year_cutoff=2025):
    """年份 < year_cutoff 的只取每月2天"""
    result = []
    for d in dates:
        year = int(d[:4])
        if year >= year_cutoff:
            result.append(d)
        else:
            month = d[:6]
            if not hasattr(downsample_dates, 'last_month') or downsample_dates.last_month != month:
                downsample_dates.last_month = month
                result.append(d)  # 每月第一个
            elif len(result) > 0 and result[-1][:6] == month:
                # 每月第15个交易日附近
                day = int(d[6:])
                if 10 <= day <= 20:
                    continue  # skip middle
                result.append(d)
            else:
                # 更均匀采样
                pass
    return result

# 简化版：2021-2023每月5个交易日，2024-2026全部
sample_dates = []
for d in trade_dates:
    year = int(d[:4])
    if year >= 2024:
        sample_dates.append(d)
    else:
        month_key = d[:6]
        if month_key not in [x[:6] for x in sample_dates if x[:4] == str(year)]:
            sample_dates.append(d)
        elif sample_dates.count(d[:6]) < 5:
            sample_dates.append(d)

print(f"  采样后交易日: {len(sample_dates)}天")

# 拉取数据
all_daily = []
for i, d in enumerate(sample_dates):
    try:
        df_day = pro.fund_daily(trade_date=d)
        df_yfd = df_day[df_day['ts_code'].isin(yfd_codes)].copy()
        if len(df_yfd) > 0:
            all_daily.append(df_yfd)
        time.sleep(0.1)
    except:
        pass
    if (i+1) % 30 == 0:
        print(f"  进度: {i+1}/{len(sample_dates)}", end='\r')

df_all = pd.concat(all_daily, ignore_index=True)
print(f"\n  总数据: {len(df_all)}条")

# ============================================================
# 2. 按ETF分组计算MACD
# ============================================================
print("\n[2/3] 分析抽脚信号...")

etf_names = {row['ts_code']: row['name'] for _, row in yfd.iterrows()}

def calc_macd(df):
    df = df.copy().sort_values('trade_date')
    df['ema_fast'] = df['close'].ewm(span=12, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=26, adjust=False).mean()
    df['dif'] = df['ema_fast'] - df['ema_slow']
    df['dea'] = df['dif'].ewm(span=9, adjust=False).mean()
    df['macd_bar'] = 2 * (df['dif'] - df['dea'])
    return df

all_signals = []
total_etfs = 0

for code, grp in df_all.groupby('ts_code'):
    if len(grp) < 80:
        continue
    total_etfs += 1
    df_m = calc_macd(grp)
    
    bar_abs_max = df_m['macd_bar'].abs().quantile(0.90)
    threshold_near_zero = bar_abs_max * 0.12
    
    # 预计算：找所有的macd_bar谷底（局部最低点）
    n = len(df_m)
    is_valley = pd.Series(False, index=df_m.index)
    for i in range(3, n-3):
        if (df_m.iloc[i]['macd_bar'] < df_m.iloc[i-1]['macd_bar'] and 
            df_m.iloc[i]['macd_bar'] < df_m.iloc[i-2]['macd_bar'] and
            df_m.iloc[i]['macd_bar'] < df_m.iloc[i+1]['macd_bar'] and
            df_m.iloc[i]['macd_bar'] < df_m.iloc[i+2]['macd_bar']):
            is_valley.iloc[i] = True
    
    valley_indices = df_m.index[is_valley].tolist()
    
    for i in range(42, n):
        curr = df_m.iloc[i]
        prev = df_m.iloc[i-1]
        
        # 抽脚条件
        if curr['macd_bar'] >= 0 or curr['macd_bar'] >= prev['macd_bar']:
            continue
        
        # ---- 这是抽脚信号 ----
        # 判断信号强度
        sig_type = '普通抽脚'
        
        # 判断1：是否在0轴附近
        near_zero = abs(curr['macd_bar']) < threshold_near_zero
        
        # 判断2：是否底背离
        # 当前是否在谷底附近（3天内）
        recent_valleys = [v for v in valley_indices if i - 3 <= v <= i]
        has_divergence = False
        
        if recent_valleys:
            curr_valley_idx = recent_valleys[-1]
            curr_valley_bar = df_m.loc[curr_valley_idx, 'macd_bar']
            curr_valley_price = df_m.loc[curr_valley_idx, 'close']
            
            # 找前一个谷底
            earlier_valleys = [v for v in valley_indices if v < curr_valley_idx - 5]
            if len(earlier_valleys) >= 1:
                prev_valley_idx = earlier_valleys[-1]
                prev_valley_bar = df_m.loc[prev_valley_idx, 'macd_bar']
                prev_valley_price = df_m.loc[prev_valley_idx, 'close']
                
                # 底背离：价格更低，但MACD柱谷底更高
                if curr_valley_price < prev_valley_price * 0.998 and curr_valley_bar > prev_valley_bar:
                    has_divergence = True
        
        if has_divergence:
            sig_type = '底背离+抽脚'
        elif near_zero:
            sig_type = '0轴抽脚'
        
        # 深度抽脚（额外分类）：macd_bar深度低于-50%分位
        bar_50pct = df_m['macd_bar'].quantile(0.50)
        is_deep = curr['macd_bar'] < bar_50pct
        
        sig_extra = '深度抽脚' if is_deep else ('浅度抽脚' if not near_zero else '0轴抽脚')
        
        def fwd(days):
            if i + days < n:
                return (df_m.iloc[i+days]['close'] / curr['close'] - 1) * 100
            return np.nan
        
        all_signals.append({
            'code': code,
            'name': etf_names.get(code, code),
            'date': curr['trade_date'],
            'close': curr['close'],
            'macd_bar': round(curr['macd_bar'], 5),
            'prev_bar': round(prev['macd_bar'], 5),
            'signal_type': sig_type,
            'is_deep': is_deep,
            'ret_3d': fwd(3),
            'ret_5d': fwd(5),
            'ret_10d': fwd(10),
            'ret_20d': fwd(20),
        })

print(f"  分析ETF: {total_etfs}只")
print(f"  总信号: {len(all_signals)}个")

df_sig = pd.DataFrame(all_signals)

# ============================================================
# 3. 统计分析输出
# ============================================================
print("\n[3/3] 统计分析...")

def print_stats(sub, label):
    if len(sub) < 5:
        print(f"\n  {label}: 样本不足")
        return
    
    print(f"\n{'─' * 55}")
    print(f"【{label}】共 {len(sub)} 个信号")
    print(f"{'─' * 55}")
    
    for period, pname in [(3, '3日'), (5, '5日'), (10, '10日'), (20, '20日')]:
        rets = sub[f'ret_{period}d'].dropna()
        if len(rets) < 5:
            continue
        wr = (rets > 0).mean() * 100
        avg = rets.mean()
        med = rets.median()
        print(f"  {pname}: 胜率{wr:5.1f}% | 平均{avg:+6.2f}% | 中位数{med:+6.2f}%")

# 三大分类
print("\n" + "=" * 70)
print("📊 三种抽脚信号反转概率对比")
print("=" * 70)

for st in ['普通抽脚', '0轴抽脚', '底背离+抽脚']:
    sub = df_sig[df_sig['signal_type'] == st]
    print_stats(sub, st)

# 深度 vs 浅度
print("\n" + "=" * 70)
print("📊 深度抽脚 vs 浅度抽脚 对比")
print("=" * 70)

deep = df_sig[(df_sig['is_deep'] == True) & (df_sig['signal_type'] != '0轴抽脚')]
shallow = df_sig[(df_sig['is_deep'] == False) & (df_sig['signal_type'] != '0轴抽脚') & (df_sig['signal_type'] != '底背离+抽脚')]
print_stats(deep, '深度抽脚（绿柱较深，回调充分）')
print_stats(shallow, '浅度抽脚（绿柱较浅，可能未跌透）')

# ============================================================
# 汇总表
# ============================================================
print("\n" + "=" * 70)
print("📋 汇总对比表")
print("=" * 70)

rows = []
for st in ['普通抽脚', '0轴抽脚', '底背离+抽脚']:
    sub = df_sig[df_sig['signal_type'] == st]
    if len(sub) < 5: continue
    row = {'分类': st, '样本': len(sub)}
    for p in [3, 5, 10, 20]:
        rets = sub[f'ret_{p}d'].dropna()
        if len(rets) >= 5:
            row[f'{p}日胜率'] = f"{(rets>0).mean()*100:.1f}%"
            row[f'{p}日平均'] = f"{rets.mean():+.2f}%"
        else:
            row[f'{p}日胜率'] = '-'
            row[f'{p}日平均'] = '-'
    rows.append(row)

# 加深度/浅度
for label, cond in [('深度抽脚', df_sig['is_deep']==True), ('浅度抽脚', df_sig['is_deep']==False)]:
    sub = df_sig[cond]
    if len(sub) < 5: continue
    row = {'分类': label, '样本': len(sub)}
    for p in [3, 5, 10, 20]:
        rets = sub[f'ret_{p}d'].dropna()
        if len(rets) >= 5:
            row[f'{p}日胜率'] = f"{(rets>0).mean()*100:.1f}%"
            row[f'{p}日平均'] = f"{rets.mean():+.2f}%"
    rows.append(row)

pd.set_option('display.max_columns', 20)
pd.set_option('display.width', 120)
print(pd.DataFrame(rows).to_string(index=False))

# ============================================================
# 结论
# ============================================================
print("\n" + "=" * 70)
print("📝 核心发现")
print("=" * 70)

for label, col in [('3日', 'ret_3d'), ('5日', 'ret_5d'), ('10日', 'ret_10d'), ('20日', 'ret_20d')]:
    print(f"\n  【{label}表现排序】")
    stats = []
    for st in [s for s in ['底背离+抽脚', '深度抽脚', '普通抽脚', '浅度抽脚', '0轴抽脚'] if s in df_sig['signal_type'].values or st == '深度抽脚' or st == '浅度抽脚']:
        if st in ['深度抽脚', '浅度抽脚']:
            if st == '深度抽脚':
                sub = df_sig[(df_sig['is_deep']==True) & (df_sig['signal_type']!='0轴抽脚')]
            else:
                sub = df_sig[(df_sig['is_deep']==False) & (df_sig['signal_type']!='0轴抽脚') & (df_sig['signal_type']!='底背离+抽脚')]
        else:
            sub = df_sig[df_sig['signal_type'] == st]
        rets = sub[col].dropna()
        if len(rets) >= 5:
            stats.append((st, rets.mean(), (rets>0).mean()*100, len(rets)))
    
    stats.sort(key=lambda x: x[1], reverse=True)
    for st, avg, wr, n in stats:
        print(f"    {avg:+6.2f}% | 胜率{wr:5.1f}% | ({n}次) — {st}")

print(f"\n  底背离+抽脚样本: {(df_sig['signal_type']=='底背离+抽脚').sum()}个")
print(f"  深度抽脚样本:    {(df_sig['is_deep']==True).sum()}个")
print(f"  普通抽脚样本:    {(df_sig['signal_type']=='普通抽脚').sum()}个")
print(f"  0轴抽脚样本:     {(df_sig['signal_type']=='0轴抽脚').sum()}个")
