"""Read-only checks for a source activation package.

An activation package is a review-ready proposal prepared by an assistant: the exact endpoint
and field binding, the identity/time/licence findings, and the narrow scope it recommends. It
grants nothing. A registry update needs a recorded source review on one of two bases:

- a human attestation: human-checked quoted terms evidence and a named human sign-off; or
- an assistant source review made under the user's authorization: a `source_review` with an
  official https URL and quoted excerpt for every required aspect, limited to internal
  (personal, non-distributed) use. It is never labelled or accepted as a human sign-off.

Packages written before assistant review was authorized say `awaiting_human_attestation` /
`ready_for_human_review`; they stay readable and mean "awaiting source review".

This module never writes the registry. It answers two questions:

- does the package still describe the code, the instrument configuration and the registry?
- does a candidate registry open exactly the packaged scope, on a valid basis, and nothing more?

Two target kinds are understood. A Taiwan target (`code` + `instrument_config`) is bound to an
exchange dataset profile and its field names. A SEC target (`cik` + `entity_config`) is bound to
the operator-maintained SEC entity map and to the company-concept profile: CIK, entity-name
check, market, currency, share class, roles, concepts, units and the exact request URLs. A
package never mixes the two, and every mismatch blocks.
"""
import os
import re
from pathlib import Path
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .licensing import LicenseRegistry, PERMISSIONS, Scope
from .models import Model
from .sources import PROFILES, FILING_PROFILES, SEC_IDENTITY_FIELDS
from .reviewed_collect import REVIEWED_PROFILES
from .storage import read_json

# Labels that identify an assistant or a test fixture. A candidate whose human attestation carries
# one of them is rejected: an assistant's work is recorded as `checked_by=assistant` and in
# `source_review`, never as the human one.
NON_HUMAN_MARKERS = ('codex', 'claude', 'assistant', 'chatgpt', 'fixture', 'synthetic',
                     '助手', '助理', '機器人')
# Fields of a registry entry that an activation may never alter.
FROZEN_FIELDS = ('source_id', 'publisher', 'source_class', 'endpoint_scope', 'terms_url')
# `ready_for_human_review` is the historical spelling of the same recommendation.
READY = ('ready_for_source_review', 'ready_for_human_review')
SEC_ADAPTER = 'sec_concept'
CIK = re.compile(r'\d{10}')
# Where each target kind is configured when the operator names no file.
DEFAULT_CONFIG = {'tw_instrument': 'config/tw-instruments.json', 'sec_entity': 'config/sec-entities.json'}


class Target(Model):
    ticker: str
    code: str
    exchange: Literal['TWSE', 'TPEx']
    currency: Literal['TWD']
    share_class: str
    instrument_config: str


class SecTarget(Model):
    """A US issuer identified by its SEC CIK, as configured in the SEC entity map."""
    ticker: str = Field(pattern=r'^[A-Z0-9.\-]+$', max_length=24)
    cik: str = Field(pattern=r'^\d{10}$')
    expected_entity_name_contains: str = Field(min_length=1, max_length=80, pattern=r'\S')
    exchange: Literal['NASDAQ', 'NYSE']
    currency: Literal['USD']
    share_class: str
    entity_config: str


class SecRole(Model):
    concepts: list[str] = Field(min_length=1)
    units: list[str] = Field(min_length=1)
    unit: str
    metric_keys: list[str] = Field(min_length=1)
    required: bool


class SecBinding(Model):
    """The company-concept contract a SEC item claims: compared verbatim with the source profile."""
    taxonomy: Literal['us-gaap']
    annual_forms: list[str] = Field(min_length=1)
    min_period_days: int
    max_period_days: int
    roles: dict[str, SecRole] = Field(min_length=1)
    # Every URL the collector would request for this target, in request order.
    request_urls: list[str] = Field(min_length=1)


