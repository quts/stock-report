"""Strict, timezone-aware contracts for untrusted inputs."""
from datetime import date, timedelta, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, model_validator

Horizon = Literal['3-6m', '6-12m']
Outlook = Literal['bullish', 'neutral', 'bearish', 'insufficient']
MetricRole = Literal['revenue_current', 'revenue_prior', 'eps_current', 'eps_prior', 'net_income',
                     'operating_cash_flow', 'capital_expenditure', 'close',
                     'close_anchor_3m', 'close_anchor_6m', 'close_anchor_12m']
# Who a record comes from. `official` is the issuer's regulator filing or the listing exchange's own
# dataset; `secondary` is a market-data redistributor whose figures are not an official record.
SourceClass = Literal['official', 'secondary']
# What a price series has been adjusted for. `unknown` means the provider does not document it: such a
# series may show a latest price, but is never compared across time. A dividend-adjusted series is a
# total-return construct and is never reported as a price change.
PriceAdjustment = Literal['unadjusted', 'split_adjusted', 'split_dividend_adjusted', 'unknown']
# Calendar-month windows for price change, and how far before the target date the anchor session may be.
PRICE_CHANGE_MONTHS = (3, 6, 12)
PRICE_ANCHOR_TOLERANCE_DAYS = 7
# How `published_at` was established. `observed_availability` means the source never declared a
# publication time: `published_at` then only carries the instant the record was observed to exist,
# which is an upper bound and never a claim that publication happened at that moment.
PublicationBasis = Literal['source_declared_timestamp', 'source_declared_date', 'observed_availability', 'synthetic']
# What a financial figure's reporting window covers. A label such as `FY2026` never establishes
# comparability on its own: two windows are only compared when their recorded start/end dates and
# basis say they may be. `cumulative_ytd` is a fiscal-year-to-date interim figure and is never
# annualized or treated as a trailing-twelve-month value.
PeriodBasis = Literal['fiscal_year', 'cumulative_ytd', 'single_quarter', 'trailing_twelve_months', 'trading_day', 'instant']


def check_period_window(start, end, basis):
    if (start is None) != (end is None):
        raise ValueError('A period window needs both its start and its end date')
    if start is not None and (start > end or basis is None):
        raise ValueError('A period window must be ordered and must declare its basis')


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)

class Entity(Model):
    entity_id: str = Field(pattern=r'^[A-Z0-9.\-]+$', max_length=24)
    name: str = Field(min_length=1, max_length=80)
    exchange: Literal['NASDAQ', 'NYSE', 'TWSE', 'TPEx']
    currency: Literal['USD', 'TWD']
    share_class: str = 'common'

    @model_validator(mode='after')
    def market_currency(self):
        expected = 'TWD' if self.exchange in ('TWSE', 'TPEx') else 'USD'
        if self.currency != expected:
            raise ValueError('Exchange/currency mismatch for supported common shares')
        return self

class Request(Model):
    mode: Literal['stock', 'event']
    ticker: str | None = None
    exchange: Literal['NASDAQ', 'NYSE', 'TWSE', 'TPEx'] | None = None
    event_query: str | None = Field(default=None, max_length=400)
    research_as_of: AwareDatetime
    lookback_days: int = Field(default=30, ge=1, le=365)
    horizons: list[Horizon] = Field(default_factory=lambda: ['3-6m', '6-12m'])
    max_companies: int = Field(default=5, ge=1, le=5)
    language: Literal['zh-TW'] = 'zh-TW'
    max_pdf_pages: int = Field(default=4, ge=1, le=8)
    # `four_page` keeps the compact layout (at most four pages). `investment` adds the
    # deterministic financial-analysis, valuation and scenario sections and may use up to
    # eight pages rather than omitting material analysis.
    report_profile: Literal['four_page', 'investment'] = 'four_page'
    # Outputs are exclusively for the operator's personal use. No sharing profile exists, and a
    # request asking for one is rejected instead of being quietly downgraded.
    sharing_profile: Literal['private'] = 'private'

    @model_validator(mode='after')
    def validate_mode(self):
        if self.mode == 'stock' and (not self.ticker or self.event_query):
            raise ValueError('Stock mode requires ticker and forbids event_query')
        if self.mode == 'event' and (not self.event_query or self.ticker or self.exchange):
            raise ValueError('Event mode requires event_query and forbids ticker/exchange')
        if sorted(self.horizons) != ['3-6m', '6-12m']:
            raise ValueError('Both distinct research horizons are required')
        if self.report_profile == 'four_page' and self.max_pdf_pages > 4:
            raise ValueError('The four-page profile allows at most four pages')
        if self.report_profile == 'investment' and (self.mode != 'stock' or self.max_pdf_pages < 4):
            raise ValueError('The investment profile is a stock report of four to eight pages')
        return self

