"""Independent acceptance of undeclared monetary-scale arithmetic."""
from datetime import date, timedelta
import pytest
from stock_report.quant import calculate


@pytest.mark.parametrize('changed_field,changed_value',[
    ('source_record_id','different-statement'),
    ('source_url','https://example.invalid/other-statement'),
    ('publisher','Different synthetic source'),
    ('retrieved_at',None),
])
def test_undeclared_scale_cancels_only_within_one_statement_snapshot(data,changed_field,changed_value):
    rows=[e for e in data.evidence if e.metric_key in ('revenue_current','net_income')]
    for e in rows:
        e.source_type='official'
        e.unit='source_scale_undeclared'
        e.period_start=date(2025,1,1)
        e.period_end=date(2025,12,31)
        e.period_basis='fiscal_year'
        e.publisher='Synthetic statement contract'
        e.source_record_id='same-statement-row'
        e.source_url='https://example.invalid/same-statement'
    baseline={m.name:m for m in calculate(data.evidence,data.request.research_as_of)}
    assert baseline['net_margin'].value==20
    income=next(e for e in rows if e.metric_key=='net_income')
    setattr(income,changed_field,income.retrieved_at+timedelta(seconds=1) if changed_field=='retrieved_at' else changed_value)
    result={m.name:m for m in calculate(data.evidence,data.request.research_as_of)}
    assert result['net_margin'].value is None, 'Undeclared scales from separate sources or snapshots cannot be assumed equal'
    assert result['net_margin'].missing_reason
