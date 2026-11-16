"""Fetch a reviewer-curated set of EDGAR facts, retaining normalized observations only."""
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import re
import uuid
from pydantic import Field
from typing import Literal
from .models import Model, Entity
from .sources import Observation, FILING_PROFILES
from .storage import read_json
from .stock_events import filing_url_problem

SOURCE = 'sec-edgar-archives-filing'


class FilingFact(Model):
    evidence_id: str = Field(pattern=r'^[A-Za-z0-9_.-]+$', max_length=80)
    url: str
    claim: str = Field(min_length=1, max_length=500)
    excerpts: list[str] = Field(min_length=1, max_length=6)
    # Only dates independently reviewed against the filing/index may be supplied.
    publication_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    event_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    kind: Literal['event', 'financial'] = 'event'
    financial_metric: str | None = Field(default=None, max_length=80)
    numeric_text: str | None = Field(default=None, max_length=80)
    number_scale: Literal[1, 1000, 1000000, 1000000000] = 1
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_basis: Literal['fiscal_year', 'cumulative_ytd', 'single_quarter', 'instant'] | None = None
    statement_kind: Literal['fact', 'reported_claim', 'management_guidance', 'analyst_estimate', 'consensus_estimate', 'scenario'] = 'fact'
    target_period: str | None = None
    target_period_end: date | None = None
    institution: str | None = None
    accounting_basis: Literal['gaap', 'ifrs', 'non_gaap', 'company_adjusted', 'not_applicable'] | None = None


class FilingPlan(Model):
    reviewed_by: str = Field(min_length=1, max_length=120)
    authorization: str = Field(min_length=1, max_length=300)
    facts: list[FilingFact] = Field(min_length=1, max_length=64)


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def normalized(value):
    return ' '.join(value.split())


def collect_filings(ticker, *, registry, allow_network, plan, client, now=None):
    """Validate issuer, permission, exact excerpts and dates; do not infer arbitrary claims."""
    from .collect import Collection
    if not allow_network:
        raise ValueError('Filing collection requires --allow-network; no fixture fallback')
    plan = FilingPlan.model_validate(plan)
    if len({f.evidence_id for f in plan.facts}) != len(plan.facts):
        raise ValueError('Duplicate filing evidence IDs')
    profile = FILING_PROFILES[SOURCE]
    snapshot = registry.snapshot(SOURCE)
    if not snapshot['source_reviewed'] or not snapshot['internal_use_allowed']:
        raise ValueError('Filing source is not reviewed and open for internal use')
    config = read_json(profile['entities_config'])
    rows = [r for r in config['entities'] if r['ticker'] == ticker]
    if len(rows) != 1:
        raise ValueError('Filing issuer must be uniquely configured')
    row = rows[0]
    # Validate every URL before any network request, including issuer CIK binding.
    for fact in plan.facts:
        problem = filing_url_problem(profile, fact.url, ticker, config)
        if problem:
            raise ValueError(problem)
        if not registry.get(SOURCE).covers_endpoint(fact.url):
            raise ValueError('Filing URL outside reviewed scope')
        if any(len(normalized(q)) < 12 for q in fact.excerpts):
            raise ValueError('Filing excerpts must identify substantive text')
    observations, transport, documents = [], [], {}
    for fact in plan.facts:
        if fact.url not in documents:
            raw, meta = client.fetch(fact.url, 'text/html')
            parser = Text()
            parser.feed(raw.decode('utf-8'))
            documents[fact.url] = normalized(' '.join(parser.parts))
            transport.append({'key': fact.url, 'url': fact.url, 'status': 'retrieved',
                              'attempts': meta.get('attempts'), 'cached': False})
        text = documents[fact.url]
        if normalized(row['expected_entity_name_contains']).casefold() not in text.casefold():
            raise ValueError('Issuer name not established by filing content')
        if any(normalized(q) not in text for q in fact.excerpts):
            raise ValueError(f'{fact.evidence_id}: reviewed excerpt absent from live filing')
        value = None
        if fact.numeric_text is not None:
            if not any(normalized(fact.numeric_text) in normalized(q) for q in fact.excerpts):
                raise ValueError('Numeric token must be inside a reviewed live excerpt')
            token = fact.numeric_text.replace(',', '').replace('$', '').strip()
            if token.startswith('(') and token.endswith(')'):
                token = '-' + token[1:-1].strip()
            # A reviewed statement dash explicitly represents a zero amount, not an invented residual.
            value = (0.0 if token == '-' else float(token)) * fact.number_scale
        if fact.kind == 'financial' and (value is None or not fact.financial_metric
                                         or fact.period_end is None or fact.period_basis is None):
            raise ValueError('Financial observations require a numeric token, metric and reporting window')
        # A date-only publication stamp is a conservative availability upper bound.
        pub = datetime.fromisoformat(fact.publication_date + 'T23:59:59.999999-05:00')
        event = datetime.fromisoformat(fact.event_date + 'T00:00:00-05:00')
        observed = now or datetime.now(timezone.utc)
        if pub > observed or event > observed:
            raise ValueError('Future filing date cannot establish an occurred event')
        observations.append(Observation(evidence_id=fact.evidence_id, entity_id=ticker, kind=fact.kind,
            claim=fact.claim, provider_record_id=fact.evidence_id, request_key=fact.url,
            financial_metric=fact.financial_metric, value=value, unit=fact.unit, currency=fact.currency,
            period=fact.period, period_start=fact.period_start, period_end=fact.period_end,
            period_basis=fact.period_basis, statement_kind=fact.statement_kind,
            target_period=fact.target_period, institution=fact.institution,
            target_period_end=fact.target_period_end, accounting_basis=fact.accounting_basis,
            source_url=fact.url, source_published_raw=fact.publication_date,
            publication_basis='source_declared_date', published_at=pub, event_time=event,
            retrieved_at=observed))
    return Collection(collection_id=uuid.uuid4().hex, source_id=SOURCE, endpoint=profile['endpoint'],
        capture_mode='network', source_class='official', coverage_mode='historical_filings',
        requested_ticker=ticker, collected_at=now or datetime.now(timezone.utc),
        entity=Entity(entity_id=ticker, name=row['expected_entity_name_contains'],
                      exchange=row['exchange'], currency=row['currency'], share_class=row['share_class']),
        observations=observations, license=snapshot, field_binding={'issuer': 'archive_path_cik'},
        transport=transport, sec_contact_configured=True, retention='research_input_allowed',
        notes=['Reviewer-curated filing facts; live exact-excerpt and issuer checks passed. '
               'This is bounded collection, not automatic news discovery. '
               'No raw responses or response hashes retained.'])