class Evidence(Model):
    evidence_id: str = Field(pattern=r'^[A-Za-z0-9_.-]+$', max_length=80)
    entity_id: str
    claim: str = Field(max_length=500)
    kind: Literal['identity', 'financial', 'price', 'event', 'rating', 'relationship']
    financial_metric: str | None = Field(default=None, max_length=80)
    statement_kind: Literal['fact', 'reported_claim', 'management_guidance', 'analyst_estimate', 'consensus_estimate', 'scenario'] = 'fact'
    target_period: str | None = Field(default=None, max_length=80)
    value: float | None = None
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_basis: PeriodBasis | None = None
    price_adjustment: PriceAdjustment | None = None
    source_url: str
    article_url: str | None = Field(default=None, pattern=r'^https://[^\s]+$', max_length=1000)
    # Accounting basis of a financial figure. Reported (GAAP/IFRS) and adjusted figures never
    # share a series or a ratio; None means the source did not classify it.
    accounting_basis: Literal['gaap', 'ifrs', 'non_gaap', 'company_adjusted', 'not_applicable'] | None = None
    # Period a forward estimate refers to, so guidance, consensus and actuals align on dates
    # rather than on free-text labels.
    target_period_end: date | None = None
    publisher: str
    source_type: Literal['official', 'secondary', 'synthetic']
    source_record_id: str
    metric_key: MetricRole | None = None
    source_hash: str | None = None
    event_time: AwareDatetime
    published_at: AwareDatetime
    publication_basis: PublicationBasis = 'source_declared_timestamp'
    availability_observed_at: AwareDatetime | None = None
    source_published_raw: str | None = Field(default=None, max_length=120)
    source_period_raw: str | None = Field(default=None, max_length=120)
    source_unit_raw: str | None = Field(default=None, max_length=60)
    retrieved_at: AwareDatetime
    research_as_of: AwareDatetime
    verification_status: Literal['verified', 'unverified', 'conflicting', 'unavailable']
    license_status: Literal['confirmed', 'unknown', 'restricted'] = 'unknown'
    attribution: str
    # Legacy redistribution flag, kept for stored-record compatibility. It gates nothing: the
    # personal report depends on `internal_use_allowed` with confirmed terms.
    shareable_in_pdf: bool = False
    internal_use_allowed: bool = False
    terms_url: str
    license_checked_at: AwareDatetime
    license_source_id: str | None = Field(default=None, max_length=64)
    license_registry_version: str | None = Field(default=None, max_length=32)
    institution: str | None = None
    analyst: str | None = None
    raw_rating: str | None = None
    valid_until: AwareDatetime | None = None
    target_price: float | None = None

    @model_validator(mode='after')
    def publication_time_is_modeled_truthfully(self):
        check_period_window(self.period_start, self.period_end, self.period_basis)
        if self.statement_kind in ('management_guidance', 'analyst_estimate', 'consensus_estimate') and not self.target_period:
            raise ValueError('Forward estimates require an explicit target period')
        if self.statement_kind in ('analyst_estimate', 'consensus_estimate') and not self.institution:
            raise ValueError('External estimates require a named analyst or consensus provider')
        if self.publication_basis == 'observed_availability':
            if self.availability_observed_at is None or self.availability_observed_at != self.published_at:
                raise ValueError('Observed availability must be recorded explicitly and carried as the publication upper bound')
        elif self.availability_observed_at is not None and self.availability_observed_at < self.published_at:
            raise ValueError('Observed availability cannot precede the declared publication time')
        if self.source_type == 'official' and self.publication_basis == 'synthetic':
            raise ValueError('Official evidence cannot declare a synthetic publication basis')
        return self

