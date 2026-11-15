"""Investment-oriented research data contract shared by Taiwan and US equities.

Every figure in an investment report belongs to exactly one category, and its provenance and
data-quality status travel with it. Categories are never converted into one another: guidance
is not consensus, a model assumption is not a fact, and a derived calculation names its inputs.
"""
from datetime import date
from .storage import read_json

CATEGORIES = ('historical_fact', 'management_guidance', 'analyst_consensus', 'model_assumption',
              'derived_calculation', 'investment_interpretation')
STATUSES = ('available', 'not_applicable', 'not_disclosed', 'not_found', 'source_unavailable',
            'conflicting', 'stale')
STATUS_LABELS = {'available': '可用', 'not_applicable': '不適用', 'not_disclosed': '公司未揭露',
                 'not_found': '未找到', 'source_unavailable': '來源無法取得', 'conflicting': '來源數值衝突',
                 'stale': '資料過期'}
ADJUSTED_BASES = ('non_gaap', 'company_adjusted')
MONEY_UNITS = ('USD', 'TWD')


def category(e):
    """Category of an evidence record; secondary reports of facts stay facts with secondary provenance."""
    return {'fact': 'historical_fact', 'reported_claim': 'historical_fact',
            'management_guidance': 'management_guidance', 'analyst_estimate': 'analyst_consensus',
            'consensus_estimate': 'analyst_consensus', 'scenario': 'model_assumption'}[e.statement_kind]


def accounting_bucket(e):
    """Reported (GAAP/IFRS or unclassified statement figures) never mix with adjusted figures."""
    return 'adjusted' if e.accounting_basis in ADJUSTED_BASES else 'reported'


def provenance(e):
    """Artifact-safe provenance of one figure."""
    return {'evidence_id': e.evidence_id, 'source_url': e.article_url or e.source_url, 'publisher': e.publisher,
            'published_at': e.published_at.isoformat(), 'period': e.period,
            'period_start': e.period_start.isoformat() if e.period_start else None,
            'period_end': e.period_end.isoformat() if e.period_end else None, 'period_basis': e.period_basis,
            'currency': e.currency, 'unit': e.unit, 'accounting_basis': e.accounting_basis,
            'category': category(e), 'as_of': e.research_as_of.isoformat(),
            'quality': 'available' if e.verification_status == 'verified' else e.verification_status}


def fiscal_year_end_month(ticker, exchange):
    """Fiscal year end from operator configuration; None when it is not configured."""
    if exchange in ('TWSE', 'TPEx'):
        rows = [r for r in read_json('config/tw-instruments.json')['instruments'] if r['ticker'] == ticker]
        return rows[0].get('fiscal_year_end_month') if len(rows) == 1 else None
    rows = [r for r in read_json('config/sec-entities.json')['entities'] if r['ticker'] == ticker]
    return rows[0].get('fiscal_year_end_month') if len(rows) == 1 else None


def canonical_period(start, end, basis, fy_end_month=None):
    """Canonical label that separates fiscal from calendar periods.

    `FY2027-Q2` is used only when the fiscal year end month is configured; otherwise the label is
    the calendar window itself, so a fiscal quarter is never presented as a calendar quarter."""
    if end is None:
        return None
    if basis == 'instant':
        return f'@{end.isoformat()}'
    window = f'{start.isoformat() if start else "?"}~{end.isoformat()}'
    if not fy_end_month or start is None:
        return window
    # Fiscal year is named by the calendar year in which it ends (Dell FY2027 ends Jan 2027).
    fy = end.year + (1 if end.month > fy_end_month else 0)
    if fy_end_month == 12:
        fy = end.year
    if basis == 'fiscal_year':
        return f'FY{fy}'
    months_in = (end.year * 12 + end.month - (fy - 1) * 12 - fy_end_month) % 12 or 12
    quarter = min(4, max(1, round(months_in / 3)))
    tag = {'single_quarter': f'Q{quarter}', 'cumulative_ytd': f'{quarter * 3}M'}.get(basis, basis)
    return f'FY{fy}-{tag}'


def compatible(a, b):
    """Why two figures may not be combined in one ratio or comparison, or None."""
    if accounting_bucket(a) != accounting_bucket(b):
        return 'adjusted and reported figures are not combined'
    if a.unit in MONEY_UNITS and b.unit in MONEY_UNITS and (a.currency or a.unit) != (b.currency or b.unit):
        return 'different currencies are not combined without a disclosed conversion'
    if 'source_scale_undeclared' in (a.unit, b.unit):
        return 'undeclared monetary scale'
    return None


def days_between(a: date, b: date):
    return abs((a - b).days)
