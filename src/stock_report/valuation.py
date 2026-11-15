"""Evidence-based valuation: method selection, reproducible inputs, explicit limitations.

Methods come from the sector profile and run only when their inputs are usable. A method that
cannot run is reported with its status and reason; nothing is estimated to fill the gap.
Historical inputs (trailing) and forward inputs (consensus) are kept apart; management guidance
is never turned into a forward multiple here, it may only feed a labelled scenario assumption.
"""
from datetime import timedelta
from .fin_series import latest_instant
from .fin_analysis import debt_at, sector_profile
from .policy import reportable

MAX_PRICE_LAG_TRADING_DAYS = 3
EXCHANGE_TZ_HOURS = {'TWSE': 8, 'TPEx': 8, 'NYSE': -5, 'NASDAQ': -5}
METHOD_LABELS = {'pe_trailing': '本益比（TTM）', 'pe_forward': '預估本益比（共識）', 'ev_ebitda': 'EV/EBITDA',
                 'p_fcf': '股價／自由現金流', 'fcf_yield': '自由現金流殖利率', 'p_b': '股價淨值比',
                 'historical_range': '歷史估值區間', 'peer_relative': '同業相對估值', 'scenario': '情境估值'}


def weekdays_between(earlier, later):
    """Weekdays strictly after `earlier` up to and including `later`; exchange holidays are not modelled."""
    days, day = 0, earlier
    while day < later:
        day += timedelta(days=1)
        days += day.weekday() < 5
    return days


def latest_price(data, entity):
    quotes = [e for e in data.evidence if e.entity_id == entity.entity_id and e.kind == 'price'
              and e.metric_key == 'close' and e.value and e.value > 0 and reportable(e, data.request)]
    if not quotes:
        return None, {'status': 'not_found', 'note': '沒有已查核且可內部使用的收盤價來源'}
    quote = max(quotes, key=lambda e: (e.event_time, e.evidence_id))
    if quote.currency != entity.currency:
        return None, {'status': 'conflicting', 'note': f'收盤價幣別 {quote.currency} 與報表幣別 {entity.currency} 不同'}
    from datetime import timezone
    tz = timezone(timedelta(hours=EXCHANGE_TZ_HOURS.get(entity.exchange, 0)))
    trade_day = quote.event_time.astimezone(tz).date()
    lag = weekdays_between(trade_day, data.request.research_as_of.astimezone(tz).date())
    info = {'status': 'available', 'value': quote.value, 'currency': quote.currency, 'trade_date': trade_day.isoformat(),
            'evidence_ids': [quote.evidence_id], 'trading_days_lag': lag,
            'note': '交易日以週一至週五估算，未納入交易所假日'}
    if lag > MAX_PRICE_LAG_TRADING_DAYS:
        info['status'] = 'stale'
        info['note'] = f'收盤價距截止時點 {lag} 個交易日，超過 {MAX_PRICE_LAG_TRADING_DAYS} 日'
    return quote, info


def result(method, status, value=None, unit=None, inputs=(), formula=None, limitations=(), as_of=None):
    return {'method': method, 'label': METHOD_LABELS[method], 'status': status,
            'value': None if value is None else round(value, 6), 'unit': unit, 'inputs': list(inputs),
            'formula': formula, 'limitations': list(limitations), 'as_of': as_of}


def inp(name, value, unit, evidence_ids, window=None):
    return {'name': name, 'value': value, 'unit': unit, 'evidence_ids': sorted(set(evidence_ids)), 'window': window}


def shares(series):
    out = latest_instant(series, 'shares_outstanding')
    if out:
        return out['value'], out['evidence_ids'], f'@{out["end"]}', None
    diluted = series['quarters'].get('diluted_shares', [])
    if diluted:
        last = diluted[-1]
        return last['value'], last['evidence_ids'], f'{last["start"]}~{last["end"]}', \
            '以最新一季加權平均稀釋股數代替期末流通股數'
    return None