class Relationship(Model):
    entity_id: str
    relationship: Literal['direct', 'indirect', 'hypothetical']
    evidence_ids: list[str]

class Metric(Model):
    entity_id: str
    name: str
    value: float | None
    unit: str
    currency: str | None
    period: str | None
    formula: str
    evidence_ids: list[str]
    missing_reason: str | None = None

EventReviewStatus = Literal['completed', 'failed', 'not_run']
EventCategory = Literal['management_change', 'guidance', 'earnings', 'merger_acquisition', 'regulatory',
                        'legal', 'financing', 'product', 'other']


ImpactArea = Literal['revenue_earnings', 'free_cash_flow', 'competitive_position', 'capital_allocation',
                     'valuation', 'regulatory_geopolitical']
DataStatus = Literal['available', 'not_applicable', 'not_disclosed', 'not_found', 'source_unavailable',
                     'conflicting', 'stale']


class DataGap(Model):
    """A recorded outcome for a required field that has no usable value.

    A failed retrieval is `source_unavailable`, never proof that the data does not exist."""
    field: str = Field(pattern=r'^[a-z0-9_]+$', max_length=60)
    status: Literal['not_applicable', 'not_disclosed', 'not_found', 'source_unavailable']
    reason: str = Field(min_length=1, max_length=300)
    sources_tried: list[str] = Field(default_factory=list, max_length=12)
    checked_at: AwareDatetime


class MaterialLead(Model):
    """A potentially material event the review could not establish from usable evidence.

    `summary` is the reviewer's own wording. It never quotes or carries data from a source whose
    terms are unconfirmed, and it is always presented as unverified."""
    lead_id: str = Field(pattern=r'^[A-Za-z0-9_.-]+$', max_length=80)
    summary: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=200)


class ReviewedEvent(Model):
    """A company event established by usable evidence inside the reviewed window."""
    event_id: str = Field(pattern=r'^[A-Za-z0-9_.-]+$', max_length=80)
    title: str = Field(min_length=1, max_length=200)
    category: EventCategory
    materiality: Literal['major', 'notable'] = 'major'
    status: Literal['occurred', 'in_effect', 'officially_announced', 'proposed']
    event_time: AwareDatetime
    verification: Literal['official', 'secondary_corroborated']
    evidence_ids: list[str] = Field(min_length=1, max_length=6)
    impact_areas: list[ImpactArea] = Field(default_factory=list, max_length=6)


class EventReview(Model):
    """What is known about recent company events for a stock request, and how it is known.

    Absence of events is only meaningful when `status` is `completed`: a failed or omitted scan
    says nothing about whether something happened. A real stock import without this record is a
    financial snapshot, never complete stock research."""
    status: EventReviewStatus
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    reviewed_by: Literal['human', 'assistant', 'synthetic-fixture'] | None = None
    reviewer: str | None = Field(default=None, max_length=120)
    reviewed_at: AwareDatetime | None = None
    authorization: str | None = Field(default=None, max_length=300)
    # Reviewer-described places that were searched (names or URLs); never source content.
    sources_checked: list[str] = Field(default_factory=list, max_length=24)
    failure_reason: str | None = Field(default=None, max_length=300)
    events: list[ReviewedEvent] = Field(default_factory=list, max_length=24)
    unresolved_leads: list[MaterialLead] = Field(default_factory=list, max_length=24)

    @model_validator(mode='after')
    def status_is_supported_by_its_record(self):
        if self.status == 'completed':
            if (self.window_start is None or self.window_end is None or self.window_start >= self.window_end
                    or self.reviewed_by is None or not self.reviewer or self.reviewed_at is None
                    or not self.sources_checked):
                raise ValueError('A completed event review must record its window, reviewer, time and the sources checked')
        elif self.status == 'failed' and not self.failure_reason:
            raise ValueError('A failed event review must say why it failed')
        if len({e.event_id for e in self.events}) != len(self.events) or \
                len({lead.lead_id for lead in self.unresolved_leads}) != len(self.unresolved_leads):
            raise ValueError('Duplicate event or lead IDs in event review')
        return self

    def complete(self):
        """Completed with nothing material left open."""
        return self.status == 'completed' and not self.unresolved_leads


