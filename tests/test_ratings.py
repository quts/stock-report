"""Institution-level consensus rejects conflicting/invalid records and mappings."""
from datetime import timedelta
import pytest
from stock_report.quant import consensus


def rows(data):
    base = data.evidence[0]
    cutoff = data.request.research_as_of
    def make(eid, institution='Firm One', raw='Buy', **changes):
        return base.model_copy(update=dict(evidence_id=eid, kind='rating', institution=institution,
            raw_rating=raw, analyst='Analyst One', published_at=cutoff-timedelta(days=1),
            valid_until=cutoff+timedelta(days=1), target_price=100) | changes)
    return make, cutoff


def test_normalized_institution_override_and_duplicate_messages(data, mapping):
    make, cutoff = rows(data)
    mapping['institutions'][' Firm ONE '] = {'Outperform': 'Buy'}
    records = [make('a', 'Ｆｉｒｍ  One', 'outperform'), make('b', 'firm one', 'Outperform')]
    result = consensus(records + records, 'NVDA', cutoff, mapping)
    assert result['institutions'] == 1 and result['counts']['Buy'] == 1
    assert result['message_count'] == 2 and result['analysts'] == 1
    assert result['rows'][0]['evidence_ids'] == ['a', 'b']


def test_same_time_conflict_does_not_depend_on_evidence_id_or_input_order(data, mapping):
    make, cutoff = rows(data)
    records = [make('a', raw='Buy'), make('z', raw='Sell')]
    first = consensus(records, 'NVDA', cutoff, mapping)
    assert first == consensus(list(reversed(records)), 'NVDA', cutoff, mapping)
    assert first['counts']['Unclassified'] == 1 and first['counts']['Buy'] == 0
    assert first['conflicting_institutions'] == 1 and '衝突' in first['warning']
    assert first['rows'][0]['raw_ratings'] == ['Buy', 'Sell']


def test_only_latest_institution_timestamp_counts(data, mapping):
    make, cutoff = rows(data)
    records = [make('old', raw='Sell', published_at=cutoff-timedelta(days=2)), make('new', raw='Buy')]
    result = consensus(records, 'NVDA', cutoff, mapping)
    assert result['counts']['Buy'] == 1 and result['rows'][0]['evidence_ids'] == ['new']
    assert result['message_count'] == 2


@pytest.mark.parametrize('change', [{'license_status': 'unknown'}, {'internal_use_allowed': False},
    {'verification_status': 'unverified'}, {'institution': '  '}, {'valid_until': None},
    {'valid_until': 'expired'}, {'published_at': 'future'}, {'event_time': 'future'},
    {'research_as_of': 'other'}])
def test_unusable_messages_do_not_inflate_counts(data, mapping, change):
    make, cutoff = rows(data)
    change = dict(change)
    for key, value in list(change.items()):
        if value == 'expired': change[key] = cutoff-timedelta(seconds=1)
        if value in ('future', 'other'): change[key] = cutoff+timedelta(seconds=1)
    result = consensus([make('a', **change)], 'NVDA', cutoff, mapping)
    assert result['institutions'] == result['message_count'] == result['analysts'] == 0


def test_target_price_conflict_is_not_arbitrarily_resolved(data, mapping):
    make, cutoff = rows(data)
    result = consensus([make('a'), make('b', target_price=200)], 'NVDA', cutoff, mapping)
    assert result['counts']['Buy'] == 1 and result['rows'][0]['target_price'] is None


def test_same_analyst_name_at_different_institutions_is_not_merged(data, mapping):
    make, cutoff = rows(data)
    result = consensus([make('a', institution='Firm One'), make('b', institution='Firm Two')], 'NVDA', cutoff, mapping)
    assert result['analysts'] == 2


@pytest.mark.parametrize('currency', [None, 'TWD'])
def test_target_price_requires_one_explicit_consistent_currency(data, mapping, currency):
    make, cutoff = rows(data)
    result = consensus([make('a', currency='USD'), make('b', currency=currency)], 'NVDA', cutoff, mapping)
    assert result['rows'][0]['target_price'] is None


@pytest.mark.parametrize('mapping', [
    {'default': {'Buy': 'StrongBuy'}}, {'default': {'Buy': 'Buy', 'BUY': 'Sell'}},
    {'default': [], 'institutions': {}}, {'default': {}, 'institutions': []},
    {'default': {}, 'institutions': {'Firm': {}, ' FIRM ': {}}}])
def test_invalid_mapping_cannot_silently_change_consensus(data, mapping):
    make, cutoff = rows(data)
    with pytest.raises(ValueError): consensus([make('a')], 'NVDA', cutoff, mapping)
