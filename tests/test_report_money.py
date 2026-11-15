"""Monetary display must fit large values without rounding small amounts to zero."""
import pytest
from pypdf import PdfReader
from stock_report.policy import evaluate
from stock_report.quant import calculate
from stock_report.render import render


@pytest.mark.parametrize('cash,expected', [
    (8552000000, '85.52'), (9000, '9000.00 USD'), (-9000, '-9000.00 USD')])
def test_money_scale_keeps_amount_and_currency(data, proposal, mapping, tmp_path, cash, expected):
    for e in data.evidence:
        if e.metric_key in ('operating_cash_flow', 'capital_expenditure'):
            e.unit = 'USD'
            e.value = cash if e.metric_key == 'operating_cash_flow' else 0
    data.metrics = calculate(data.evidence, data.request.research_as_of)
    path = tmp_path/'money.pdf'
    assert render(data, evaluate(data, proposal), mapping, path) == 2
    text = ''.join(page.extract_text() for page in PdfReader(path).pages)
    assert expected in text and 'USD USD' not in text