def event_review_state(data):
    """Why a stock run is not complete research, as stable codes; empty when it is (or for event mode)."""
    if data.request.mode != 'stock':
        return []
    review = data.event_review
    if review is None:
        return ['EVENT_REVIEW_MISSING']
    codes = [] if review.status == 'completed' else [f'EVENT_REVIEW_{review.status.upper()}']
    return codes + (['MATERIAL_LEAD_UNRESOLVED'] if review.unresolved_leads else [])


def research_scope(data):
    if data.request.mode != 'stock':
        return 'event_research'
    return 'financial_snapshot' if data.event_review is None or data.event_review.status != 'completed' \
        else 'stock_research'


class FilingDiscovery(Model):
    """Latest eligible filing found in a licensed official submissions index at the cutoff."""
    entity_id: str
    cik: str = Field(pattern=r'^\d{10}$')
    as_of: AwareDatetime
    discovered_at: AwareDatetime
    form: Literal['10-K', '10-Q', '10-K/A', '10-Q/A']
    accession: str = Field(pattern=r'^\d{10}-\d{2}-\d{6}$')
    period_end: date
    published_at: AwareDatetime
    document_url: str
    discovery_evidence_id: str


class LatestFinancialReview(Model):
    discovery: FilingDiscovery
    evidence_ids: list[str] = Field(default_factory=list, max_length=160)


class EarningsPreviewReview(Model):
    status: Literal['completed', 'unavailable', 'not_run']
    as_of: AwareDatetime
    searched_at: AwareDatetime
    search_queries: list[str] = Field(min_length=1, max_length=16)
    sources_checked: list[str] = Field(min_length=1, max_length=24)
    next_earnings_at: AwareDatetime | None = None
    schedule_evidence_ids: list[str] = Field(default_factory=list, max_length=6)
    expectation_evidence_ids: list[str] = Field(default_factory=list, max_length=24)
    limitations: list[str] = Field(default_factory=list, max_length=12)


TW_EXCHANGES = ('TWSE', 'TPEx')
TAIPEI = timezone(timedelta(hours=8))


def tw_required_period_end(cutoff, fiscal_year_end_month=12):
    """Latest period whose statutory report must already be public at the cutoff.

    Securities and Exchange Act Art. 36: Q1-Q3 reports within 45 days after the quarter ends, the
    annual report within three months after the fiscal year ends. A report is only required once
    its deadline day has fully passed in Taipei. Only December fiscal years are supported."""
    if fiscal_year_end_month != 12:
        return None
    today = cutoff.astimezone(TAIPEI).date()
    due = []
    for year in (today.year - 2, today.year - 1, today.year):
        for month, day in ((3, 31), (6, 30), (9, 30)):
            end = date(year, month, day)
            due.append((end, end + timedelta(days=45)))
        due.append((date(year, 12, 31), date(year + 1, 3, 31)))
    return max(end for end, deadline in due if deadline < today)