class ActivationItem(Model):
    source_id: str
    recommendation: Literal['ready_for_source_review', 'ready_for_human_review', 'not_recommended_yet']
    endpoint: str
    dataset_page: str
    dataset_resource: str
    license_name: str
    license_url: str
    field_binding: dict[str, list[str]]
    # Required for a SEC target, forbidden for a Taiwan target.
    sec_binding: SecBinding | None = None
    evidence_produced: list[str]
    retained_normalized_fields: list[str]
    not_retained: list[str]
    identity_findings: list[str]
    time_findings: list[str]
    license_findings: list[str]
    proposed_scope: Scope
    proposed_attribution: str = Field(max_length=200)
    report_effect: str
    # What the reviewer (human, or assistant under authorization) must establish. `human_checklist`
    # is the historical field name; both are read.
    human_checklist: list[str] = Field(default_factory=list)
    review_checklist: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def narrow_scope_only(self):
        scope = self.proposed_scope
        if scope.pdf_share_allowed or scope.store_response_hash or scope.store_raw_response:
            raise ValueError(f'{self.source_id}: a package may only propose internal use of normalized evidence; '
                             f'redistribution, response hashes and raw responses stay closed')
        if self.recommendation == 'not_recommended_yet' and scope.internal_use_allowed:
            raise ValueError(f'{self.source_id}: an item that is not recommended cannot propose any permission')
        if self.recommendation in READY and not (self.human_checklist or self.review_checklist):
            raise ValueError(f'{self.source_id}: a recommended item must list what the review has to establish')
        return self

    def checklist(self):
        return [*self.review_checklist, *self.human_checklist]


class ActivationPackage(Model):
    schema_version: Literal['1'] = '1'
    package_id: str = Field(pattern=r'^[a-z0-9][a-z0-9.\-]{2,63}$')
    prepared_at: AwareDatetime
    prepared_by: str
    prepared_by_kind: Literal['assistant']
    status: Literal['awaiting_source_review', 'awaiting_human_attestation']
    registry_baseline_version: str
    target: Target | SecTarget
    items: list[ActivationItem] = Field(min_length=1, max_length=8)
    # The package itself never carries an attestation or a review; those are recorded in the registry.
    human_attestation: None = None
    source_review: None = None
    notes: list[str] = Field(default_factory=list)


def looks_non_human(text):
    lowered = (text or '').lower()
    return any(marker in lowered for marker in NON_HUMAN_MARKERS)


def target_kind(target):
    return 'sec_entity' if isinstance(target, SecTarget) else 'tw_instrument'


def tw_target_drift(target, instruments):
    rows = instruments.get('instruments', []) if isinstance(instruments, dict) else []
    entry = next((row for row in rows if isinstance(row, dict) and row.get('ticker') == target.ticker), None)
    if entry is None:
        return [f'{target.ticker} 不在 {target.instrument_config}；未設定的標的不得啟用']
    return [f'標的設定 {key}={entry.get(key)!r} 與審查包 {getattr(target, key)!r} 不一致'
            for key in ('code', 'exchange', 'share_class') if entry.get(key) != getattr(target, key)]


def sec_target_drift(target, config):
    """The package target against the SEC entity map: every identity field must agree exactly."""
    rows = config.get('entities') if isinstance(config, dict) else None
    if not isinstance(rows, list):
        return [f'設定檔不是 SEC 實體設定（缺少 entities 清單）；SEC 標的只能以 {target.entity_config} 核對']
    matches = [row for row in rows if isinstance(row, dict) and row.get('ticker') == target.ticker]
    if not matches:
        return [f'{target.ticker} 不在 {target.entity_config}；未設定的標的不得啟用']
    if len(matches) > 1:
        return [f'{target.ticker} 在 SEC 實體設定中出現 {len(matches)} 次；身份不明確，不得啟用']
    entry, problems = matches[0], []
    cik = entry.get('cik')
    if not isinstance(cik, str) or not CIK.fullmatch(cik):
        problems.append(f'SEC 實體設定的 CIK {cik!r} 不是十位數字字串')
    name = entry.get('expected_entity_name_contains')
    if not isinstance(name, str) or not name.strip():
        problems.append('SEC 實體設定缺少 expected_entity_name_contains；沒有名稱綁定的標的不得啟用')
    for key in ('cik', 'expected_entity_name_contains', 'exchange', 'currency', 'share_class'):
        if entry.get(key) != getattr(target, key):
            problems.append(f'標的設定 {key}={entry.get(key)!r} 與審查包 {getattr(target, key)!r} 不一致')
    return problems


