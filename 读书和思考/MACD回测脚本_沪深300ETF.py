#!/usr/bin/env python3
"""MACD策略回测 v2 - 沪深300ETF(510300.SH) 修正版"""
import tushare as ts
import pandas as pd
import numpy as np

TOKEN = '147094326c2f019599ba7d9e17d72db086f53128369412f059f0e88d'
pro = ts.pro_api(TOKEN)

print("拉取沪深300ETF日线数据...")
df = pro.fund_daily(ts_code='510300.SH', start_date='20200501', end_date='20260527')
df['trade_date'] = pd.to_datetime(df['trade_date'])
df = df.sort_values('trade_date').reset_index(drop=True)

# ── 计算MACD ──
def calc_macd(df, fast=12, slow=26, signal=9):
    df = df.copy()
    df['ema_fast'] = df['close'].ewm(span=fast, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=slow, adjust=False).mean()
    df['dif'] = df['ema_fast'] - df['ema_slow']
    df['dea'] = df['dif'].ewm(span=signal, adjust=False).mean()
    df['macd_bar'] = 2 * (df['dif'] - df['dea'])
    return df

df = calc_macd(df)
print(f"数据: {df['trade_date'].iloc[0].date()} ~ {df['trade_date'].iloc[-1].date()}, {len(df)}天")

# ── 回测引擎 ──
def backtest(df, buy_signals, sell_signals, name, cost=0.0003):
    """返回净值序列和统计"""
    nav = pd.Series(1.0, index=df.index)
    pos = 0
    entry_price = 0
    trades_log = []
    
    for i in range(len(df)):
        if i == 0:
            continue
        price = df.iloc[i]['close']
        date = df.iloc[i]['trade_date']
        
        # 先卖后买
        if pos and sell_signals.iloc[i]:
            r = (price / entry_price - 1) * 100
            trades_log.append({'date': date, 'type': 'sell', 'ret': r})
            nav.iloc[i] = nav.iloc[i-1] * (1 - cost)
            pos = 0
        elif not pos and buy_signals.iloc[i]:
            entry_price = price
            pos = 1
            trades_log.append({'date': date, 'type': 'buy', 'ret': 0})
            nav.iloc[i] = nav.iloc[i-1] * (1 - cost)
        else:
            if pos:
                nav.iloc[i] = nav.iloc[i-1] * (price / df.iloc[i-1]['close'])
            else:
                nav.iloc[i] = nav.iloc[i-1]
    
    # 持仓到结束
    if pos:
        r = (df.iloc[-1]['close'] / entry_price - 1) * 100
        trades_log.append({'date': df.iloc[-1]['trade_date'], 'type': 'sell(末)', 'ret': r})
    
    # 统计
    total_ret = (nav.iloc[-1] - 1) * 100
    years = (df['trade_date'].iloc[-1] - df['trade_date'].iloc[0]).days / 365.0
    ann_ret = (nav.iloc[-1] ** (1/years) - 1) * 100 if years > 0 else 0
    
    peak = nav.expanding().max()
    dd = (nav / peak - 1) * 100
    max_dd = dd.min()
    
    buy_trades = [t for t in trades_log if t['type'] == 'buy']
    sell_trades = [t for t in trades_log if t['type'] != 'buy']
    n_trades = min(len(buy_trades), len(sell_trades))
    profits = [t['ret'] for t in sell_trades if t['ret'] != 0]
    
    win_rate = sum(1 for p in profits if p > 0) / len(profits) * 100 if profits else 0
    avg_pnl = np.mean(profits) if profits else 0
    
    # 基准
    bench_ret = (df['close'].iloc[-1] / df.iloc[0]['close'] - 1) * 100
    bench_nav = df['close'] / df.iloc[0]['close']
    bench_peak = np.maximum.accumulate(bench_nav)
    bench_dd = (bench_nav / bench_peak - 1) * 100
    
    return {
        '策略': name,
        '总收益%': round(total_ret, 2),
        '年化%': round(ann_ret, 2),
        '最大回撤%': round(max_dd, 2),
        '交易次数': n_trades,
        '胜率%': round(win_rate, 1),
        '平均单笔%': round(avg_pnl, 2),
        '基准收益%': round(bench_ret, 2),
        '基准回撤%': round(bench_dd.min(), 2),
        'nav': nav,
        'trades': trades_log,
        'dd_series': dd,
    }

# ── 策略定义 ──
df_b = df.iloc[40:].reset_index(drop=True)  # 跳过预热

# 1. DIF趋势法
def s1(df):
    b = (df['dif'].shift(1) <= 0) & (df['dif'] > 0)
    s = (df['dif'].shift(1) >= 0) & (df['dif'] < 0)
    return b, s

# 2. DEA金叉/死叉（注意：macd_bar翻红/翻绿=金叉/死叉，是一样的）
def s2(df):
    b = (df['dif'].shift(1) <= df['dea'].shift(1)) & (df['dif'] > df['dea'])
    s = (df['dif'].shift(1) >= df['dea'].shift(1)) & (df['dif'] < df['dea'])
    return b, s