def tw_financial_review_state(data):
    """Taiwan issuers have no SEC-style filing index here: freshness follows the statutory deadline."""
    from .storage import read_json
    cutoff, ticker = data.request.research_as_of, data.request.ticker
    rows = [r for r in read_json('config/tw-instruments.json')['instruments'] if r['ticker'] == ticker]
    if len(rows) != 1:
        return ['LATEST_FINANCIAL_REVIEW_INVALID']
    required = tw_required_period_end(cutoff, rows[0].get('fiscal_year_end_month'))
    if required is None:
        return ['LATEST_FINANCIAL_REVIEW_INVALID']
    facts = [e for e in data.evidence
             if e.entity_id == ticker and e.kind == 'financial' and e.statement_kind == 'fact'
             and e.source_type == 'official' and e.value is not None and e.period_end is not None
             and e.financial_metric in ('revenue', 'net_income') and e.verification_status == 'verified'
             and e.license_status == 'confirmed' and e.internal_use_allowed
             and e.published_at <= cutoff and e.event_time <= cutoff and e.research_as_of == cutoff]
    if not facts:
        return ['LATEST_FINANCIAL_REPORT_MISSING']
    latest = max(e.period_end for e in facts)
    if latest < required:
        return ['LATEST_FINANCIAL_REPORT_STALE']
    if {'revenue', 'net_income'} - {e.financial_metric for e in facts if e.period_end == latest}:
        return ['LATEST_FINANCIAL_STATEMENTS_INCOMPLETE']
    return []


def financial_review_state(data):
    """A completed real stock study must include the latest published statements, not only annual facts."""
    if data.request.mode != 'stock' or data.dataset_kind != 'real':
        return []
    if data.request.exchange in TW_EXCHANGES:
        return tw_financial_review_state(data)
    review = data.latest_financials
    if review is None:
        return ['LATEST_FINANCIAL_REPORT_MISSING']
    discovery, cutoff = review.discovery, data.request.research_as_of
    from .storage import read_json
    configured = [row for row in read_json('config/sec-entities.json')['entities']
                  if row['ticker'] == data.request.ticker]
    if (discovery.entity_id != data.request.ticker or discovery.as_of != cutoff
            or discovery.discovered_at < cutoff or discovery.published_at > cutoff
            or discovery.period_end > cutoff.date() or len(configured) != 1
            or configured[0]['cik'] != discovery.cik
            or not discovery.document_url.startswith(f'https://www.sec.gov/Archives/edgar/data/{int(discovery.cik)}/{discovery.accession.replace("-", "")}/')):
        return ['LATEST_FINANCIAL_REVIEW_INVALID']
    index = {e.evidence_id: e for e in data.evidence}
    def usable(e):
        return bool(e and e.entity_id == data.request.ticker and e.verification_status == 'verified'
                    and e.license_status == 'confirmed' and e.internal_use_allowed
                    and e.published_at <= cutoff and e.event_time <= cutoff and e.research_as_of == cutoff)
    discovery_record = index.get(discovery.discovery_evidence_id)
    if (not usable(discovery_record) or discovery_record.license_source_id != 'sec-submissions-history'
            or discovery_record.source_url != f'https://data.sec.gov/submissions/CIK{discovery.cik}.json'
            or discovery_record.source_record_id != discovery.accession):
        return ['LATEST_FINANCIAL_DISCOVERY_UNVERIFIED']
    rows = [index.get(eid) for eid in review.evidence_ids]
    rows = [e for e in rows if usable(e) and e.kind == 'financial' and e.value is not None
            and e.statement_kind == 'fact' and e.source_url == discovery.document_url
            and e.period_end == discovery.period_end]
    required = {'revenue', 'net_income', 'operating_cash_flow'}
    if not required <= {e.financial_metric for e in rows}:
        return ['LATEST_FINANCIAL_STATEMENTS_INCOMPLETE']
    if discovery.form.startswith('10-Q') and not any(e.financial_metric == 'revenue'
                                           and e.period_basis == 'single_quarter' for e in rows):
        return ['LATEST_QUARTER_MISSING']
    return []