def sec_item_drift(item, profile, target):
    """A SEC item against the company-concept profile: roles, concepts, units, fields and URLs."""
    sid = item.source_id
    if profile.get('adapter') != SEC_ADAPTER or not isinstance(profile.get('roles'), dict):
        return [f'{sid}：不是 SEC company-concept 來源，與 SEC 標的不相容']
    problems, binding = [], item.sec_binding
    if profile.get('entities_config') != target.entity_config:
        problems.append(f'{sid}：審查包的 entity_config {target.entity_config!r} 不是來源契約使用的設定檔')
    if '{cik}' not in profile['endpoint'] or '{concept}' not in profile['endpoint']:
        problems.append(f'{sid}：來源契約端點缺少 CIK／concept 參數，無法綁定標的')
    if item.field_binding != {key: [name] for key, name in SEC_IDENTITY_FIELDS.items()}:
        problems.append(f'{sid}：欄位對應與來源契約不同')
    if binding is None:
        return problems + [f'{sid}：SEC 項目必須提供 sec_binding（roles／concepts／units／request_urls）']
    if f'/{binding.taxonomy}/' not in profile['endpoint']:
        problems.append(f'{sid}：taxonomy {binding.taxonomy} 與來源契約端點不符')
    for key in ('annual_forms', 'min_period_days', 'max_period_days'):
        if getattr(binding, key) != profile.get(key):
            problems.append(f'{sid}：{key} 與來源契約不同')
    roles = profile['roles']
    if set(binding.roles) != set(roles):
        problems.append(f'{sid}：roles {sorted(binding.roles)} 與來源契約 {sorted(roles)} 不同')
    for role in sorted(set(binding.roles) & set(roles)):
        claimed, spec = binding.roles[role], roles[role]
        # Concept and unit order is the fallback priority, so it is compared as a list.
        if claimed.concepts != list(spec['concepts']):
            problems.append(f'{sid}：{role} 的 concept 與來源契約不同')
        if claimed.units != list(spec['units']) or claimed.unit != spec['unit']:
            problems.append(f'{sid}：{role} 的單位與來源契約不同')
        if claimed.metric_keys != list(spec['metric_keys']) or claimed.required != bool(spec.get('required')):
            problems.append(f'{sid}：{role} 的 metric_keys／required 與來源契約不同')
    # The collector rejects a fact whose unit currency differs from the configured one.
    for role, spec in roles.items():
        if any(unit.split('/')[0] != target.currency for unit in spec['units']):
            problems.append(f'{sid}：{role} 的來源單位與標的幣別 {target.currency} 不符')
    expected = [profile['endpoint'].format(cik=target.cik, concept=concept)
                for spec in roles.values() for concept in spec['concepts']]
    if binding.request_urls != expected:
        problems.append(f'{sid}：request_urls 與來源契約依 CIK {target.cik} 產生的請求網址不同')
    return problems


def tw_item_drift(item, profile, target):
    sid, problems = item.source_id, []
    if profile.get('exchange') != target.exchange or profile.get('currency') != target.currency:
        problems.append(f'{sid}：來源市場／幣別與標的不符')
    fields = profile.get('fields')
    if item.sec_binding is not None or not isinstance(fields, dict):
        problems.append(f'{sid}：不是臺灣交易所欄位來源，與臺灣標的不相容')
    elif item.field_binding != {key: list(names) for key, names in fields.items()}:
        problems.append(f'{sid}：欄位對應與來源契約不同')
    return problems


