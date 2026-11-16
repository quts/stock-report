"""Descriptive errors; zero actuals count for MAE, bias and pooled WAPE."""
from statistics import mean


def summarize(rows):
    errors = [r['predicted'] - r['actual'] for r in rows]
    denominator = sum(abs(r['actual']) for r in rows)
    return {'mae': mean(abs(e) for e in errors) if errors else None,
            'bias': mean(errors) if errors else None,
            'wape_percent': 100 * sum(abs(e) for e in errors) / denominator if denominator else None,
            'aggregate_bias_percent': 100 * sum(errors) / denominator if denominator else None}
