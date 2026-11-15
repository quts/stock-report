"""Authored registry payloads for the assistant-source-review code path.

Everything here is fabricated test content built on the closed registry snapshot fixture. It proves
nothing about any real source's terms and is never written to config/.
"""
import copy
from stock_report.storage import read_json

CLOSED = 'tests/fixtures/licenses-closed.json'
CLOSED_PACKAGE = 'tests/fixtures/activation-package-closed.json'
CLOSED_INSTRUMENTS = 'tests/fixtures/tw-instruments-closed.json'
REVIEWED_AT = '2026-11-16T10:00:00+08:00'
CHECKED_AT = '2026-11-16T09:00:00+08:00'
ASPECTS = ('official_source', 'identity', 'publication_time', 'terms_of_use', 'free_access')


def source_of(payload, source_id):
    return next(s for s in payload['sources'] if s['source_id'] == source_id)


def finding(aspect, url):
    return {'aspect': aspect, 'official_url': url, 'checked_at': CHECKED_AT,
            'excerpt': 'UNIT TEST TEXT - not a quotation of any real official page.',
            'conclusion': 'unit test only'}


def assistant_review(source, extra_urls=()):
    """A structurally sufficient review of `source`; `extra_urls` add further terms findings."""
    findings = [finding(aspect, source['terms_url']) for aspect in ASPECTS]
    findings += [finding('terms_of_use', url) for url in extra_urls]
    return {'reviewed_by': 'assistant', 'reviewer': 'unit-test assistant (not a real review)',
            'reviewed_at': REVIEWED_AT, 'authorization': 'unit test stand-in for the user authorization',
            'reviewed_endpoint_scope': source['endpoint_scope'], 'verdict': 'sufficient',
            'findings': findings, 'unresolved': []}


def assistant_reviewed_payload(source_ids, version='9.8.0', base=CLOSED):
    """The closed snapshot with `source_ids` confirmed for internal use by an assistant review only."""
    payload = copy.deepcopy(read_json(base))
    payload['registry_version'] = version
    for source_id in source_ids:
        source = source_of(payload, source_id)
        source.update(declared_status='confirmed', scope={'internal_use_allowed': True},
                      source_review=assistant_review(source))
    return payload
