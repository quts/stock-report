"""Official-source adapters: identity and field normalization only.

Rules that hold for every adapter here:

* No mock or synthetic fallback. A failed or unrecognized response raises.
* Provider record identity (`provider_record_id`) is kept separate from the
  normalized metric role (`role`), and the provider's own period/unit strings are
  preserved verbatim next to the normalized ones.
* Publication time is modeled, never guessed. A source that declares a filing or
  trading date yields `source_declared_date` with a conservative end-of-day upper
  bound; a source that declares nothing yields `observed_availability`, whose
  `published_at` is only the instant the record was observed to exist.
* Field names are a recorded contract, overridable per run, and a mismatch fails
  loudly with the observed keys instead of silently picking another column.
"""
from datetime import date, datetime, timedelta, timezone
import re
from typing import Literal
import calendar
from pydantic import AwareDatetime, Field, model_validator
from .models import (Entity, MetricRole, Model, PRICE_ANCHOR_TOLERANCE_DAYS, PRICE_CHANGE_MONTHS, PeriodBasis,
                     PriceAdjustment, PublicationBasis, check_period_window)
from .quant import months_before
from .storage import read_json

TAIPEI = timezone(timedelta(hours=8))
# A date-only US stamp is bounded by the end of that day in UTC-05:00, the latest
# wall clock a US Eastern calendar day can end on. Fixed offsets avoid depending on
# a tz database inside the image and keep the bound conservative under DST.
US_DATE_UPPER_BOUND = timezone(timedelta(hours=-5))
MAX_RECORDS = 20000


class SourceContractError(ValueError):
    """A provider response does not match the recorded field/shape contract."""


class RequestSpec(Model):
    key: str = Field(max_length=120)
    url: str
    required: bool = True


class Observation(Model):
    """One normalized provider record, before any research cutoff is applied."""
    evidence_id: str = Field(pattern=r'^[A-Za-z0-9_.-]+$', max_length=80)
    entity_id: str
    kind: Literal['identity', 'financial', 'price', 'event']
    financial_metric: str | None = Field(default=None, max_length=80)
    statement_kind: Literal['fact', 'reported_claim', 'management_guidance', 'analyst_estimate', 'consensus_estimate', 'scenario'] = 'fact'
    target_period: str | None = Field(default=None, max_length=80)
    institution: str | None = Field(default=None, max_length=120)
    role: MetricRole | None = None
    claim: str = Field(max_length=500)
    value: float | None = None
    unit: str | None = None
    currency: str | None = None
    period: str | None = None
    # The reporting window as dates, plus what it covers. `period` alone is only a label.
    period_start: date | None = None
    period_end: date | None = None
    period_basis: PeriodBasis | None = None
    price_adjustment: PriceAdjustment | None = None
    provider_record_id: str = Field(min_length=1, max_length=120)
    request_key: str = Field(max_length=120)
    source_url: str
    article_url: str | None = Field(default=None, pattern=r'^https://[^\s]+$', max_length=1000)
    accounting_basis: Literal['gaap', 'ifrs', 'non_gaap', 'company_adjusted', 'not_applicable'] | None = None
    target_period_end: date | None = None
    source_published_raw: str | None = Field(default=None, max_length=120)
    source_period_raw: str | None = Field(default=None, max_length=120)
    source_unit_raw: str | None = Field(default=None, max_length=60)
    publication_basis: PublicationBasis
    published_at: AwareDatetime
    availability_observed_at: AwareDatetime | None = None
    event_time: AwareDatetime
    retrieved_at: AwareDatetime

    @model_validator(mode='after')
    def period_window_is_complete(self):
        check_period_window(self.period_start, self.period_end, self.period_basis)
        return self


class ParseResult(Model):
    entity: Entity
    observations: list[Observation] = Field(min_length=1)
    field_binding: dict[str, str | None] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


def to_number(text):
    """Provider numerics are strings with separators; blanks stay missing, never zero."""
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    cleaned = str(text).replace(',', '').replace('　', '').strip()
    if cleaned in ('', '--', '---', 'N/A', 'null', '不適用'):
        return None
    try:
        return float(cleaned)
    except ValueError:
        raise SourceContractError(f'Numeric field is not parseable: {cleaned[:40]!r}')


# Documented calendar variants. ROC (民國) and ISO digit counts are disjoint, so a value can
# only satisfy one declared variant; anything else fails instead of being guessed.
DATE_VARIANTS = {'roc': (6, 7), 'iso': (8,)}


def parse_source_date(text, formats):
    """Return (date, variant_used). `formats` is the explicitly declared variant list."""
    if isinstance(formats, str):
        formats = [formats]
    unknown = [f for f in formats if f not in DATE_VARIANTS]
    if not formats or unknown:
        raise SourceContractError(f'Undeclared calendar variant {unknown or formats}; declared variants: '
                                  f'{sorted(DATE_VARIANTS)}')
    digits = re.sub(r'[^0-9]', '', str(text or ''))
    for variant in formats:
        if len(digits) not in DATE_VARIANTS[variant]:
            continue
        if variant == 'roc':
            year, month, day = int(digits[:-4]) + 1911, int(digits[-4:-2]), int(digits[-2:])
        else:
            year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:])
        try:
            return date(year, month, day), variant
        except ValueError:
            continue
    raise SourceContractError(f'Date {str(text)[:20]!r} matches none of the declared calendar variants '
                              f'{list(formats)}; no fallback interpretation is applied')


def start_of_day(day, tz):
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def end_of_day(day, tz):
    """Conservative upper bound for a date-only stamp."""
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=tz)


def bind_field(record, names):
    for name in names:
        if name in record:
            return name
    return None


# Normalized roles served by the Taiwan income-statement summaries. `money` figures keep the
# undeclared source scale and are only meaningful as ratios inside one filing record.
TW_INCOME_ROLES = {
    'revenue_current': {'field': 'revenue', 'kind': 'money', 'label': '營業收入', 'metric': 'revenue'},
    'net_income': {'field': 'net_income_parent', 'kind': 'money', 'label': '歸屬於母公司業主之淨利（淨損）',
                   'metric': 'net_income'},
    'eps_current': {'field': 'eps_basic', 'kind': 'per_share', 'label': '基本每股盈餘', 'metric': 'basic_eps'},
}