def package_drift(package, registry, instruments):
    """Facts in the package that no longer match code, instrument config or registry."""
    target = package.target
    sec = isinstance(target, SecTarget)
    problems = sec_target_drift(target, instruments) if sec else tw_target_drift(target, instruments)
    if registry.registry_version != package.registry_baseline_version:
        problems.append(f'登錄檔版本為 {registry.registry_version}，審查包基準為 {package.registry_baseline_version}；'
                        f'需重新核對審查包')
    known = {s.source_id: s for s in registry.sources}
    if len({item.source_id for item in package.items}) != len(package.items):
        problems.append('審查包重複列出同一來源')
    for item in package.items:
        profile, source = (PROFILES.get(item.source_id) or FILING_PROFILES.get(item.source_id)
                           or REVIEWED_PROFILES.get(item.source_id)), known.get(item.source_id)
        if profile is None or source is None:
            problems.append(f'{item.source_id} 不在來源契約或授權登錄檔')
            continue
        if not (item.endpoint == profile['endpoint'] == source.endpoint_scope):
            problems.append(f'{item.source_id}：端點與來源契約或登錄範圍不一致')
        if profile.get('adapter') in ('sec_filing_document', 'sec_submissions', 'reviewed_html', 'reviewed_rss'):
            if not sec or item.sec_binding is not None:
                problems.append(f'{item.source_id}：申報文件必須使用 SEC 標的，不能聲稱 XBRL 欄位契約')
            if item.field_binding != profile['identity_binding']:
                problems.append(f'{item.source_id}：申報身份欄位契約不符')
            if sec and profile.get('entities_config', 'config/sec-entities.json') != target.entity_config:
                problems.append(f'{item.source_id}：申報標的設定不符')
            if sec and profile.get('tickers') and target.ticker not in profile['tickers']:
                problems.append(f'{item.source_id}：標的不在此來源契約範圍')
        else:
            problems += (sec_item_drift if sec else tw_item_drift)(item, profile, target)
    return problems


def load_target_config(target, instruments):
    """Resolve the target configuration. Returns (config, problems); never reads a package path.

    `instruments` is an already-loaded mapping, a path chosen by the operator, or None for the
    default of the target kind. A SEC target is only checked against the file it names."""
    sec = isinstance(target, SecTarget)
    if isinstance(instruments, dict):
        return instruments, []
    path = Path(instruments) if instruments is not None else Path(DEFAULT_CONFIG[target_kind(target)])
    if sec and os.path.normpath(path) != os.path.normpath(target.entity_config):
        return {}, [f'設定檔 {path} 不是審查包指定的 {target.entity_config}；不得以其他設定檔核對 SEC 標的']
    return read_json(path), []


def candidate_violations(package, current, candidate):
    """Everything a candidate registry changes beyond the packaged scope and its review basis.

    Returns (violations, changed_source_ids, review_basis_by_source)."""
    violations, bases = [], {}
    items = {item.source_id: item for item in package.items}
    before = {s.source_id: s for s in current.sources}
    after = {s.source_id: s for s in candidate.sources}
    if set(before) != set(after):
        violations.append('候選登錄檔新增或移除了來源；啟用只能修改審查包列出的來源')
    if candidate.registry_version == current.registry_version:
        violations.append('候選登錄檔必須提升 registry_version，既有採集才會顯示版本差異')
    changed = []
    for source_id in sorted(set(before) & set(after)):
        old, new = before[source_id], after[source_id]
        if old == new:
            continue
        changed.append(source_id)
        item = items.get(source_id)
        if item is None or item.recommendation not in READY:
            violations.append(f'{source_id}：不在審查包建議啟用的範圍，卻被修改')
            continue
        for name in FROZEN_FIELDS:
            if getattr(old, name) != getattr(new, name):
                violations.append(f'{source_id}：{name} 不得在啟用時變更')
        if new.declared_status != 'confirmed':
            violations.append(f'{source_id}：狀態為 {new.declared_status}，不是完整的來源查核確認')
            continue
        for name in PERMISSIONS:
            if getattr(new.scope, name) != getattr(item.proposed_scope, name):
                violations.append(f'{source_id}：{name}={getattr(new.scope, name)} 超出或不同於審查包建議的 '
                                  f'{getattr(item.proposed_scope, name)}')
        if new.attribution != item.proposed_attribution:
            violations.append(f'{source_id}：來源標示必須採用審查包的顯名文字')
        human = [t for t in new.terms_evidence if t.checked_by == 'human']
        allowed_urls = {item.dataset_page, item.license_url}
        # A human-labelled record written by an assistant or a fixture is a disguised sign-off on
        # either basis; an assistant's work belongs in checked_by=assistant and source_review.
        if any(looks_non_human(t.checker) for t in human) or looks_non_human(new.attested_by):
            violations.append(f'{source_id}：簽認人或人工查核者標示為助理／測試，不能視為人工簽認；'
                              f'AI 查核請記錄於 source_review')
        basis = bases[source_id] = new.review_basis()
        if basis == 'human_attestation':
            if not {t.terms_url for t in human} >= allowed_urls:
                violations.append(f'{source_id}：人工逐字引用必須同時涵蓋資料集頁面與授權條款頁')
            if new.attested_at and any(t.checked_at > new.attested_at for t in human):
                violations.append(f'{source_id}：簽認時間早於人工查核時間')
        elif basis == 'assistant_source_review':
            review = new.source_review
            if new.attested_by or new.attested_at or human:
                violations.append(f'{source_id}：AI 查核不得同時填寫人工簽認欄位或人工查核紀錄')
            if not {f.official_url for f in review.findings} >= allowed_urls:
                violations.append(f'{source_id}：AI 查核的官方依據必須同時涵蓋資料集頁面與授權條款頁')
            if not {t.terms_url for t in new.terms_evidence} >= allowed_urls:
                violations.append(f'{source_id}：資料集頁面與授權條款頁都必須留有逐字引用（terms_evidence）')
        else:
            violations.append(f'{source_id}：沒有可辨識的查核依據（人工簽認或合格 AI 查核）')
        if old.source_review is not None and new.source_review != old.source_review and basis != 'assistant_source_review':
            violations.append(f'{source_id}：既有的 AI 查核紀錄不得在人工簽認時改寫')
        if [t for t in old.terms_evidence if t not in new.terms_evidence]:
            violations.append(f'{source_id}：既有的助理查核紀錄不得刪除或改寫')
    if not changed:
        violations.append('候選登錄檔與目前登錄檔相同，沒有任何啟用')
    return violations, changed, bases


