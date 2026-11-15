"""Exchange trading calendars: weekends, holidays and whether a year's list is verified."""
from datetime import date, timedelta
from .storage import read_json


def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def _observed(d, saturday_rule=True):
    if d.weekday() == 5:
        return d - timedelta(days=1) if saturday_rule else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year):
    """NYSE full-day closures from the exchange's fixed rules; ad hoc closures are not modelled."""
    days = {_observed(date(year, 1, 1), saturday_rule=False),  # a Saturday New Year is not observed
            _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3), _easter(year) - timedelta(days=2),
            _last_weekday(year, 5, 0), _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 11, 3, 4),
            _observed(date(year, 7, 4)), _observed(date(year, 12, 25))}
    if year >= 2022:
        days.add(_observed(date(year, 6, 19)))
    return {d for d in days if d is not None}


def closed_weekdays(exchange, year, config=None):
    """(set of non-trading weekdays, verified) for one exchange and year."""
    config = config or read_json('config/trading-calendars.json')
    spec = config['exchanges'].get(exchange, {})
    if spec.get('rules') == 'nyse_fixed_rules':
        return nyse_holidays(year), True
    listed = spec.get('years', {}).get(str(year))
    if listed is None:
        return set(), False
    return {date.fromisoformat(d) for d in listed['closed_weekdays']}, True


def is_trading_day(exchange, day, config=None):
    closed, verified = closed_weekdays(exchange, day.year, config)
    return day.weekday() < 5 and day not in closed, verified


def trading_days_after(exchange, earlier, later, config=None):
    """Trading days strictly after `earlier` up to and including `later`, and whether every year was verified."""
    count, verified, day = 0, True, earlier
    while day < later:
        day += timedelta(days=1)
        trading, known = is_trading_day(exchange, day, config)
        count += trading
        verified = verified and known
    return count, verified
