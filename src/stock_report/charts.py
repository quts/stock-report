"""Small inline SVG charts for the PDF; data only, no decoration, no external resources."""
from html import escape

INK, MUTED, GRID = '#203047', '#64748b', '#dce5eb'
SERIES = ('#17778b', '#c2410c', '#6d28d9')


def _scale(values, low=None, high=None):
    vals = [v for v in values if v is not None]
    if not vals:
        return 0, 1
    lo = min(vals + ([low] if low is not None else []))
    hi = max(vals + ([high] if high is not None else []))
    if lo == hi:
        hi = lo + 1
    return lo, hi


def bars_and_line(labels, bars, line=None, bar_label='', line_label='', width=330, height=165):
    """Bars (e.g. revenue) with an optional second-axis line (e.g. EPS)."""
    if not labels or all(b is None for b in bars):
        return None
    left, right, top, bottom = 46, 46 if line else 10, 18, 34
    w, h = width - left - right, height - top - bottom
    _, bhi = _scale(bars, 0)
    step = w / len(labels)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             f'<line x1="{left}" y1="{top + h}" x2="{left + w}" y2="{top + h}" stroke="{GRID}"/>']
    for i, (lab, b) in enumerate(zip(labels, bars)):
        x = left + i * step + step * 0.18
        if b is not None and b > 0:
            bh = b / bhi * h
            parts.append(f'<rect x="{x:.1f}" y="{top + h - bh:.1f}" width="{step * 0.64:.1f}" height="{bh:.1f}" fill="{SERIES[0]}"/>')
        parts.append(f'<text x="{x + step * 0.32:.1f}" y="{top + h + 13}" font-size="7" text-anchor="middle" fill="{MUTED}">{escape(lab)}</text>')
    parts.append(f'<text x="{left}" y="11" font-size="8.5" fill="{SERIES[0]}">■ {escape(bar_label)}</text>')
    if line and any(v is not None for v in line):
        lo, hi = _scale(line, 0)
        pts = [(left + i * step + step * 0.5, top + h - (v - lo) / (hi - lo) * h) for i, v in enumerate(line) if v is not None]
        parts.append('<polyline fill="none" stroke="{}" stroke-width="1.8" points="{}"/>'.format(
            SERIES[1], ' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)))
        parts += [f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="{SERIES[1]}"/>' for x, y in pts]
        parts.append(f'<text x="{left + w}" y="11" font-size="8.5" text-anchor="end" fill="{SERIES[1]}">● {escape(line_label)}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def lines(labels, named_series, unit='%', width=330, height=165):
    data = [(n, s) for n, s in named_series if any(v is not None for v in s)]
    if not labels or not data:
        return None
    left, right, top, bottom = 34, 24, 18, 30
    w, h = width - left - right, height - top - bottom
    lo, hi = _scale([v for _, s in data for v in s])
    lo, hi = lo - (hi - lo) * 0.1, hi + (hi - lo) * 0.1
    step = w / max(1, len(labels) - 1)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    for frac in (0, 0.5, 1):
        y = top + h - frac * h
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + w}" y2="{y:.1f}" stroke="{GRID}"/>')
        parts.append(f'<text x="{left - 4}" y="{y + 3:.1f}" font-size="8" text-anchor="end" fill="{MUTED}">{lo + frac * (hi - lo):.0f}{unit}</text>')
    for i, lab in enumerate(labels):
        parts.append(f'<text x="{left + i * step:.1f}" y="{top + h + 13}" font-size="7" text-anchor="middle" fill="{MUTED}">{escape(lab)}</text>')
    for k, (name, s) in enumerate(data):
        color = SERIES[k % len(SERIES)]
        pts = [(left + i * step, top + h - (v - lo) / (hi - lo) * h) for i, v in enumerate(s) if v is not None]
        parts.append('<polyline fill="none" stroke="{}" stroke-width="1.8" points="{}"/>'.format(
            color, ' '.join(f'{x:.1f},{y:.1f}' for x, y in pts)))
        parts.append(f'<text x="{left + 6 + k * 90}" y="11" font-size="8" fill="{color}">— {escape(name)}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def scenario_bars(rows, reference=None, unit='', width=520, height=150):
    rows = [r for r in rows if r['implied_price'] is not None]
    if not rows:
        return None
    left, top, bar_h, gap = 60, 14, 24, 14
    hi = max([r['implied_price'] for r in rows] + ([reference] if reference else []))
    w = width - left - 80
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    colors = {'bear': SERIES[1], 'base': SERIES[0], 'bull': SERIES[2]}
    for i, r in enumerate(rows):
        y = top + i * (bar_h + gap)
        bw = r['implied_price'] / hi * w
        parts.append(f'<text x="{left - 6}" y="{y + bar_h * 0.65:.1f}" font-size="9" text-anchor="end" fill="{INK}">{r["name"].title()}</text>')
        parts.append(f'<rect x="{left}" y="{y}" width="{bw:.1f}" height="{bar_h}" fill="{colors[r["name"]]}"/>')
        parts.append(f'<text x="{left + bw + 4:.1f}" y="{y + bar_h * 0.65:.1f}" font-size="9" fill="{INK}">{r["implied_price"]:,.0f}{unit}</text>')
    if reference:
        x = left + reference / hi * w
        parts.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" y2="{top + len(rows) * (bar_h + gap)}" stroke="{INK}" stroke-dasharray="3,2"/>')
        parts.append(f'<text x="{x:.1f}" y="{top - 8}" font-size="8" text-anchor="middle" fill="{INK}">參考價 {reference:,.0f}</text>')
    parts.append('</svg>')
    return ''.join(parts)
