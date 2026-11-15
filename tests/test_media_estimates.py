"""Official vs secondary media, and guidance vs named estimates vs scenarios, stay distinct."""
from datetime import timedelta
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from stock_report.collect import collect
from stock_report.models import CompanyResearch, CitedParagraph, Proposal
from stock_report.policy import reportable, validate_company_research
from stock_report.reviewed_collect import DELL_SCHEDULE
from v2_fixtures import CUTOFF, evidence, preview, real_dell

NOW = CUTOFF + timedelta(minutes=1)
RSS = 'lemonde-economy-rss'
IR = 'dell-ir-next-earnings-1661'


class Registry:
    def __init__(self, source_class, allowed=True, scope=('https://',)):
        self.source_class, self.allowed, self.scope = source_class, allowed, scope

    def snapshot(self, source):
        return dict(source_id=source, source_class=self.source_class, source_reviewed=self.allowed,
                    internal_use_allowed=self.allowed, review_basis='assistant_source_review')

    def get(self, source):
        return SimpleNamespace(covers_endpoint=lambda url: url.startswith(self.scope))


class Client:
    def __init__(self, body):
        self.body, self.calls = body, []

    def fetch(self, url, accept):
        self.calls.append(url)
        return self.body.encode(), {'attempts': 1, 'response_sha256': 'never-retain'}


def rss(items, title='Le Monde.fr - Economy'):
    rows = ''.join(f'<item><title>{t}</title><description>{d}</description><link>{l}</link>'
                   f'<guid>{l}</guid><pubDate>{p}</pubDate></item>' for t, d, l, p in items)
    return f'<?xml version="1.0"?><rss><channel><title>{title}</title>{rows}</channel></rss>'


ITEM = ('Dell shares rise on AI servers', 'Test-only summary about Dell.',
        'https://www.lemonde.fr/en/economy/article/2026/11/10/test.html', 'Tue, 10 Nov 2026 09:00:00 +0000')


def collect_rss(body, registry=None, keywords=('Dell',)):
    client = Client(body)
    result = collect(RSS, 'DELL', registry=registry or Registry('secondary'), allow_network=True,
                     client=client, now=NOW, profile_overrides={'keywords': list(keywords)})
    return result, client


def test_media_rss_is_secondary_reported_claim_with_article_link_and_no_body():
    result, _ = collect_rss(rss([ITEM, ('Unrelated', 'Nothing', 'https://www.lemonde.fr/x', ITEM[3])]))
    assert result.source_class == 'secondary' and len(result.observations) == 1
    o = result.observations[0]
    assert o.statement_kind == 'reported_claim' and o.article_url == ITEM[2]
    assert 'never-retain' not in result.model_dump_json()


def test_future_dated_media_item_is_not_admitted():
    future = ITEM[:3] + ('Wed, 18 Nov 2026 09:00:00 +0000',)
    result, _ = collect_rss(rss([future]))
    assert result.observations == []


@pytest.mark.parametrize('body,match', [
    (rss([ITEM[:2] + ('https://evil.example/a', ITEM[3])]), 'another publisher'),
    (rss([ITEM], title='Some Other Feed'), 'identity'),
    ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss><channel/></rss>', 'DTD'),
])
def test_media_identity_and_parser_safety(body, match):
    with pytest.raises(ValueError, match=match):
        collect_rss(body)


def test_unconfirmed_media_source_stays_closed_without_network():
    client = Client(rss([ITEM]))
    with pytest.raises(ValueError, match='stays closed'):
        collect(RSS, 'DELL', registry=Registry('secondary', allowed=False), allow_network=True,
                client=client, now=NOW, profile_overrides={'keywords': ['Dell']})
    assert client.calls == []


def test_registry_class_must_match_secondary_contract():
    with pytest.raises(ValueError, match='Source class'):
        collect_rss(rss([ITEM]), registry=Registry('official'))


def ir_plan(**fact):
    base = dict(evidence_id='dell-ir-call', url=DELL_SCHEDULE, claim='Test-only scheduled call',
                excerpts=['will hold a conference call on November 24'],
                publication_date='2026-11-03', event_date='2026-11-03')
    base.update(fact)
    return dict(reviewed_by='test', authorization='test only', facts=[base])


IR_BODY = '<p>Dell Technologies (NYSE: DELL) will hold a conference call on November 24. Released Nov 3</p>'


def collect_ir(plan=None, body=IR_BODY, ticker='DELL'):
    return collect(IR, ticker, registry=Registry('official'), allow_network=True, client=Client(body),
                   now=NOW, profile_overrides=plan or ir_plan())


def test_official_issuer_release_binds_issuer_and_exact_excerpt():
    result = collect_ir()
    assert result.source_class == 'official' and result.observations[0].kind == 'event'
    assert 'excerpts' not in result.model_dump_json()