# 3. 金叉+0轴以下买入过滤（低位金叉更可靠）
def s3(df):
    b = ((df['dif'].shift(1) <= df['dea'].shift(1)) & (df['dif'] > df['dea'])) & (df['dif'] < 0)
    s = (df['dif'].shift(1) >= df['dea'].shift(1)) & (df['dif'] < df['dea'])
    return b, s

# 4. 柱状线抽脚买+缩头卖（不等翻红翻绿）
def s4(df):
    # 抽脚：绿柱（macd_bar<0）且比前一天缩短
    b = (df['macd_bar'] < 0) & (df['macd_bar'] > df['macd_bar'].shift(1))
    # 缩头：红柱（macd_bar>0）且比前一天缩短
    s = (df['macd_bar'] > 0) & (df['macd_bar'] < df['macd_bar'].shift(1))
    return b, s

# 5. 柱状线双重底背离+翻红确认
def s5(df):
    b = pd.Series(False, index=df.index)
    s = (df['dif'].shift(1) >= df['dea'].shift(1)) & (df['dif'] < df['dea'])
    window = 30
    for i in range(window, len(df)):
        # 找最近的两个macd_bar谷底
        vals = df['macd_bar'].iloc[i-window:i+1].values
        closes = df['close'].iloc[i-window:i+1].values
        n = len(vals)
        # 找局部最低点
        valleys = []
        for j in range(1, n-1):
            if vals[j] < vals[j-1] and vals[j] < vals[j+1] and vals[j] < 0:
                valleys.append(j)
        if len(valleys) >= 2:
            v1, v2 = valleys[-2], valleys[-1]
            # 价格新低但macd_bar谷底抬高 = 底背离
            if closes[v2] < closes[v1] and vals[v2] > vals[v1]:
                # macd_bar开始上升
                if df.iloc[i]['macd_bar'] > df.iloc[i-1]['macd_bar'] and df.iloc[i]['macd_bar'] < 0:
                    b.iloc[i] = True
    return b, s

# 6. DIF趋势法 + 均线过滤（只在价格在MA60之上操作）
def s6(df):
    ma60 = df['close'].rolling(60).mean()
    b = (df['dif'].shift(1) <= 0) & (df['dif'] > 0) & (df['close'] > ma60)
    s = (df['dif'].shift(1) >= 0) & (df['dif'] < 0)
    return b, s

# 7. 双均线+DIF过滤（保守版：金叉买+价格在MA20上，死叉卖+价格在MA20下）
def s7(df):
    ma20 = df['close'].rolling(20).mean()
    b = ((df['dif'].shift(1) <= df['dea'].shift(1)) & (df['dif'] > df['dea'])) & (df['close'] > ma20)
    s = ((df['dif'].shift(1) >= df['dea'].shift(1)) & (df['dif'] < df['dea'])) & (df['close'] < ma20)
    return b, s


results = []
all_strategies = [
    ('① DIF上穿0轴买/下穿卖', s1),
    ('② DEA金叉买/死叉卖', s2),
    ('③ 低位金叉买(DIF<0)', s3),
    ('④ 柱状线抽脚买/缩头卖', s4),
    ('⑤ 柱状线双重底背离', s5),
    ('⑥ DIF趋势+MA60过滤', s6),
    ('⑦ 金叉+MA20上买/死叉+MA20下卖', s7),
]

for name, fn in all_strategies:
    buy, sell = fn(df_b)
    r = backtest(df_b, buy, sell, name)
    results.append(r)

bench = (df_b['close'].iloc[-1] / df_b.iloc[0]['close'] - 1) * 100
bench_dd = ((df_b['close'] / df_b.iloc[0]['close']).div(
    np.maximum.accumulate(df_b['close'] / df_b.iloc[0]['close'])) - 1).min() * 100

# 输出
print(f"\n回测: {df_b['trade_date'].iloc[0].date()} ~ {df_b['trade_date'].iloc[-1].date()} (约6年)")
print(f"{'='*90}")
print(f"{'策略':<25} {'总收益':>8} {'年化':>8} {'最大回撤':>10} {'次数':>6} {'胜率':>8} {'平均单笔':>10}")
print(f"{'-'*90}")
for r in results:
    print(f"{r['策略']:<25} {r['总收益%']:>7.2f}% {r['年化%']:>7.2f}% {r['最大回撤%']:>9.2f}% {r['交易次数']:>4}  {r['胜率%']:>6.1f}% {r['平均单笔%']:>8.2f}%")
print(f"{'-'*90}")
print(f"{'买入持有(基准)':<25} {bench:>7.2f}% {'':>8} {bench_dd:>9.2f}%")
print(f"{'='*90}")

# 最佳策略详解
best = max(results, key=lambda r: r['总收益%'])
print(f"\n🔍 最佳策略：{best['策略']}")
print(f"  总收益{best['总收益%']}% | 年化{best['年化%']}% | 回撤{best['最大回撤%']}%")
print(f"  交易{best['交易次数']}笔 | 胜率{best['胜率%']}% | 平均{best['平均单笔%']}%")