def check(package_path, registry, instruments=None, candidate_path=None):
    package = ActivationPackage.model_validate(read_json(package_path))
    config, config_problems = load_target_config(package.target, instruments)
    drift = config_problems + package_drift(package, registry, config)
    known = {s.source_id: s for s in registry.sources}
    items = [{'source_id': item.source_id, 'recommendation': item.recommendation,
              'registry_status': known[item.source_id].declared_status if item.source_id in known else 'absent',
              'endpoint': item.endpoint, 'proposed_scope': item.proposed_scope.model_dump(),
              'open_questions': len(item.open_questions)} for item in package.items]
    result = {'package_id': package.package_id, 'target': package.target.ticker,
              'target_kind': target_kind(package.target),
              'package_status': package.status,
              'registry_version': registry.registry_version, 'items': items, 'package_drift': drift,
              'registry_modified': False}
    if drift:
        return {**result, 'status': 'blocked', 'blockers': drift}
    if candidate_path is None:
        ready = [item for item in package.items if item.recommendation in READY]
        bases = {item.source_id: known[item.source_id].review_basis() for item in ready}
        if ready and all(bases.values()):
            # The registry already records a review for every recommended source: nothing is pending.
            return {**result, 'status': 'registry_already_reviewed', 'registry_review_basis': bases,
                    'warnings': ['登錄檔已記錄查核依據；此檢查未比對啟用範圍，AI 查核不是人工簽認']}
        return {**result, 'status': 'awaiting_source_review',
                'accepted_review_bases': ['human_attestation', 'assistant_source_review'],
                'required_review_actions': [f'{item.source_id}：{step}' for item in ready for step in item.checklist()],
                'warnings': ['審查包不授與任何權限；登錄檔未被修改，來源維持封閉',
                             '查核可由人工簽認，或由使用者授權的 AI 依官方依據完成並記錄於 source_review；'
                             'AI 查核不是人工簽認，證據不足時維持封閉']}
    # A candidate that is not a valid registry (for example confirmed without a human attestation
    # or a sufficient assistant review) fails model validation here and is reported as a failure.
    candidate = LicenseRegistry.model_validate(read_json(candidate_path))
    violations, changed, bases = candidate_violations(package, registry, candidate)
    if violations:
        return {**result, 'status': 'blocked', 'candidate_changed_sources': changed,
                'candidate_review_basis': bases, 'blockers': violations}
    return {**result, 'status': 'candidate_matches_package', 'candidate_changed_sources': changed,
            'candidate_review_basis': bases, 'candidate_registry_version': candidate.registry_version,
            'warnings': ['此檢查只比對範圍與查核紀錄的結構，不能證明引用內容正確、官方頁面確實如此記載，'
                         '也不能證明人工簽認者確實為人類；AI 查核一律標示為 assistant_source_review，不是人工簽認']}
