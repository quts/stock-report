"""What the research needs, what is available, and why the rest is missing.

The requirement list is generic; the sector profile decides which entries are mandatory. A field
with no usable value is `not_found` unless a recorded gap (DataGap) says what actually happened,
so a failed retrieval is reported as `source_unavailable`, never as proof of absence.
"""
from .fin_analysis import sector_profile

REQUIREMENTS = [
    ('price_latest', '最新收盤價'), ('price_history', '歷史股價序列'), ('shares', '股數'),
    ('market_cap', '市值'), ('net_debt', '淨負債'), ('revenue_quarterly', '單季營收'),
    ('margins', '毛利率／營業利益率'), ('net_income_quarterly', '單季淨利'), ('eps', 'EPS（TTM）'),
    ('operating_cash_flow', '營業現金流'), ('capital_expenditure', '資本支出'), ('free_cash_flow', '自由現金流'),
    ('working_capital', '營運資金'), ('total_equity', '股東權益'), ('history_8q', '八季歷史'),
    ('guidance', '公司指引'), ('consensus', '市場共識'), ('peers', '同業估值'),
]
LABELS = dict(REQUIREMENTS)


def _metric(analysis, name):
    return next((m for m in analysis['metrics'] if m['name'] == name), None)


def assess(data, series, analysis, valuation, expectations, entity_id):
    """`valuation` carries the market block and the historical/peer references."""
    profile_name, profile = sector_profile(entity_id)
    mandatory = set(profile['mandatory'])
    gaps = {g.field: g for g in data.data_gaps}
    conflicts = {c['metric'] for c in series['conflicts']}
    market, price = valuation['market'], valuation['market']['price']

    def status_of(field):
        if field == 'price_latest':
            return price['status'], price.get('note')
        if field == 'price_history':
            ref = valuation.get('references', {}).get('historical_pe', {})
            if ref.get('status') == 'sufficient':
                return 'available', f'{ref["stats"]["count"]} 個季末估值點'
            return 'not_found', f'歷史收盤價 {price.get("history_points", 0)} 筆，不足以建立歷史估值區間'
        if field == 'shares':
            return ('available', market['shares'].get('note')) if 'shares' in market else ('not_found', '沒有股數資料')
        if field == 'market_cap':
            return ('available', None) if 'market_cap' in market else ('not_found', '需要收盤價與股數')
        if field == 'net_debt':
            m = _metric(analysis, 'net_debt')
            return (m['status'], m['note']) if m else ('not_found', None)
        if field == 'revenue_quarterly':
            return ('available', None) if series['quarters'].get('revenue') else ('not_found', None)
        if field == 'margins':
            m = _metric(analysis, 'operating_margin') or _metric(analysis, 'gross_margin')
            return (m['status'], m['note']) if m else ('not_found', None)
        if field == 'net_income_quarterly':
            return ('available', None) if series['quarters'].get('net_income') else ('not_found', None)
        if field == 'eps':
            m = _metric(analysis, 'eps_ttm')
            return (m['status'], m['note']) if m else ('not_found', None)
        if field == 'operating_cash_flow':
            ok = series['quarters'].get('operating_cash_flow') or series['cumulative'].get('operating_cash_flow')
            return ('available', None) if ok else ('not_found', None)
        if field == 'capital_expenditure':
            ok = series['quarters'].get('capital_expenditure') or series['cumulative'].get('capital_expenditure')
            return ('available', None) if ok else ('not_found', None)
        if field == 'free_cash_flow':
            m = _metric(analysis, 'free_cash_flow')
            return (m['status'], m['window'] if m['status'] == 'available' else m['note']) if m else ('not_found', None)
        if field == 'working_capital':
            m = _metric(analysis, 'working_capital_change')
            return (m['status'], m['note']) if m else ('not_found', None)
        if field == 'total_equity':
            return ('available', None) if series['instants'].get('total_equity') else ('not_found', None)
        if field == 'history_8q':
            m = _metric(analysis, 'history_quarters')
            return ('available', None) if m and m['value'] >= 8 else ('not_found', m['note'] if m else None)
        if field == 'guidance':
            return ('available', None) if expectations['guidance'] else ('not_found', None)
        if field == 'consensus':
            return ('available', None) if expectations['consensus'] else ('not_found', '沒有已查核的共識來源')
        if field == 'peers':
            peer = valuation.get('references', {}).get('peers', {}).get('pe_trailing')
            if peer and peer['status'] == 'sufficient':
                return 'available', f'{peer["stats"]["count"]} 家同業'
            return 'not_found', '同業估值資料不足或未匯入'
        return 'not_found', None

    rows = []
    metric_of = {'revenue_quarterly': 'revenue', 'net_income_quarterly': 'net_income', 'eps': 'diluted_eps',
                 'operating_cash_flow': 'operating_cash_flow', 'capital_expenditure': 'capital_expenditure'}
    for field, label in REQUIREMENTS:
        status, note = status_of(field)
        gap = gaps.get(field)
        if status != 'available' and metric_of.get(field) in conflicts:
            status, note = 'conflicting', '同一期間的來源數值不一致，已排除該期'
        if status in ('not_found',) and gap is not None:
            status, note = gap.status, gap.reason
        if profile_name == 'financials' and field in ('capital_expenditure', 'free_cash_flow', 'working_capital'):
            status, note = 'not_applicable', '金融業不以此衡量'
        # Provider status codes and CLI remedies stay in qa.json; the report gets plain language.
        reader = note
        if note and ('=' in note or 'cli ' in note or 'import-market-data' in note):
            reader = ('沒有可合法取得、已查核權限的收盤價；可由使用者匯入有權使用的價格資料' if field.startswith('price')
                      else '來源無法取得；來源狀態與補救方式見 qa.json')
        rows.append({'field': field, 'label': label, 'status': status, 'mandatory': field in mandatory, 'note': note,
                     'reader_note': reader, 'sources_tried': gap.sources_tried if gap else []})
    warnings = [f'{r["label"]}：{r["status"]}' + (f'（{r["note"]}）' if r['note'] else '')
                for r in rows if r['mandatory'] and r['status'] != 'available']
    return {'profile': profile_name, 'fields': rows, 'mandatory_gaps': [r['field'] for r in rows
                                                                      if r['mandatory'] and r['status'] != 'available'],
            'warnings': warnings}