class ResearchInput(Model):
    schema_version: Literal['1'] = '1'
    dataset_kind: Literal['mock', 'real']
    request: Request
    entities: list[Entity] = Field(min_length=1, max_length=5)
    evidence: list[Evidence]
    metrics: list[Metric]
    relationships: list[Relationship] = Field(default_factory=list)
    event_evidence_ids: list[str] = Field(default_factory=list)
    # Recent-event review for a stock request. None means no review was recorded at all.
    event_review: EventReview | None = None
    latest_financials: LatestFinancialReview | None = None
    earnings_preview: EarningsPreviewReview | None = None
    data_gaps: list[DataGap] = Field(default_factory=list, max_length=60)
    versions: dict[str, str]

    @model_validator(mode='after')
    def unique_records(self):
        ids = [e.evidence_id for e in self.evidence]
        companies = [e.entity_id for e in self.entities]
        if len(ids) != len(set(ids)) or len(companies) != len(set(companies)):
            raise ValueError('Duplicate evidence or entity IDs')
        if len(companies) > self.request.max_companies:
            raise ValueError('Company limit exceeded')
        if self.request.mode == 'stock' and (len(companies) != 1 or companies[0] != self.request.ticker):
            raise ValueError('Stock identity mismatch')
        if self.dataset_kind == 'real' and any(e.source_type == 'synthetic' for e in self.evidence):
            raise ValueError('Synthetic evidence cannot be labeled real')
        preview = self.earnings_preview
        if preview is not None:
            if preview.as_of != self.request.research_as_of or preview.searched_at < preview.as_of:
                raise ValueError('Earnings preview review must cover the supplied cutoff')
            if preview.next_earnings_at is not None and preview.next_earnings_at <= preview.as_of:
                raise ValueError('Next earnings time must be a future scheduled event')
            index = {e.evidence_id: e for e in self.evidence}
            for eid in preview.schedule_evidence_ids + preview.expectation_evidence_ids:
                e = index.get(eid)
                if e is None or e.entity_id != self.request.ticker or e.published_at > preview.as_of:
                    raise ValueError('Earnings preview cites absent, wrong-issuer or future evidence')
            for eid in preview.expectation_evidence_ids:
                if index[eid].statement_kind not in ('management_guidance', 'analyst_estimate', 'consensus_estimate'):
                    raise ValueError('Earnings expectations must classify guidance or external estimates explicitly')
        review = self.event_review
        if review is not None:
            if self.request.mode != 'stock':
                raise ValueError('An event review record belongs to a stock request')
            if (self.dataset_kind == 'real') == (review.reviewed_by == 'synthetic-fixture') and review.reviewed_by:
                raise ValueError('A synthetic fixture review labels mock data only, and mock data carries no other review')
            if self.dataset_kind == 'real' and review.reviewed_by == 'assistant' and not review.authorization:
                raise ValueError('An assistant event review must record the user authorization it rests on')
            if review.status == 'completed' and (
                    review.window_end != self.request.research_as_of
                    or review.window_start > self.request.research_as_of - timedelta(days=self.request.lookback_days)):
                raise ValueError('A completed event review must cover the whole request window up to the research cutoff')
            index = {e.evidence_id: e for e in self.evidence}
            for event in review.events:
                rows = [index.get(eid) for eid in event.evidence_ids]
                if any(e is None or e.kind != 'event' or e.entity_id != self.request.ticker for e in rows):
                    raise ValueError('A reviewed event must cite event evidence of the requested company')
                if event.event_time > self.request.research_as_of:
                    raise ValueError('A reviewed event cannot postdate the research cutoff')
        return self


class CitedParagraph(Model):
    text: str = Field(min_length=1, max_length=1600)
    evidence_ids: list[str] = Field(min_length=1, max_length=16)


class CompanyResearch(Model):
    entity_id: str
    overview: list[CitedParagraph] = Field(default_factory=list, max_length=4)
    key_takeaways: list[CitedParagraph] = Field(default_factory=list, max_length=4)
    fundamentals: list[CitedParagraph] = Field(default_factory=list, max_length=8)
    news: list[CitedParagraph] = Field(default_factory=list, max_length=8)
    earnings_preview: list[CitedParagraph] = Field(default_factory=list, max_length=6)
    monitoring: list[CitedParagraph] = Field(default_factory=list, max_length=6)