# Field contracts are provisional until a recorded network smoke run confirms them;
# `--source-profile` overrides any key without a code change.
PROFILES = {
    'twse-openapi-stock-day-all': {
        'adapter': 'exchange_quote', 'source_class': 'official',
        'endpoint': 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
        'exchange': 'TWSE', 'currency': 'TWD', 'ticker_suffix': '.TW',
        # A code shape is a precondition, never proof of share class: four-digit codes are also
        # used by depositary receipts and other instruments, so config/tw-instruments.json must
        # carry an explicit entry for every supported ticker.
        'code_shape_pattern': r'[1-9]\d{3}',
        'instruments_config': 'config/tw-instruments.json',
        'supported_instrument_types': ['common_share'],
        'coverage_mode': 'latest_only',
        'fields': {'code': ['Code', '證券代號'],
                   'name': ['Name', '證券名稱'],
                   'close': ['ClosingPrice', '收盤價'],
                   'date': ['Date', '日期']},
        'required_fields': ['code', 'name', 'close', 'date'],
        # Field names and the ROC trading date were confirmed against the recorded smoke sample
        # in reports/phase-0/provider-probes.json (Date=1151113, Code/Name/ClosingPrice).
        'date_formats': ['roc', 'iso'],
    },
    'tpex-openapi-mainboard-daily-close': {
        'adapter': 'exchange_quote', 'source_class': 'official',
        'endpoint': 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes',
        'exchange': 'TPEx', 'currency': 'TWD', 'ticker_suffix': '.TWO',
        'code_shape_pattern': r'[1-9]\d{3}',
        'instruments_config': 'config/tw-instruments.json',
        'supported_instrument_types': ['common_share'],
        'coverage_mode': 'latest_only',
        'fields': {'code': ['SecuritiesCompanyCode', '公司代號'],
                   'name': ['CompanyName', '公司名稱'],
                   'close': ['Close', '收盤'],
                   'date': ['Date', '日期']},
        'required_fields': ['code', 'name', 'close', 'date'],
        'date_formats': ['roc', 'iso'],
    },
    # Taiwan consolidated income statement summaries (general-industry format). Contract observed
    # on 2026-11-15, see docs/phase-0/taiwan-financial-source-notes.md:
    #  * one row per issuer for the latest filed period only, identified by ROC year + quarter;
    #  * figures are fiscal-year-to-date cumulative (cross-checked against the monthly revenue
    #    filing), so they are never a single quarter and never a trailing twelve months;
    #  * the dataset declares no monetary scale, only "（元）" on the EPS column;
    #  * the only date is the table output date, which is not an issuer filing date.
    'twse-openapi-income-general': {
        'adapter': 'tw_income_statement', 'source_class': 'official',
        'endpoint': 'https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci',
        'exchange': 'TWSE', 'currency': 'TWD', 'ticker_suffix': '.TW',
        'code_shape_pattern': r'[1-9]\d{3}',
        'instruments_config': 'config/tw-instruments.json',
        'supported_instrument_types': ['common_share'],
        'coverage_mode': 'latest_only',
        'statement_format': 'general', 'supported_fiscal_year_end_months': [12],
        'period_basis': 'cumulative_ytd', 'year_calendar': 'roc', 'date_formats': ['roc'],
        'money_unit': 'source_scale_undeclared',
        'fields': {'code': ['公司代號'], 'name': ['公司名稱'], 'year': ['年度'], 'quarter': ['季別'],
                   'table_date': ['出表日期'], 'revenue': ['營業收入'],
                   'net_income_parent': ['淨利（淨損）歸屬於母公司業主'],
                   'eps_basic': ['基本每股盈餘（元）']},
        'required_fields': ['code', 'name', 'year', 'quarter', 'table_date', 'revenue', 'net_income_parent',
                            'eps_basic'],
        'roles': TW_INCOME_ROLES,
    },
    'tpex-openapi-income-general': {
        'adapter': 'tw_income_statement', 'source_class': 'official',
        'endpoint': 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_ci',
        'exchange': 'TPEx', 'currency': 'TWD', 'ticker_suffix': '.TWO',
        'code_shape_pattern': r'[1-9]\d{3}',
        'instruments_config': 'config/tw-instruments.json',
        'supported_instrument_types': ['common_share'],
        'coverage_mode': 'latest_only',
        'statement_format': 'general', 'supported_fiscal_year_end_months': [12],
        'period_basis': 'cumulative_ytd', 'year_calendar': 'roc', 'date_formats': ['roc'],
        'money_unit': 'source_scale_undeclared',
        'fields': {'code': ['SecuritiesCompanyCode'], 'name': ['CompanyName'], 'year': ['Year'],
                   'quarter': ['Season'], 'table_date': ['Date'], 'revenue': ['營業收入'],
                   'net_income_parent': ['淨利（淨損）歸屬於母公司業主'],
                   'eps_basic': ['基本每股盈餘（元）']},
        'required_fields': ['code', 'name', 'year', 'quarter', 'table_date', 'revenue', 'net_income_parent',
                            'eps_basic'],
        'roles': TW_INCOME_ROLES,
    },
    # Secondary market-data provider, not an exchange or filing record. Contract observed on
    # 2026-11-15, see docs/phase-0/us-price-source-notes.md:
    #  * an undocumented, key-less JSON endpoint; the response itself returns symbol, currency,
    #    exchange code, instrument type and granularity, which are checked against configuration;
    #  * one row per session, stamped with the session open instant; no publication time;
    #  * `close` has no documented adjustment basis for this endpoint and `adjclose` is a
    #    dividend-adjusted (total-return style) series, so the basis stays `unknown` and no
    #    cross-date comparison is derived from real captures.
    'yahoo-chart-daily-close': {
        'adapter': 'daily_price_series', 'source_class': 'secondary',
        'endpoint': 'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d&events=split',
        'instruments_config': 'config/us-price-instruments.json',
        'coverage_mode': 'latest_only',
        'price_field': 'close', 'adjustment_basis': 'unknown',
        'granularity': '1d', 'max_rows': 800, 'max_history_days': 800, 'max_staleness_days': 10,
        'anchor_months': list(PRICE_CHANGE_MONTHS), 'anchor_tolerance_days': PRICE_ANCHOR_TOLERANCE_DAYS,
    },
    'sec-xbrl-companyconcept': {
        'adapter': 'sec_concept', 'source_class': 'official',
        'endpoint': 'https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json',
        'entities_config': 'config/sec-entities.json',
        'coverage_mode': 'historical_filings',
        'annual_forms': ['10-K'],
        'min_period_days': 300, 'max_period_days': 400,
        # `quarterly` emits every 10-Q/10-K window (single quarter, year-to-date, fiscal year and
        # balance-sheet instants) of the roles below for the most recent `max_quarters` periods.
        'series': 'annual', 'max_quarters': 12,
        'quarterly_roles': {
            'revenue': {'concepts': ['RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues'], 'units': ['USD'], 'unit': 'USD', 'required': True},
            'gross_profit': {'concepts': ['GrossProfit'], 'units': ['USD'], 'unit': 'USD'},
            'operating_income': {'concepts': ['OperatingIncomeLoss'], 'units': ['USD'], 'unit': 'USD'},
            'net_income': {'concepts': ['NetIncomeLoss'], 'units': ['USD'], 'unit': 'USD', 'required': True},
            'income_before_tax': {'concepts': ['IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest'],
                                  'units': ['USD'], 'unit': 'USD'},
            'interest_expense': {'concepts': ['InterestExpense', 'InterestExpenseNonoperating'], 'units': ['USD'], 'unit': 'USD'},
            'diluted_eps': {'concepts': ['EarningsPerShareDiluted'], 'units': ['USD/shares'], 'unit': 'per_share'},
            'diluted_shares': {'concepts': ['WeightedAverageNumberOfDilutedSharesOutstanding'], 'units': ['shares'], 'unit': 'shares'},
            'operating_cash_flow': {'concepts': ['NetCashProvidedByUsedInOperatingActivities'], 'units': ['USD'], 'unit': 'USD'},
            'capital_expenditure': {'concepts': ['PaymentsToAcquirePropertyPlantAndEquipment'], 'units': ['USD'], 'unit': 'USD'},
            'depreciation_amortization': {'concepts': ['DepreciationDepletionAndAmortization', 'DepreciationAndAmortization'], 'units': ['USD'], 'unit': 'USD'},
            'cf_share_based_compensation': {'concepts': ['ShareBasedCompensation'], 'units': ['USD'], 'unit': 'USD', 'cash_sign': 1},
            'cf_deferred_tax': {'concepts': ['DeferredIncomeTaxesAndTaxCredits', 'DeferredIncomeTaxExpenseBenefit'], 'units': ['USD'], 'unit': 'USD', 'cash_sign': 1},
            'cf_other_noncash': {'concepts': ['OtherNoncashIncomeExpense'], 'units': ['USD'], 'unit': 'USD', 'cash_sign': 1},
            'cf_receivables': {'concepts': ['IncreaseDecreaseInAccountsReceivable'], 'units': ['USD'], 'unit': 'USD', 'cash_sign': -1},
            'cf_inventory': {'concepts': ['IncreaseDecreaseInInventories'], 'units': ['USD'], 'unit': 'USD', 'cash_sign': -1},
            'cf_payables': {'concepts': ['IncreaseDecreaseInAccountsPayable'], 'units': ['USD'], 'unit': 'USD', 'cash_sign': 1},
            'cf_financing_receivables': {'concepts': ['IncreaseDecreaseInFinanceReceivables'], 'units': ['USD'], 'unit': 'USD', 'cash_sign': -1},
            'cash_equivalents': {'concepts': ['CashAndCashEquivalentsAtCarryingValue'], 'units': ['USD'], 'unit': 'USD'},
            'accounts_receivable_net': {'concepts': ['AccountsReceivableNetCurrent'], 'units': ['USD'], 'unit': 'USD'},
            'inventory': {'concepts': ['InventoryNet'], 'units': ['USD'], 'unit': 'USD'},
            'accounts_payable': {'concepts': ['AccountsPayableCurrent'], 'units': ['USD'], 'unit': 'USD'},
            'short_term_debt': {'concepts': ['DebtCurrent', 'LongTermDebtCurrent'], 'units': ['USD'], 'unit': 'USD'},
            'long_term_debt': {'concepts': ['LongTermDebtNoncurrent'], 'units': ['USD'], 'unit': 'USD'},
            'total_equity': {'concepts': ['StockholdersEquity'], 'units': ['USD'], 'unit': 'USD'},
        },
        'roles': {
            'revenue': {'concepts': ['RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet'],
                        'units': ['USD'], 'unit': 'USD', 'required': True,
                        'metric_keys': ['revenue_current', 'revenue_prior'], 'label': '年度營收'},
            'net_income': {'concepts': ['NetIncomeLoss'], 'units': ['USD'], 'unit': 'USD', 'required': True,
                           'metric_keys': ['net_income'], 'label': '年度淨利'},
            'eps': {'concepts': ['EarningsPerShareDiluted', 'EarningsPerShareBasic'],
                    'units': ['USD/shares'], 'unit': 'per_share', 'required': False,
                    'metric_keys': ['eps_current', 'eps_prior'], 'label': '年度稀釋每股盈餘'},
            'operating_cash_flow': {'concepts': ['NetCashProvidedByUsedInOperatingActivities',
                                                 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'],
                                    'units': ['USD'], 'unit': 'USD', 'required': False,
                                    'metric_keys': ['operating_cash_flow'], 'label': '年度營業現金流'},
            'capital_expenditure': {'concepts': ['PaymentsToAcquirePropertyPlantAndEquipment'],
                                    'units': ['USD'], 'unit': 'USD', 'required': False,
                                    'metric_keys': ['capital_expenditure'], 'label': '年度資本支出'},
        },
    },
}


# Reviewer-cited filing documents. Nothing is collected from these by this code: a record enters
# only through a reviewed dossier, and the path CIK binds the document to the configured issuer
# (EDGAR archive paths carry the CIK without leading zeros).
FILING_PROFILES = {
    'sec-edgar-archives-filing': {
        'adapter': 'sec_filing_document', 'source_class': 'official',
        'endpoint': 'https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}',
        'entities_config': 'config/sec-entities.json',
        'segments': {'cik': r'[1-9]\d{0,9}', 'accession': r'\d{18}', 'document': r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}'},
        'identity_binding': {'cik': ['archive_path_cik'], 'accession': ['archive_path_accession']},
        'evidence_kinds': ['event'],
    },
}


# What a source *is* and what its figures have been adjusted for are claims that need evidence in
# the code and the source notes. A per-run profile override may rebind field names; it may not
# assert provenance.
PROTECTED_PROFILE_KEYS = ('adapter', 'source_class', 'adjustment_basis', 'price_field')


def merge_profile(source_id, overrides=None):
    if source_id not in PROFILES:
        raise ValueError(f'Unknown source_id {source_id}; known sources: {sorted(PROFILES)}')
    protected = sorted(set(overrides or {}) & set(PROTECTED_PROFILE_KEYS))
    if protected:
        raise ValueError(f'Profile keys {protected} assert source provenance and cannot be overridden per run; '
                         f'change the recorded contract together with its evidence instead')
    profile = {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
               for k, v in PROFILES[source_id].items()}
    for key, value in (overrides or {}).items():
        if key not in profile:
            raise ValueError(f'Unknown profile key {key} for {source_id}')
        if isinstance(profile[key], dict) and isinstance(value, dict):
            profile[key].update(value)
        else:
            profile[key] = value
    return profile


class TaiwanInstrumentAdapter:
    """Shared issuer, market and instrument-type gate for Taiwan exchange sources."""

    def __init__(self, source_id, profile, instruments_config=None):
        self.source_id, self.profile = source_id, profile
        self.coverage_mode = profile['coverage_mode']
        config = instruments_config if instruments_config is not None else read_json(profile['instruments_config'])
        self.instruments = {row['ticker']: row for row in config['instruments']}
        self.instruments_version = config.get('config_version', 'unknown')

    def instrument(self, ticker):
        """Share class comes from explicit configuration, never from the code shape alone."""
        suffix = self.profile['ticker_suffix']
        if not ticker.endswith(suffix):
            raise ValueError(f'{self.source_id} serves {self.profile["exchange"]} tickers ending in {suffix}')
        code = ticker[:-len(suffix)]
        if not re.fullmatch(self.profile['code_shape_pattern'], code):
            raise ValueError(f'{code} does not match the documented code shape for {self.profile["exchange"]}')
        row = self.instruments.get(ticker)
        if row is None:
            raise ValueError(f'{ticker} has no verified instrument-type entry in '
                             f'{self.profile["instruments_config"]}; a four-digit code does not establish a '
                             f'common share (depositary receipts and funds share that shape). '
                             f'Configured tickers: {sorted(self.instruments)}')
        if row['exchange'] != self.profile['exchange'] or row['code'] != code:
            raise ValueError(f'{ticker} instrument configuration does not match this source market or code')
        if row['instrument_type'] not in self.profile['supported_instrument_types']:
            raise ValueError(f'{ticker} is configured as {row["instrument_type"]}, which this adapter does not '
                             f'support: {self.profile["supported_instrument_types"]}')
        return code, row

    def table(self, payloads, key):
        """The one JSON array this source serves, or a contract error. Never a substitute."""
        payload = payloads.get(key)
        if not isinstance(payload, list):
            raise SourceContractError(f'{self.source_id}: expected a JSON array, got {type(payload).__name__}')
        if not payload:
            raise SourceContractError(f'{self.source_id}: endpoint returned an empty array; no fallback data is used')
        if len(payload) > MAX_RECORDS:
            raise SourceContractError(f'{self.source_id}: response row count {len(payload)} exceeds the configured limit')
        sample = next((r for r in payload if isinstance(r, dict)), None)
        if sample is None:
            raise SourceContractError(f'{self.source_id}: response contains no JSON objects')
        binding = {}
        for field, names in self.profile['fields'].items():
            bound = bind_field(sample, names)
            if bound is None and field in self.profile['required_fields']:
                raise SourceContractError(
                    f'{self.source_id}: no field for {field!r} among {names}; observed fields: {sorted(sample)[:24]}')
            binding[field] = bound
        return payload, binding

    def issuer_record(self, ticker, payload, binding):
        """Exactly one row for the configured code, whose name passes the configured identity check."""
        code, instrument = self.instrument(ticker)
        matches = [r for r in payload if isinstance(r, dict) and str(r.get(binding['code'], '')).strip() == code]
        if len(matches) != 1:
            raise SourceContractError(f'{self.source_id}: expected exactly one record for {code}, found {len(matches)}')
        record = matches[0]
        name = str(record.get(binding['name'], '')).strip()
        if not name:
            raise SourceContractError(f'{self.source_id}: record for {code} carries no company name')
        expected_name = instrument.get('expected_name_contains')
        if expected_name and expected_name not in name:
            raise SourceContractError(f'{self.source_id}: record name {name[:40]!r} does not contain the configured '
                                      f'identity check {expected_name!r} for {ticker}')
        return code, instrument, record, name


class ExchangeQuoteAdapter(TaiwanInstrumentAdapter):
    """Taiwan exchange daily-close OpenAPI endpoints (latest session only)."""

    def requests(self, ticker):
        self.instrument(ticker)
        return [RequestSpec(key='daily', url=self.profile['endpoint'])]

    def parse(self, ticker, payloads, collected_at):
        payload, binding = self.table(payloads, 'daily')
        code, instrument, record, name = self.issuer_record(ticker, payload, binding)
        entity = Entity(entity_id=ticker, name=name, exchange=self.profile['exchange'],
                        currency=self.profile['currency'], share_class=instrument['share_class'])
        notes = [f'股份類別來自 {self.profile["instruments_config"]} 版本 {self.instruments_version}'
                 f'（{instrument["verified_by"]}），非來源欄位，也不是由代號型態推定']
        observed = collected_at
        raw_date = str(record.get(binding['date']) or '').strip() or None if binding.get('date') else None
        if binding.get('date') and raw_date is None:
            raise SourceContractError(f'{self.source_id}: record for {code} has an empty {binding["date"]!r} field; '
                                      f'the trading date is part of the recorded contract')
        if raw_date:
            session, variant = parse_source_date(raw_date, self.profile['date_formats'])
            binding['date_format'] = variant
            if start_of_day(session, TAIPEI) > observed:
                raise SourceContractError(f'{self.source_id}: session date {raw_date} starts after the observation '
                                          f'instant; the response is not internally consistent')
            # A date-only session is bounded by the end of that trading day, but never later than
            # the moment the record was actually observed.
            event_time = min(end_of_day(session, TAIPEI), observed)
            if event_time == observed:
                notes.append('交易日與觀測同日，事件時間上界收斂至實際觀測時刻')
            period_raw = raw_date
        else:
            # No trading date column: the session is only known to be at or before observation.
            event_time, period_raw = observed, None
            notes.append('來源未提供交易日欄位，事件時間以觀測可得性上界表示')
        # Neither endpoint declares a publication timestamp, so availability is all we may assert.
        shared = {'entity_id': ticker, 'publication_basis': 'observed_availability', 'published_at': observed,
                  'availability_observed_at': observed, 'event_time': event_time, 'retrieved_at': observed,
                  'source_url': self.profile['endpoint'], 'source_published_raw': None,
                  'source_period_raw': period_raw, 'request_key': 'daily'}
        record_id = f'{code}@{raw_date or observed.date().isoformat()}'
        observations = [Observation(
            evidence_id=f'{self.source_id}-{ticker}-identity'[:80], kind='identity',
            claim=f'{name}（{code}）於 {self.profile["exchange"]} 每日收盤資料中的名稱與代號；'
                  f'來源未宣告公開時間，僅能以擷取當下可得性為上界。',
            provider_record_id=record_id, **shared)]
        close = to_number(record.get(binding['close']))
        if close is None:
            notes.append('來源未提供可用收盤價（無成交或欄位為空），保留缺值，不以零或前日價替代')
        else:
            observations.append(Observation(
                evidence_id=f'{self.source_id}-{ticker}-close'[:80], kind='price', role='close',
                claim=f'{name}（{code}）最新可得收盤價 {close} {self.profile["currency"]}'
                      f'（交易日欄位：{raw_date or "未提供"}）；來源未宣告公開時間。',
                value=close, unit='per_share', currency=self.profile['currency'], period='close',
                provider_record_id=record_id, **shared))
        return ParseResult(entity=entity, observations=observations, field_binding=binding, notes=notes)


class TaiwanIncomeStatementAdapter(TaiwanInstrumentAdapter):
    """Taiwan consolidated income-statement summary: the latest filed period of one issuer.

    What the response declares, and what it does not:

    * The period is a year and a quarter number. The figures are fiscal-year-to-date
      cumulative, so the window runs from the fiscal year start to that quarter's end. The
      fiscal year end is not in the response; it comes from explicit instrument configuration,
      and an issuer without it is refused instead of being assumed to follow the calendar year.
    * Only the latest period is served. There is no prior-year window, so growth is not
      derivable here, and a cumulative EPS is never annualized into a trailing figure.
    * Monetary columns carry no declared scale. They are kept verbatim under an explicit
      `source_scale_undeclared` unit, which only supports ratios within the same record.
    * The single date is the table output date. It is not an issuer filing date, so
      publication is modeled as observed availability and the stamp is kept only as raw text.
    """

    def statement_instrument(self, ticker):
        code, row = self.instrument(ticker)
        if row.get('financial_statement_format') != self.profile['statement_format']:
            raise ValueError(f'{ticker} is not configured with financial_statement_format='
                             f'{self.profile["statement_format"]!r}; financial, insurance and other formats '
                             f'are published in different datasets and are not read by this adapter')
        if row.get('fiscal_year_end_month') not in self.profile['supported_fiscal_year_end_months']:
            raise ValueError(f'{ticker} has no supported fiscal_year_end_month in '
                             f'{self.profile["instruments_config"]} (supported: '
                             f'{self.profile["supported_fiscal_year_end_months"]}); the response does not declare '
                             f'the fiscal year, so the reporting window is not derived without it')
        return code, row

    def requests(self, ticker):
        self.statement_instrument(ticker)
        return [RequestSpec(key='income', url=self.profile['endpoint'])]

    def window(self, year_raw, quarter_raw):
        """(start, end) of the cumulative window named by the source's year and quarter."""
        if self.profile['year_calendar'] != 'roc':
            raise SourceContractError(f'{self.source_id}: undeclared year calendar {self.profile["year_calendar"]!r}')
        if not re.fullmatch(r'\d{2,3}', year_raw):
            raise SourceContractError(f'{self.source_id}: year {year_raw[:12]!r} is not a ROC year; '
                                      f'no other calendar is assumed')
        if quarter_raw not in ('1', '2', '3', '4'):
            raise SourceContractError(f'{self.source_id}: quarter {quarter_raw[:12]!r} is not one of 1-4')
        year, end_month = int(year_raw) + 1911, int(quarter_raw) * 3
        return date(year, 1, 1), date(year, end_month, calendar.monthrange(year, end_month)[1])

    def parse(self, ticker, payloads, collected_at):
        self.statement_instrument(ticker)
        payload, binding = self.table(payloads, 'income')
        code, instrument, record, name = self.issuer_record(ticker, payload, binding)
        observed = collected_at
        year_raw, quarter_raw, table_raw = (str(record.get(binding[key]) or '').strip()
                                            for key in ('year', 'quarter', 'table_date'))
        start, end = self.window(year_raw, quarter_raw)
        basis = self.profile['period_basis']
        if basis != 'cumulative_ytd':
            raise SourceContractError(f'{self.source_id}: period basis {basis!r} has no verified contract here')
        if end_of_day(end, TAIPEI) >= observed:
            raise SourceContractError(f'{self.source_id}: the reported period {year_raw}Q{quarter_raw} had not ended '
                                      f'at the observation instant; the response is not internally consistent')
        if not table_raw:
            raise SourceContractError(f'{self.source_id}: record for {code} has an empty table output date')
        table_date, variant = parse_source_date(table_raw, self.profile['date_formats'])
        if table_date <= end:
            raise SourceContractError(f'{self.source_id}: table output date {table_raw} is not after the period '
                                      f'end it reports; the period fields are not trusted')
        if start_of_day(table_date, TAIPEI) > observed:
            raise SourceContractError(f'{self.source_id}: table output date {table_raw} starts after the '
                                      f'observation instant; the response is not internally consistent')
        binding.update(table_date_format=variant, year_calendar=self.profile['year_calendar'], period_basis=basis)
        period = f'{start.year}YTD-Q{quarter_raw}'
        period_raw = f'{binding["year"]}={year_raw} {binding["quarter"]}={quarter_raw}'
        record_id = f'{code}@{year_raw}Q{quarter_raw}@{table_raw}'
        entity = Entity(entity_id=ticker, name=name, exchange=self.profile['exchange'],
                        currency=self.profile['currency'], share_class=instrument['share_class'])
        # The output date says when the table was generated, not when the issuer filed, so the
        # only publication claim made is that the record existed when it was observed.
        shared = {'entity_id': ticker, 'publication_basis': 'observed_availability', 'published_at': observed,
                  'availability_observed_at': observed, 'event_time': end_of_day(end, TAIPEI),
                  'retrieved_at': observed, 'source_url': self.profile['endpoint'],
                  'source_published_raw': f'{binding["table_date"]}={table_raw}', 'source_period_raw': period_raw,
                  'request_key': 'income', 'provider_record_id': record_id}
        window_text = f'{start.isoformat()}~{end.isoformat()} 年初至今累計'
        observations = [Observation(
            evidence_id=f'{self.source_id}-{ticker}-identity'[:80], kind='identity',
            claim=f'{name}（{code}）列於 {self.profile["exchange"]} 綜合損益表（一般業）彙總資料；'
                  f'來源僅提供出表日期，未宣告公司申報時間，僅能以擷取當下可得性為上界。', **shared)]
        notes = [f'股份類別、財報格式與會計年度結束月份來自 {self.profile["instruments_config"]} 版本 '
                 f'{self.instruments_version}（{instrument["verified_by"]}），非來源欄位',
                 # Notes reach the inspection record, so they describe the rule and never the period.
                 '期間起訖由來源的年度／季別欄位與設定的會計年度推得（年初至今累計）；來源未提供起訖日欄位',
                 '出表日期不是公司申報日，公開時間採觀測可得性上界，不支援早於擷取時點的歷史截止',
                 '來源未宣告金額級距：金額保留來源原值，僅可用於同一申報紀錄內的比率，不作跨來源金額比較',
                 '省略 revenue_growth／eps_growth：端點只提供最新一期累計數，沒有可比的前期同窗資料',
                 '省略 pe：累計基本每股盈餘不年化、不冒充 TTM',
                 '省略 free_cash_flow：此資料集不含現金流量表']
        for role, spec in self.profile['roles'].items():
            field = binding[spec['field']]
            value = to_number(record.get(field))
            if value is None:
                notes.append(f'{role}：來源欄位為空，保留缺值，不以零或其他科目替代')
                continue
            if spec['kind'] == 'per_share':
                declared = re.search(r'[（(]([^（()）]+)[）)]\s*$', field)
                if declared is None or declared[1] != '元':
                    raise SourceContractError(f'{self.source_id}: per-share field {field!r} does not declare the '
                                              f'expected unit; it is not assumed')
                unit, unit_raw, unit_text = 'per_share', declared[1], f'{declared[1]}／股'
            else:
                unit, unit_raw, unit_text = self.profile['money_unit'], None, '來源未宣告金額級距'
            observations.append(Observation(
                evidence_id=f'{self.source_id}-{ticker}-{role}'[:80], kind='financial', role=role,
                financial_metric=spec.get('metric'),
                claim=f'{name}（{code}）{spec["label"]}（來源欄位「{field}」）{value}（{unit_text}），'
                      f'期間 {window_text}（來源年度 {year_raw}、季別 {quarter_raw}）；非單季、非 TTM。',
                value=value, unit=unit, currency=self.profile['currency'], period=period, period_start=start,
                period_end=end, period_basis=basis, source_unit_raw=unit_raw, **shared))
        return ParseResult(entity=entity, observations=observations, field_binding=binding, notes=notes)


class DailyPriceSeriesAdapter:
    """Daily close series from a secondary market-data provider's chart endpoint.

    * Identity: the provider symbol, market and instrument mapping is operator configuration.
      The response's own symbol, currency, exchange code, instrument type and granularity are
      checked against it, and the binding records which facts the response returned.
    * Sessions: each row is stamped with its session open instant. The trade date is kept as
      a date, separate from any publication or observation time, and only sessions whose
      calendar day has fully ended in the exchange's latest UTC offset are used. The current
      or just-finished session is excluded instead of calling a last price a close.
    * Publication: nothing is declared, so every point is `observed_availability` and cannot
      support a research cutoff earlier than the capture.
    * Adjustment: the configured basis is carried on every point. `unknown` means no change
      over time is ever derived from the series.
    """

    def __init__(self, source_id, profile, instruments_config=None):
        self.source_id, self.profile = source_id, profile
        self.coverage_mode = profile['coverage_mode']
        config = instruments_config if instruments_config is not None else read_json(profile['instruments_config'])
        self.instruments = {row['ticker']: row for row in config['instruments']}
        self.instruments_version = config.get('config_version', 'unknown')

    def instrument(self, ticker):
        row = self.instruments.get(ticker)
        if row is None:
            raise ValueError(f'{ticker} has no provider-symbol mapping in {self.profile["instruments_config"]}; '
                             f'configured: {sorted(self.instruments)}')
        mapping = (row.get('provider_symbols') or {}).get(self.source_id)
        if not mapping:
            raise ValueError(f'{ticker} has no mapping for provider {self.source_id}; a symbol is never guessed '
                             f'from the ticker or borrowed from another provider')
        if not re.fullmatch(r'[A-Z][A-Z.\-]{0,9}', mapping.get('symbol') or ''):
            raise ValueError(f'{ticker}: configured provider symbol is not a plain equity symbol')
        if row.get('instrument_type') != 'common_share':
            raise ValueError(f'{ticker} is configured as {row.get("instrument_type")}; only common shares are supported')
        return row, mapping

    def requests(self, ticker):
        _, mapping = self.instrument(ticker)
        return [RequestSpec(key='chart', url=self.profile['endpoint'].format(symbol=mapping['symbol']))]

    def fail(self, message):
        raise SourceContractError(f'{self.source_id}: {message}')

    def result(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('chart'), dict):
            self.fail(f'expected a JSON object with a chart member, got {type(payload).__name__}')
        chart = payload['chart']
        if chart.get('error') is not None:
            self.fail('the provider returned an error object; no fallback data is used')
        results = chart.get('result')
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            self.fail('expected exactly one chart result')
        return results[0]

    def trade_day(self, stamp):
        """Session date of an open-instant stamp, without a time zone database.

        US equity sessions open mid-day in UTC, so the date is the same under both UTC offsets the
        exchange uses. A stamp for which they disagree is not a regular session open."""
        if isinstance(stamp, bool) or not isinstance(stamp, int):
            self.fail('a session timestamp is not an integer epoch second')
        try:
            days = {datetime.fromtimestamp(stamp, timezone(timedelta(hours=offset))).date() for offset in (-4, -5)}
        except (OverflowError, OSError, ValueError):
            self.fail('a session timestamp is outside the representable range')
        if len(days) != 1:
            self.fail('a session timestamp does not identify one exchange calendar day')
        return days.pop()

    def parse(self, ticker, payloads, collected_at):
        row, mapping = self.instrument(ticker)
        result = self.result(payloads.get('chart'))
        meta = result.get('meta')
        if not isinstance(meta, dict):
            self.fail('chart result carries no meta object')
        observed = collected_at
        # Facts the response itself returned, each checked against operator configuration.
        returned = {'symbol': (meta.get('symbol'), [mapping['symbol']]),
                    'currency': (meta.get('currency'), [row['currency']]),
                    'exchange_code': (meta.get('exchangeName'), mapping['expected_exchange_codes']),
                    'instrument_type': (meta.get('instrumentType'), [mapping['expected_instrument_type']]),
                    'granularity': (meta.get('dataGranularity'), [self.profile['granularity']])}
        for field, (value, accepted) in returned.items():
            if value not in accepted:
                self.fail(f'response {field} {str(value)[:24]!r} is not the configured {accepted} for {ticker}; '
                          f'the series is not attributed to this instrument')
        name = str(meta.get('longName') or '').strip()
        expected = mapping.get('expected_name_contains')
        if not name or (expected and expected.casefold() not in name.casefold()):
            self.fail(f'response longName does not contain the configured identity check {expected!r}')
        stamps = result.get('timestamp')
        quotes = (result.get('indicators') or {}).get('quote')
        if not isinstance(stamps, list) or not stamps:
            self.fail('chart result carries no session timestamps; no fallback data is used')
        if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict):
            self.fail('expected exactly one quote indicator block')
        closes = quotes[0].get(self.profile['price_field'])
        if not isinstance(closes, list) or len(closes) != len(stamps):
            self.fail(f'{self.profile["price_field"]!r} series is missing or not aligned with the timestamps')
        if len(stamps) > self.profile['max_rows']:
            self.fail(f'row count {len(stamps)} exceeds the configured limit {self.profile["max_rows"]}')
        sessions, skipped_null, excluded_open = [], 0, 0
        previous_stamp, previous_day = None, None
        for stamp, close in zip(stamps, closes):
            day = self.trade_day(stamp)
            if previous_stamp is not None and stamp <= previous_stamp:
                self.fail('session timestamps are duplicated or not in ascending order')
            if day == previous_day:
                self.fail('two rows claim the same trading day; conflicting days are not reconciled')
            previous_stamp, previous_day = stamp, day
            if start_of_day(day, US_DATE_UPPER_BOUND) > observed:
                self.fail('a row is dated after the observation instant; the response is not internally consistent')
            if (observed.astimezone(US_DATE_UPPER_BOUND).date() - day).days > self.profile['max_history_days']:
                self.fail(f'a row is older than the configured history bound of {self.profile["max_history_days"]} days')
            if end_of_day(day, US_DATE_UPPER_BOUND) > observed:
                # The session's calendar day has not ended: its last price is not a completed close.
                excluded_open += 1
                continue
            if close is None:
                skipped_null += 1
                continue
            if isinstance(close, bool) or not isinstance(close, (int, float)) or close != close or close <= 0 \
                    or close in (float('inf'),):
                self.fail('a close value is not a positive finite number')
            sessions.append((day, stamp, float(close)))
        if not sessions:
            self.fail('no completed session with a close is available; the current session is never used')
        latest = sessions[-1]
        if (observed.astimezone(US_DATE_UPPER_BOUND).date() - latest[0]).days > self.profile['max_staleness_days']:
            self.fail(f'the latest completed session is more than {self.profile["max_staleness_days"]} days before '
                      f'the observation; a stale series is not presented as the latest close')
        splits = ((result.get('events') or {}).get('splits') or {})
        binding = {'symbol': 'chart.result[0].meta.symbol', 'currency': 'chart.result[0].meta.currency',
                   'exchange_code': 'chart.result[0].meta.exchangeName',
                   'instrument_type': 'chart.result[0].meta.instrumentType',
                   'granularity': 'chart.result[0].meta.dataGranularity', 'name': 'chart.result[0].meta.longName',
                   'session': 'chart.result[0].timestamp',
                   'close': f'chart.result[0].indicators.quote[0].{self.profile["price_field"]}',
                   'adjustment_basis': self.profile['adjustment_basis'],
                   'identity_returned_by_response': 'symbol,currency,exchange_code,instrument_type,granularity,name',
                   'identity_from_operator_config': 'ticker_to_symbol,exchange,share_class',
                   'split_events_declared': str(len(splits)) if isinstance(splits, dict) else None}
        entity = Entity(entity_id=ticker, name=name[:80], exchange=row['exchange'], currency=meta['currency'],
                        share_class=row['share_class'])
        url = self.profile['endpoint'].format(symbol=mapping['symbol'])
        basis = self.profile['adjustment_basis']

        def point(role, session, label):
            day, stamp, close = session
            return Observation(
                evidence_id=f'{self.source_id}-{ticker}-{role}'[:80], entity_id=ticker, kind='price', role=role,
                claim=f'{name}（{mapping["symbol"]}）{label}：交易日 {day.isoformat()} 收盤 {close} {meta["currency"]}／股，'
                      f'取自次級市場資料供應者，非交易所或申報紀錄；價格調整基礎 {basis}；'
                      f'來源未宣告公開時間，僅能以擷取當下可得性為上界。',
                value=close, unit='per_share', currency=meta['currency'],
                period='close' if role == 'close' else day.isoformat(), period_start=day, period_end=day,
                period_basis='trading_day', price_adjustment=basis,
                provider_record_id=f'{mapping["symbol"]}@{stamp}', request_key='chart', source_url=url,
                source_published_raw=None, source_period_raw=f'timestamp={stamp}', source_unit_raw=meta['currency'],
                publication_basis='observed_availability', published_at=observed,
                availability_observed_at=observed, event_time=end_of_day(day, US_DATE_UPPER_BOUND),
                retrieved_at=observed)

        observations = [Observation(
            evidence_id=f'{self.source_id}-{ticker}-identity'[:80], entity_id=ticker, kind='identity',
            claim=f'{name}：次級市場資料供應者回應自帶代號 {meta["symbol"]}、幣別 {meta["currency"]}、交易所代碼 '
                  f'{meta["exchangeName"]}、商品類型 {meta["instrumentType"]}，與操作人設定相符；'
                  f'代號對應、交易市場 {row["exchange"]} 與股份類別來自操作人設定，不是來源欄位。',
            provider_record_id=f'{mapping["symbol"]}@meta', request_key='chart', source_url=url,
            publication_basis='observed_availability', published_at=observed, availability_observed_at=observed,
            event_time=end_of_day(latest[0], US_DATE_UPPER_BOUND), retrieved_at=observed),
            point('close', latest, '最新已完成交易日')]
        notes = [f'代號對應、交易市場與股份類別來自 {self.profile["instruments_config"]} 版本 {self.instruments_version}'
                 f'（{row.get("verified_by", "unspecified")}），不是來源欄位；回應自帶的代號、幣別、交易所代碼與商品類型已比對',
                 '來源為次級市場資料供應者，不是交易所或申報紀錄；證據標為 secondary',
                 '交易日取自每列時間戳，與公開／擷取時間分開保存；來源未宣告公開時間，不支援早於擷取時點的歷史截止',
                 f'尚未結束的交易日一律排除（本次排除 {excluded_open} 列），不把盤中或剛收盤的最後價格當成收盤價',
                 f'價格調整基礎：{basis}；僅為價格，不含股利，不是總報酬']
        if basis == 'unknown':
            notes.append('省略 price_change_3m／6m／12m：來源未文件化收盤序列的分割／股利調整基礎，不推定跨期可比')
        if skipped_null:
            notes.append(f'{skipped_null} 個已完成交易日的收盤為空，保留缺值，不以前後日補值')
        for months in self.profile['anchor_months']:
            target = months_before(latest[0], months)
            candidates = [s for s in sessions if s[0] <= target]
            if not candidates or (target - candidates[-1][0]).days > self.profile['anchor_tolerance_days']:
                notes.append(f'close_anchor_{months}m：目標日前 {self.profile["anchor_tolerance_days"]} 日內沒有已完成'
                             f'交易日的收盤，保留缺值，不以較遠日期替代')
                continue
            observations.append(point(f'close_anchor_{months}m', candidates[-1], f'{months} 個曆月前基準交易日'))
        return ParseResult(entity=entity, observations=observations, field_binding=binding, notes=notes)


