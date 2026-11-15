"""Strict, timezone-aware contracts for untrusted inputs."""
from datetime import date
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
PeriodBasis = Literal['fiscal_year', 'cumulative_ytd', 'single_quarter', 'trailing_twelve_months', 'trading_day']


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
    max_pdf_pages: Literal[2] = 2
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
        return self

class Evidence(Model):
    evidence_id: str = Field(pattern=r'^[A-Za-z0-9_.-]+$', max_length=80)
    entity_id: str
    claim: str = Field(max_length=500)
    kind: Literal['identity', 'financial', 'price', 'event', 'rating', 'relationship']
    value: float | None = None
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_basis: PeriodBasis | None = None
    price_adjustment: PriceAdjustment | None = None
    source_url: str
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

class ResearchInput(Model):
    schema_version: Literal['1'] = '1'
    dataset_kind: Literal['mock', 'real']
    request: Request
    entities: list[Entity] = Field(min_length=1, max_length=5)
    evidence: list[Evidence]
    metrics: list[Metric]
    relationships: list[Relationship] = Field(default_factory=list)
    event_evidence_ids: list[str] = Field(default_factory=list)
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
        return self

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
    prompt_version: Literal['2'] = '2'
    usage: dict[str, int] | None = None
    outlooks: list[HorizonProposal] = Field(max_length=10)

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