def value(data, series, entity, analysis):
    profile_name, profile = sector_profile(entity.entity_id)
    methods = profile['valuation_methods']
    as_of = data.request.research_as_of.isoformat()
    quote, price = latest_price(data, entity)
    results, market = [], {'price': price}
    share = shares(series)
    if share:
        market['shares'] = {'value': share[0], 'evidence_ids': share[1], 'window': share[2], 'note': share[3]}
    if share and quote is not None:
        market['market_cap'] = quote.value * share[0]
    debt = debt_at(series)
    cash = latest_instant(series, 'cash_equivalents')
    if 'market_cap' in market and debt and cash and debt[1] == cash['end']:
        market['net_debt'] = debt[0] - cash['value']
        market['enterprise_value'] = market['market_cap'] + market['net_debt']
        market['net_debt_ids'] = debt[2] + cash['evidence_ids']
    price_ok = price['status'] == 'available'
    no_price = '缺少可用的最新收盤價' if price['status'] == 'not_found' else price['note']
    eps_metric = next((m for m in analysis['metrics'] if m['name'] == 'eps_ttm' and m['status'] == 'available'), None)
    eps = None if eps_metric is None else {'value': eps_metric['value'], 'evidence_ids': eps_metric['evidence_ids'],
                                           'window': eps_metric['window'], 'formula': eps_metric['formula']}
    for method in methods:
        if method in ('pe_trailing', 'pe_forward', 'ev_ebitda', 'p_fcf', 'fcf_yield', 'p_b') and not price_ok:
            results.append(result(method, 'stale' if price['status'] == 'stale' else 'source_unavailable',
                                  limitations=[no_price], as_of=as_of))
            continue
        if method == 'pe_trailing':
            if eps is None:
                results.append(result(method, 'not_found', limitations=['沒有連續四季稀釋 EPS'], as_of=as_of))
            elif eps['value'] <= 0:
                results.append(result(method, 'not_applicable', limitations=['TTM EPS 為零或負，本益比無意義'], as_of=as_of))
            else:
                results.append(result(method, 'available', quote.value / eps['value'], 'x', as_of=as_of,
                                      inputs=[inp('price', quote.value, quote.currency, [quote.evidence_id]),
                                              inp('eps_ttm', eps['value'], 'per_share', eps['evidence_ids'], eps['window'])],
                                      formula=f'price / TTM EPS ({eps["formula"]})',
                                      limitations=['TTM EPS：' + eps['formula']]))
        elif method == 'pe_forward':
            forward = [e for e in data.evidence if e.entity_id == entity.entity_id
                       and e.statement_kind == 'consensus_estimate' and e.financial_metric in ('diluted_eps', 'eps')
                       and e.value and e.target_period_end and e.target_period_end > data.request.research_as_of.date()
                       and reportable(e, data.request)]
            if not forward:
                results.append(result(method, 'not_found', as_of=as_of, limitations=[
                    '沒有可驗證的市場共識 EPS；以公司指引推算的 EPS 屬模型假設，只用於情境估值']))
            else:
                f = min(forward, key=lambda e: e.target_period_end)
                results.append(result(method, 'available' if f.value > 0 else 'not_applicable',
                                      quote.value / f.value if f.value > 0 else None, 'x', as_of=as_of,
                                      inputs=[inp('price', quote.value, quote.currency, [quote.evidence_id]),
                                              inp('consensus_eps', f.value, 'per_share', [f.evidence_id], f.target_period)],
                                      formula='price / consensus EPS', limitations=[f'共識來源：{f.institution}']))
        elif method == 'ev_ebitda':
            ebitda = next((m for m in analysis['metrics'] if m['name'] == 'ebitda_ttm' and m['status'] == 'available'), None)
            if 'enterprise_value' not in market or ebitda is None:
                results.append(result(method, 'not_found', as_of=as_of, limitations=[
                    '缺少股數、同日現金與負債，或 TTM 折舊攤銷，無法計算 EV/EBITDA']))
            elif ebitda['value'] <= 0:
                results.append(result(method, 'not_applicable', as_of=as_of, limitations=['TTM EBITDA 為零或負']))
            else:
                results.append(result(method, 'available', market['enterprise_value'] / ebitda['value'], 'x', as_of=as_of,
                                      inputs=[inp('enterprise_value', market['enterprise_value'], entity.currency,
                                                  [quote.evidence_id] + market['shares']['evidence_ids'] + market['net_debt_ids']),
                                              inp('ebitda_ttm', ebitda['value'], entity.currency, ebitda['evidence_ids'], ebitda['window'])],
                                      formula='(price * shares + debt - cash) / TTM EBITDA',
                                      limitations=[n for n in [market['shares']['note'], '有息負債不含租賃負債'] if n]))
        elif method in ('p_fcf', 'fcf_yield'):
            fcf = next((m for m in analysis['metrics'] if m['name'] == 'free_cash_flow' and m['status'] == 'available'), None)
            if 'market_cap' not in market or fcf is None or not str(fcf['window']).startswith('TTM'):
                why = '缺少股數' if 'market_cap' not in market else (
                    '沒有 TTM 自由現金流' if fcf is None else '只有年初至今的自由現金流；不年化成 TTM')
                results.append(result(method, 'not_found', as_of=as_of, limitations=[why]))
            elif method == 'p_fcf' and fcf['value'] <= 0:
                results.append(result(method, 'not_applicable', as_of=as_of, limitations=['TTM 自由現金流為零或負']))
            else:
                v = market['market_cap'] / fcf['value'] if method == 'p_fcf' else fcf['value'] / market['market_cap'] * 100
                results.append(result(method, 'available', v, 'x' if method == 'p_fcf' else 'percent', as_of=as_of,
                                      inputs=[inp('market_cap', market['market_cap'], entity.currency,
                                                  [quote.evidence_id] + market['shares']['evidence_ids']),
                                              inp('free_cash_flow_ttm', fcf['value'], entity.currency, fcf['evidence_ids'], fcf['window'])],
                                      formula='market cap / TTM FCF' if method == 'p_fcf' else 'TTM FCF / market cap * 100',
                                      limitations=[n for n in [market['shares']['note']] if n]))
        elif method == 'p_b':
            equity = latest_instant(series, 'total_equity')
            if equity is None or 'shares' not in market:
                results.append(result(method, 'not_found', as_of=as_of, limitations=['缺少股東權益或股數']))
            else:
                bvps = equity['value'] / market['shares']['value']
                results.append(result(method, 'available' if bvps > 0 else 'not_applicable',
                                      quote.value / bvps if bvps > 0 else None, 'x', as_of=as_of,
                                      inputs=[inp('book_value_per_share', bvps, 'per_share',
                                                  equity['evidence_ids'] + market['shares']['evidence_ids'])],
                                      formula='price / (equity / shares)'))
        elif method == 'historical_range':
            results.append(result(method, 'source_unavailable', as_of=as_of, limitations=[
                '沒有已查核的歷史股價序列，無法建立歷史本益比區間']))
        elif method == 'peer_relative':
            results.append(result(method, 'not_found', as_of=as_of, limitations=[
                '沒有已查核的同業估值資料；不以未查核網站的倍數比較']))
    available = [r for r in results if r['status'] == 'available']
    reference = [r for r in available if r['method'] in ('historical_range', 'peer_relative')]
    if not price_ok:
        assessment = 'unavailable'
    elif not available:
        assessment = 'unavailable'
    elif not reference:
        assessment = 'current_multiples_only'
    else:
        assessment = 'evaluated'
    return {'profile': profile_name, 'methods_considered': methods, 'results': results, 'market': market,
            'assessment': assessment}