# Response fields that carry the issuer identity in every company-concept payload.
SEC_IDENTITY_FIELDS = {'cik': 'cik', 'entity_name': 'entityName'}


class SecConceptAdapter:
    """SEC EDGAR XBRL company-concept facts for an operator-configured issuer."""

    def __init__(self, source_id, profile, entities_config=None):
        self.source_id, self.profile = source_id, profile
        self.coverage_mode = profile['coverage_mode']
        config = entities_config if entities_config is not None else read_json(profile['entities_config'])
        self.entities = {row['ticker']: row for row in config['entities']}
        self.config_version = config.get('config_version', 'unknown')

    def entity_config(self, ticker):
        if ticker not in self.entities:
            raise ValueError(f'{ticker} is not configured in the SEC entity map; configured: {sorted(self.entities)}')
        return self.entities[ticker]

    def requests(self, ticker):
        cik = self.entity_config(ticker)['cik']
        if not re.fullmatch(r'\d{10}', cik):
            raise ValueError('Configured CIK must be ten digits')
        specs = []
        roles = self.profile['quarterly_roles'] if self.profile.get('series') == 'quarterly' else self.profile['roles']
        for role, spec in roles.items():
            for concept in spec['concepts']:
                specs.append(RequestSpec(key=f'{role}:{concept}', required=False,
                                         url=self.profile['endpoint'].format(cik=cik, concept=concept)))
        return specs

    def annual_facts(self, payload, spec, source_url):
        units = payload.get('units')
        if not isinstance(units, dict) or not units:
            raise SourceContractError(f'{self.source_id}: concept response carries no units object')
        unit_key = bind_field(units, spec['units'])
        if unit_key is None:
            raise SourceContractError(f'{self.source_id}: none of the units {spec["units"]} present; '
                                      f'observed units: {sorted(units)[:12]}')
        rows = units[unit_key]
        if not isinstance(rows, list):
            raise SourceContractError(f'{self.source_id}: unit {unit_key} does not hold a list of facts')
        selected = {}
        for fact in rows:
            if not isinstance(fact, dict):
                continue
            form = str(fact.get('form', '')).split('/')[0]
            if fact.get('fp') != 'FY' or form not in self.profile['annual_forms']:
                continue
            start, end, filed, accn = fact.get('start'), fact.get('end'), fact.get('filed'), fact.get('accn')
            value = to_number(fact.get('val'))
            if not (start and end and filed and accn) or value is None:
                continue
            start_date, end_date, filed_date = (parse_source_date(start, ['iso'])[0], parse_source_date(end, ['iso'])[0],
                                                parse_source_date(filed, ['iso'])[0])
            span = (end_date - start_date).days
            if not self.profile['min_period_days'] <= span <= self.profile['max_period_days']:
                continue
            if filed_date < end_date:
                raise SourceContractError(f'{self.source_id}: filing date precedes the period end it reports')
            row = {'value': value, 'start': start, 'end': end, 'filed': filed, 'accn': accn,
                   'start_date': start_date,
                   'fy': fact.get('fy'), 'fp': fact.get('fp'), 'form': fact.get('form'), 'frame': fact.get('frame'),
                   'end_date': end_date, 'filed_date': filed_date, 'unit_key': unit_key, 'source_url': source_url}
            current = selected.get(end)
            # Keep the most recently filed statement of a period: the authoritative figure, and a
            # conservative (later) publication bound than the period's first disclosure.
            if current is None or (row['filed_date'], row['accn']) > (current['filed_date'], current['accn']):
                selected[end] = row
        return sorted(selected.values(), key=lambda r: r['end_date'], reverse=True)

    QUARTER_BASES = (('single_quarter', 80, 100), ('cumulative_ytd', 170, 290), ('fiscal_year', 350, 380))

    def quarterly_facts(self, payload, spec):
        """Retain each filing vintage. Selecting the latest here destroys PIT evidence."""
        units = payload.get('units')
        unit_key = bind_field(units, spec['units']) if isinstance(units, dict) else None
        if unit_key is None or not isinstance(units[unit_key], list):
            return []
        selected = {}
        for fact in units[unit_key]:
            if not isinstance(fact, dict) or str(fact.get('form', '')).split('/')[0] not in ('10-Q', '10-K'):
                continue
            end, filed, accn, value = fact.get('end'), fact.get('filed'), fact.get('accn'), to_number(fact.get('val'))
            if not (end and filed and accn) or value is None:
                continue
            end_date, filed_date = parse_source_date(end, ['iso'])[0], parse_source_date(filed, ['iso'])[0]
            if filed_date < end_date:
                raise SourceContractError(f'{self.source_id}: filing date precedes the period end it reports')
            start_date, basis = None, 'instant'
            if fact.get('start'):
                start_date = parse_source_date(fact['start'], ['iso'])[0]
                span = (end_date - start_date).days
                basis = next((name for name, low, high in self.QUARTER_BASES if low <= span <= high), None)
                if basis is None:
                    continue
            row = dict(value=value, start_date=start_date, end_date=end_date, filed_date=filed_date, accn=accn,
                       form=fact.get('form'), fy=fact.get('fy'), fp=fact.get('fp'), basis=basis, unit_key=unit_key)
            key = (start_date, end_date, filed_date, accn)
            if key in selected and selected[key]['value'] != value:
                raise SourceContractError(f'{self.source_id}: conflicting facts within one filing context')
            if key not in selected:
                selected[key] = row
        return list(selected.values())

    def parse_quarterly(self, ticker, payloads, collected_at):
        config = self.entity_config(ticker)
        notes, observations, identity_source, entity_name = [], [], None, None
        for role, spec in self.profile['quarterly_roles'].items():
            rows, used, payload = [], None, None
            for concept in spec['concepts']:
                candidate = payloads.get(f'{role}:{concept}')
                if not isinstance(candidate, dict):
                    continue
                found = self.quarterly_facts(candidate, spec)
                # A concept change is never spliced: the concept with the newest window wins.
                if found and (not rows or max(r['end_date'] for r in found) > max(r['end_date'] for r in rows)):
                    rows, used, payload = found, concept, candidate
            if not rows:
                if spec.get('required'):
                    raise SourceContractError(f'{self.source_id}: no quarterly facts for required role {role}')
                notes.append(f'{role}: 無 10-Q/10-K 事實，保留缺值（not_found）')
                continue
            if payload.get('cik') is None or int(payload['cik']) != int(config['cik']):
                raise SourceContractError(f'{self.source_id}: response CIK does not match configured issuer for {ticker}')
            name = str(payload.get('entityName', '')).strip()
            expected = config.get('expected_entity_name_contains')
            if expected and expected.casefold() not in name.casefold():
                raise SourceContractError(f'{self.source_id}: response entityName does not match {expected!r}')
            entity_name = entity_name or name
            if spec['unit'] in ('USD',) and rows[0]['unit_key'] != config['currency']:
                raise SourceContractError(f'{self.source_id}: {role} is not reported in {config["currency"]}')
            ends = sorted({r['end_date'] for r in rows}, reverse=True)[:self.profile['max_quarters']]
            url = self.profile['endpoint'].format(cik=config['cik'], concept=used)
            for row in sorted((r for r in rows if r['end_date'] in ends), key=lambda r: (r['end_date'], r['basis'])):
                tag = {'single_quarter': 'q', 'cumulative_ytd': 'ytd', 'fiscal_year': 'fy', 'instant': 'i'}[row['basis']]
                variants = [r for r in rows if (r['start_date'], r['end_date']) == (row['start_date'], row['end_date'])]
                suffix = f'-v{row["filed_date"]:%Y%m%d}-{row["accn"].replace("-", "")[-6:]}' if len(variants) > 1 else ''
                identifier = f'sec-{ticker}-{role}-{row["end_date"].isoformat()}-{tag}'
                sign = spec.get('cash_sign', 1)
                observations.append(Observation(
                    evidence_id=identifier[:80-len(suffix)] + suffix, entity_id=ticker,
                    kind='financial', financial_metric=role, accounting_basis='gaap',
                    value=row['value'] * sign, unit=spec['unit'], currency=config['currency'] if spec['unit'] != 'shares' else None,
                    period=f'{row["fy"]}{row["fp"]}' if row['fy'] and row['fp'] else None,
                    period_start=row['start_date'] or row['end_date'], period_end=row['end_date'], period_basis=row['basis'],
                    claim=f'us-gaap:{used} {row["value"]} {row["unit_key"]}，期間 '
                          f'{row["start_date"] or ""}~{row["end_date"]}（{row["basis"]}），來自 {row["form"]} {row["accn"]}，'
                          f'申報日 {row["filed_date"]}。' + (f' 現金流符號轉換 × {sign}（非餘額差額）。' if 'cash_sign' in spec else ''),
                    provider_record_id=f'{row["accn"]}:{used}:{row["start_date"]}:{row["end_date"]}'[:120],
                    request_key=f'{role}:{used}', source_url=url, source_published_raw=row['filed_date'].isoformat(),
                    source_period_raw=f'{row["start_date"]}..{row["end_date"]} fy={row["fy"]} fp={row["fp"]}'[:120],
                    source_unit_raw=row['unit_key'], publication_basis='source_declared_date',
                    published_at=end_of_day(row['filed_date'], US_DATE_UPPER_BOUND),
                    event_time=end_of_day(row['end_date'], US_DATE_UPPER_BOUND), retrieved_at=collected_at))
                if identity_source is None or row['filed_date'] > identity_source['filed_date']:
                    identity_source = {**row, 'url': url, 'request_key': f'{role}:{used}'}
        if identity_source is None or not entity_name:
            raise SourceContractError(f'{self.source_id}: no filing established the issuer identity')
        entity = Entity(entity_id=ticker, name=entity_name[:80], exchange=config['exchange'],
                        currency=config['currency'], share_class=config['share_class'])
        stamp = end_of_day(identity_source['filed_date'], US_DATE_UPPER_BOUND)
        observations.insert(0, Observation(
            evidence_id=f'sec-{ticker}-identity'[:80], entity_id=ticker, kind='identity',
            claim=f'{entity_name}（CIK {int(config["cik"])}）取自 companyconcept 回應的 entityName 與 cik 欄位。',
            provider_record_id=f'{identity_source["accn"]}:response.entityName', request_key=identity_source['request_key'],
            source_url=identity_source['url'], source_published_raw=identity_source['filed_date'].isoformat(),
            publication_basis='source_declared_date', published_at=stamp, event_time=stamp, retrieved_at=collected_at))
        notes.append('季度序列：單季、年初至今、年度與資產負債時點分開保存；單季推導由分析層以同一申報口徑計算')
        return ParseResult(entity=entity, observations=observations, field_binding=dict(SEC_IDENTITY_FIELDS), notes=notes)

    def parse(self, ticker, payloads, collected_at):
        if self.profile.get('series') == 'quarterly':
            return self.parse_quarterly(ticker, payloads, collected_at)
        config = self.entity_config(ticker)
        notes, observations, identity_source = [], [], None
        entity_name = None
        for role, spec in self.profile['roles'].items():
            facts, used_concept, payload = [], None, None
            for concept in spec['concepts']:
                candidate = payloads.get(f'{role}:{concept}')
                if candidate is None:
                    continue
                if not isinstance(candidate, dict):
                    raise SourceContractError(f'{self.source_id}: {role}:{concept} response is not a JSON object')
                found = self.annual_facts(candidate, spec, self.profile['endpoint'].format(
                    cik=config['cik'], concept=concept))
                if found:
                    # Revenue taxonomy can change between filings. Choose the newest
                    # complete concept series, keeping configured priority for ties;
                    # never splice current/prior values from different concepts.
                    if not facts or found[0]['end_date'] > facts[0]['end_date']:
                        facts, used_concept, payload = found, concept, candidate
                    if role != 'revenue':
                        break
                    continue
                notes.append(f'{role}:{concept} 無符合年度條件的事實')
            if not facts:
                if spec.get('required'):
                    raise SourceContractError(
                        f'{self.source_id}: no annual {self.profile["annual_forms"]} facts for required role {role} '
                        f'among concepts {spec["concepts"]}')
                notes.append(f'{role} 無可用年度資料，保留缺值')
                continue
            name = str(payload.get('entityName', '')).strip()
            cik_value = payload.get('cik')
            if cik_value is None or int(cik_value) != int(config['cik']):
                raise SourceContractError(f'{self.source_id}: response CIK {cik_value} does not match configured '
                                          f'{config["cik"]} for {ticker}')
            expected = config.get('expected_entity_name_contains')
            if expected and expected.casefold() not in name.casefold():
                raise SourceContractError(f'{self.source_id}: response entityName {name[:60]!r} does not contain the '
                                          f'configured identity check {expected!r}')
            entity_name = entity_name or name
            if facts[0]['unit_key'].split('/')[0] != config['currency']:
                raise SourceContractError(f'{self.source_id}: {role} is reported in {facts[0]["unit_key"]}, which is '
                                          f'not the configured currency {config["currency"]}')
            for index, metric_key in enumerate(spec['metric_keys']):
                if index >= len(facts):
                    notes.append(f'{role} 僅有 {len(facts)} 個年度期間，{metric_key} 保留缺值')
                    break
                fact = facts[index]
                # Adjacent means the prior window ends the day before the current one starts. Two
                # windows whose end years merely differ by one (a changed fiscal year end, a
                # skipped year) are not a comparable prior period.
                if index == 1 and fact['end_date'] + timedelta(days=1) != facts[0]['start_date']:
                    notes.append(f'{role} 的前期與本期期間起訖不相鄰，{metric_key} 保留缺值')
                    break
                published_at = end_of_day(fact['filed_date'], US_DATE_UPPER_BOUND)
                observations.append(Observation(
                    evidence_id=f'sec-{ticker}-{metric_key}'[:80], entity_id=ticker, kind='financial',
                    role=metric_key, value=fact['value'], unit=spec['unit'], currency=config['currency'],
                    period=f'FY{fact["end_date"].year}', period_start=fact['start_date'],
                    period_end=fact['end_date'], period_basis='fiscal_year',
                    claim=f'{spec["label"]}（us-gaap:{used_concept}）{fact["value"]} {fact["unit_key"]}，'
                          f'期間 {fact["start"]}~{fact["end"]}，來自 {fact["form"]} {fact["accn"]}，申報日 {fact["filed"]}。',
                    provider_record_id=f'{fact["accn"]}:{used_concept}:{fact["end"]}',
                    request_key=f'{role}:{used_concept}',
                    source_url=fact['source_url'], source_published_raw=fact['filed'],
                    source_period_raw=f'{fact["start"]}..{fact["end"]} fy={fact["fy"]} fp={fact["fp"]} '
                                      f'frame={fact["frame"]}'[:120],
                    source_unit_raw=fact['unit_key'], publication_basis='source_declared_date',
                    published_at=published_at, event_time=end_of_day(fact['end_date'], US_DATE_UPPER_BOUND),
                    retrieved_at=collected_at))
                if identity_source is None or fact['filed_date'] > identity_source['filed_date']:
                    identity_source = {**fact, 'name': name, 'request_key': f'{role}:{used_concept}'}
        if identity_source is None or not entity_name:
            raise SourceContractError(f'{self.source_id}: no filing established the issuer identity')
        entity = Entity(entity_id=ticker, name=entity_name[:80], exchange=config['exchange'],
                        currency=config['currency'], share_class=config['share_class'])
        observations.insert(0, Observation(
            evidence_id=f'sec-{ticker}-identity'[:80], entity_id=ticker, kind='identity',
            claim=f'{entity_name}（CIK {int(config["cik"])}）取自 companyconcept 回應的 entityName 與 cik 欄位；'
                  f'同一回應的事實來自 {identity_source["form"]} {identity_source["accn"]}（申報日 '
                  f'{identity_source["filed"]}）。交易市場與幣別取自操作人維護的 {self.profile["entities_config"]}。',
            provider_record_id=f'{identity_source["accn"]}:response.entityName',
            request_key=identity_source['request_key'],
            source_url=identity_source['source_url'], source_published_raw=identity_source['filed'],
            publication_basis='source_declared_date',
            published_at=end_of_day(identity_source['filed_date'], US_DATE_UPPER_BOUND),
            event_time=end_of_day(identity_source['filed_date'], US_DATE_UPPER_BOUND),
            retrieved_at=collected_at))
        notes.append(f'交易市場／幣別／股份類別來自設定檔版本 {self.config_version}，非 SEC 回應欄位')
        return ParseResult(entity=entity, observations=observations,
                           field_binding=dict(SEC_IDENTITY_FIELDS), notes=notes)


ADAPTERS = {'exchange_quote': ExchangeQuoteAdapter, 'daily_price_series': DailyPriceSeriesAdapter, 'tw_income_statement': TaiwanIncomeStatementAdapter,
            'sec_concept': SecConceptAdapter}


def build_adapter(source_id, overrides=None, **kwargs):
    profile = merge_profile(source_id, overrides)
    return ADAPTERS[profile['adapter']](source_id, profile, **kwargs)