def test_issuer_release_rejects_missing_excerpt_wrong_issuer_or_ticker():
    with pytest.raises(ValueError, match='absent'):
        collect_ir(body=IR_BODY.replace('November 24', 'December 1'))
    with pytest.raises(ValueError, match='issuer identity'):
        collect_ir(body=IR_BODY.replace('NYSE: DELL', 'NYSE: HPQ'))
    with pytest.raises(ValueError):
        collect_ir(ticker='NVDA')
    with pytest.raises(ValueError, match='another endpoint'):
        collect_ir(plan=ir_plan(url='https://investors.delltechnologies.com/other'))


def test_management_guidance_carries_target_period():
    result = collect_ir(plan=ir_plan(statement_kind='management_guidance', target_period='Q4 FY2027'))
    assert result.observations[0].statement_kind == 'management_guidance'


# --- typed forward-looking statements -------------------------------------------------------

def test_forward_estimates_need_target_period_and_named_provider():
    with pytest.raises(ValidationError, match='target period'):
        evidence('g', statement_kind='management_guidance')
    with pytest.raises(ValidationError, match='named analyst'):
        evidence('a', statement_kind='analyst_estimate', target_period='Q4 FY2027')
    evidence('a', statement_kind='consensus_estimate', target_period='Q4 FY2027', institution='Named provider')
    evidence('s', statement_kind='scenario')  # a scenario is labelled, never attributed to anyone


def guidance():
    return evidence('guide', kind='event', statement_kind='management_guidance', target_period='Q4 FY2027',
                    financial_metric='revenue', value=31e9, unit='USD', currency='USD')


def test_preview_expectations_must_be_classified_forward_statements():
    with pytest.raises(ValidationError, match='classify'):
        real_dell(earnings=preview(status='completed', expectation_evidence_ids=['dell-identity']))
    data = real_dell(extra=[guidance()], earnings=preview(status='completed', expectation_evidence_ids=['guide']))
    assert data.earnings_preview.expectation_evidence_ids == ['guide']


@pytest.mark.parametrize('changes,match', [
    ({'next_earnings_at': CUTOFF - timedelta(days=1)}, 'future'),
    ({'as_of': CUTOFF - timedelta(days=1)}, 'cutoff'),
    ({'expectation_evidence_ids': ['missing']}, 'absent'),
])
def test_preview_rejects_past_schedule_wrong_cutoff_or_missing_citation(changes, match):
    with pytest.raises(ValidationError, match=match):
        real_dell(earnings=preview(**changes))


def test_preview_cannot_cite_a_record_published_after_cutoff():
    late = guidance().model_copy(update={'published_at': CUTOFF + timedelta(hours=2)})
    with pytest.raises(ValidationError, match='future evidence'):
        real_dell(extra=[late], earnings=preview(expectation_evidence_ids=['guide']))


# --- company research citations -----------------------------------------------------------

def paragraph(*ids):
    return CitedParagraph(text='測試段落。', evidence_ids=list(ids))


def research(**changes):
    base = {f: [paragraph('q-revenue-single_quarter')] for f in
            ('overview', 'key_takeaways', 'fundamentals', 'news', 'earnings_preview', 'monitoring')}
    base.update(changes)
    return CompanyResearch(entity_id='DELL', **base)


def proposal(*sections):
    return Proposal(input_sha256='0' * 64, model='manual-test', outlooks=[], company_research=list(sections))


def test_unreviewed_media_cannot_be_cited_in_company_research():
    closed = evidence('media-closed', kind='event', source_type='secondary', statement_kind='reported_claim',
                      license_status='unknown', internal_use_allowed=False)
    data = real_dell(extra=[closed])
    assert not reportable(closed, data.request)
    with pytest.raises(ValueError, match='unlicensed'):
        validate_company_research(data, proposal(research(news=[paragraph('media-closed')])))


def test_reviewed_secondary_media_may_be_cited_as_secondary():
    media = evidence('media-ok', kind='event', source_type='secondary', statement_kind='reported_claim',
                     article_url=ITEM[2])
    data = real_dell(extra=[media])
    assert validate_company_research(data, proposal(research(news=[paragraph('media-ok')])))


def test_four_page_real_research_needs_every_section_and_unique_entity():
    data = real_dell()
    with pytest.raises(ValueError, match='all substantive'):
        validate_company_research(data, proposal(research(news=[])))
    with pytest.raises(ValueError, match='unique'):
        validate_company_research(data, proposal(research(), research()))
    with pytest.raises(ValueError, match='unique supported'):
        validate_company_research(data, proposal(research().model_copy(update={'entity_id': 'HPQ'})))
