"""Readable reflow keeps source cells, numeric values and citations intact."""
from stock_report.mobile_layout import reflow


def test_tables_become_vertical_records_without_losing_values_or_citations():
    html = '<html><head></head><body><table><tr><th>期間</th><th>EPS</th><th>說明</th></tr><tr><td>FY2027 Q2</td><td>6.34<sup>[7]</sup></td><td>A &amp; B<br>假設</td></tr></table></body></html>'
    result = reflow(html)
    assert '<table>' not in result
    assert '<h4>FY2027 Q2</h4>' in result
    assert '<dt>EPS</dt><dd>6.34<sup>[7]</sup></dd>' in result
    assert 'A &amp; B<br>假設' in result
    assert 'font-size:14pt' in result and 'size:135mm 240mm' in result