class Assumption(Model):
    """One explicit scenario input. A model assumption is labelled as such, never as a fact."""
    name: str = Field(min_length=1, max_length=80)
    value: float
    unit: str = Field(min_length=1, max_length=40)
    basis: Literal['historical_fact', 'management_guidance', 'guidance_informed_model', 'consensus', 'model_assumption']
    evidence_ids: list[str] = Field(default_factory=list, max_length=64)
    rationale: str = Field(min_length=1, max_length=300)
    source_period: str | None = None
    forecast_period: str | None = None
    accounting_basis: str | None = None
    derivation: str | None = None


ForecastDriver = Literal['revenue_growth', 'operating_margin', 'non_operating_ratio', 'tax_rate', 'share_change',
                         'da_intensity', 'capex_intensity', 'working_capital_intensity', 'ocf_residual_intensity']


class OperatingInvalidation(Model):
    driver: ForecastDriver
    operator: Literal['lt', 'gt']
    threshold: float
    period: str
    rationale: str = Field(min_length=1, max_length=300)


class ScenarioProposal(Model):
    entity_id: str
    name: Literal['bull', 'base', 'bear']
    # Must equal the engine's forecast horizon label (see investment_inputs.json forecast.horizon).
    target_period: str = Field(min_length=1, max_length=40)
    # Driver-based forecast: each driver is a percentage with its origin. Omitted drivers fall back to
    # the deterministic historical anchor and are labelled historical_fact.
    drivers: dict[ForecastDriver, Assumption] = Field(default_factory=dict)
    assumptions: list[Assumption] = Field(default_factory=list, max_length=8)
    financial_outcome: str = Field(min_length=1, max_length=400)
    valuation_metric: Literal['eps', 'fcf_per_share', 'book_value_per_share', 'ebitda']
    # Legacy direct input. With drivers the engine computes the metric; a supplied value must reconcile.
    valuation_metric_value: float | None = None
    multiple_method: Literal['pe', 'p_fcf', 'p_b', 'ev_ebitda']
    multiple: float = Field(gt=0, le=200)
    multiple_basis: Literal['current_trailing', 'historical_range', 'peer_median', 'model_assumption']
    multiple_evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    catalysts: list[str] = Field(default_factory=list, max_length=5)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=5)
    operating_invalidations: list[OperatingInvalidation] = Field(default_factory=list, max_length=8)
    leading_indicators: list[str] = Field(default_factory=list, max_length=5)


class InvestmentThesis(Model):
    entity_id: str
    market_pricing: CitedParagraph
    key_assumptions: list[CitedParagraph] = Field(min_length=1, max_length=4)
    supporting: list[CitedParagraph] = Field(default_factory=list, max_length=4)
    opposing: list[CitedParagraph] = Field(default_factory=list, max_length=4)
    invalidation: list[str] = Field(min_length=1, max_length=5)


class HorizonProposal(Model):
    entity_id: str
    horizon: Horizon
    outlook: Outlook
    supporting_evidence_ids: list[str] = Field(max_length=12)
    counter_evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    interpretation: str = Field(max_length=350)
    assumptions: list[str] = Field(default_factory=list, max_length=4)
    unresolved_conflicts: list[str] = Field(default_factory=list, max_length=4)
    invalidation_conditions: list[str] = Field(default_factory=list, max_length=4)
    conflict_resolution: str | None = Field(default=None, max_length=350)

class Proposal(Model):
    input_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    model: str
    prompt_version: Literal['5'] = '5'
    usage: dict[str, int] | None = None
    outlooks: list[HorizonProposal] = Field(max_length=10)
    company_research: list[CompanyResearch] = Field(default_factory=list, max_length=5)
    thesis: list[InvestmentThesis] = Field(default_factory=list, max_length=5)
    scenarios: list[ScenarioProposal] = Field(default_factory=list, max_length=15)

class Finding(Model):
    rule_id: str
    entity_id: str
    horizon: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    message: str
    severity: Literal['hard', 'soft']

class Decision(Model):
    status: Literal['APPROVED', 'APPROVED_WITH_WARNINGS', 'REVISE', 'REJECTED']
    findings: list[Finding]
    outlooks: list[HorizonProposal]
    policy_version: str
