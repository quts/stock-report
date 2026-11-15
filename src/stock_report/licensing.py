"""Versioned source-license registry.

Accessibility of an endpoint never establishes any right to reuse its contents, and
neither does the fact that reports are for personal use only. Every source starts at
`unknown`, which is not cleared for the personal report or for local persistence.
`confirmed` is only representable on one of two recorded bases, and they are never merged:

- `human_attestation`: a quoted terms excerpt checked by a human, the exact terms URL and
  check date, and a named human attestation (`attested_by`). `human_attested` keeps exactly
  this meaning.
- `assistant_source_review`: the user authorized an AI assistant to verify official sources.
  The entry then carries a `source_review` whose findings each cite an official https URL
  and a quoted excerpt for every required aspect (official source, identity, publication
  time, terms of use, free access). It is never recorded as a human sign-off, may only open
  `internal_use_allowed` (lawful, free, personal, non-distributed use), and a review that
  leaves anything unresolved keeps the source closed.

`source_reviewed` is true for either basis; `review_basis` says which one.

Permissions are separate and none implies another:
- `internal_use_allowed`: values may be persisted in the local run and used in the
  personal-use PDF, metrics and proposals.
- `store_response_hash` / `store_raw_response`: local raw audit retention.
- `pdf_share_allowed`: legacy redistribution flag. Nothing is distributed, so it gates
  nothing and no redistribution attestation is needed for a personal report.

Nothing here reads credentials or derives contact details from login data.
"""
import os
from pathlib import Path
import re
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .models import Model, SourceClass
from .storage import read_json

DEFAULT_REGISTRY_PATH = Path('config/source-licenses.json')
REGISTRY_PATH_ENV = 'STOCK_REPORT_LICENSE_REGISTRY'
PERMISSIONS = ('internal_use_allowed', 'pdf_share_allowed', 'store_response_hash', 'store_raw_response')


def endpoint_in_scope(endpoint: str, scope: str) -> bool:
    """A reviewed scope may be an exact URL or a `{placeholder}` template for one dataset.

    Placeholders match a single path segment, so a different host, path or dataset never
    inherits the reviewed dataset's permissions.
    """
    if not endpoint or not scope:
        return False
    pattern = ''.join(r'[^/?#]+' if part.startswith('{') and part.endswith('}') else re.escape(part)
                      for part in re.split(r'(\{[a-z_]+\})', scope) if part)
    return re.fullmatch(pattern, endpoint) is not None


class TermsEvidence(Model):
    """A specific, quotable observation of the source's own terms."""
    terms_url: str = Field(min_length=8, max_length=300)
    checked_at: AwareDatetime
    checked_by: Literal['human', 'assistant', 'automated-fetch']
    checker: str = Field(min_length=1, max_length=120)
    excerpt: str = Field(min_length=1, max_length=2000)
    excerpt_covers: str = Field(min_length=1, max_length=400)


ReviewAspect = Literal['official_source', 'identity', 'publication_time', 'terms_of_use', 'free_access']
# Every aspect an assistant review must establish from the source's own official pages.
REQUIRED_REVIEW_ASPECTS = ('official_source', 'identity', 'publication_time', 'terms_of_use', 'free_access')
# The only permission an assistant review may open: minimum lawful, free, personal use.
ASSISTANT_REVIEW_PERMISSIONS = ('internal_use_allowed',)


class ReviewFinding(Model):
    """One traceable observation: what an official page says about one aspect of the source."""
    aspect: ReviewAspect
    official_url: str = Field(pattern=r'^https://[^\s/]+\S*$', max_length=300)
    checked_at: AwareDatetime
    excerpt: str = Field(min_length=1, max_length=2000)
    conclusion: str = Field(min_length=1, max_length=400)


class SourceReview(Model):
    """An AI assistant's source review, made under an explicit user authorization.

    It is not a human attestation and is never reported as one. `sufficient` is only
    representable when every required aspect has a finding and nothing is unresolved."""
    reviewed_by: Literal['assistant']
    reviewer: str = Field(min_length=1, max_length=120)
    reviewed_at: AwareDatetime
    # Where the user's authorization for assistant review is recorded (for example AGENTS.md and its date).
    authorization: str = Field(min_length=1, max_length=300)
    # The exact endpoint scope that was reviewed; a review never transfers to another dataset.
    reviewed_endpoint_scope: str = Field(min_length=1, max_length=300)
    use_basis: Literal['personal_non_distributed'] = 'personal_non_distributed'
    verdict: Literal['sufficient', 'insufficient']
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=24)
    unresolved: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode='after')
    def sufficiency_requires_complete_evidence(self):
        if any(f.checked_at > self.reviewed_at for f in self.findings):
            raise ValueError('A source review cannot be dated before the findings it rests on')
        if self.verdict == 'sufficient':
            missing = [a for a in REQUIRED_REVIEW_ASPECTS if a not in {f.aspect for f in self.findings}]
            if missing:
                raise ValueError(f'A sufficient source review needs an official finding for: {missing}')
            if self.unresolved:
                raise ValueError('A source review with unresolved questions is insufficient; the source stays closed')
        return self


class Scope(Model):
    internal_use_allowed: bool = False
    pdf_share_allowed: bool = False
    store_response_hash: bool = False
    store_raw_response: bool = False


class SourceLicense(Model):
    source_id: str = Field(pattern=r'^[a-z0-9][a-z0-9.\-]{2,63}$')
    publisher: str = Field(min_length=1, max_length=80)
    # Official filing/exchange record, or a secondary market-data provider.
    source_class: SourceClass = 'official'
    attribution: str = Field(min_length=1, max_length=200)
    endpoint_scope: str = Field(min_length=1, max_length=300)
    terms_url: str = Field(min_length=8, max_length=300)
    declared_status: Literal['confirmed', 'unknown', 'restricted'] = 'unknown'
    scope: Scope = Field(default_factory=Scope)
    terms_evidence: list[TermsEvidence] = Field(default_factory=list, max_length=12)
    attested_by: str | None = Field(default=None, max_length=120)
    attested_at: AwareDatetime | None = None
    # Assistant review under user authorization. Separate from, and never a substitute label for,
    # the human attestation above.
    source_review: SourceReview | None = None
    open_questions: list[str] = Field(default_factory=list, max_length=12)
    notes: str = Field(default='', max_length=1000)

    @model_validator(mode='after')
    def confirmation_requires_specific_evidence(self):
        human_evidence = any(t.checked_by == 'human' for t in self.terms_evidence)
        if (self.attested_by or self.attested_at) and not human_evidence:
            # `attested_by` is a human sign-off. An assistant review is recorded in `source_review`.
            raise ValueError(f'{self.source_id}: a named attestation is a human sign-off and requires a '
                             f'human-checked quoted excerpt; record an assistant review in source_review')
        review = self.source_review
        if review is not None and review.reviewed_endpoint_scope != self.endpoint_scope:
            raise ValueError(f'{self.source_id}: source_review covers {review.reviewed_endpoint_scope}, not this '
                             f'endpoint scope; a review never transfers to another dataset')
        if self.declared_status == 'confirmed':
            if self.assistant_reviewed() and not (human_evidence and self.attested_by):
                self._check_assistant_confirmation()
            else:
                if not human_evidence:
                    raise ValueError(f'{self.source_id}: confirmed terms require a human-checked quoted excerpt '
                                     f'or a sufficient assistant source_review with official evidence')
                if not self.attested_by or not self.attested_at:
                    raise ValueError(f'{self.source_id}: confirmed terms require a named attestation and date')
        elif any(getattr(self.scope, name) for name in PERMISSIONS):
            raise ValueError(f'{self.source_id}: permissions may only be granted by a confirmed, attested entry')
        return self

    def _check_assistant_confirmation(self):
        """Confirmation resting only on an assistant review: traceable, free, personal use, minimal."""
        review = self.source_review
        opened = [name for name in PERMISSIONS if getattr(self.scope, name) and name not in ASSISTANT_REVIEW_PERMISSIONS]
        if opened:
            raise ValueError(f'{self.source_id}: an assistant source review may only open '
                             f'{list(ASSISTANT_REVIEW_PERMISSIONS)}; {opened} need a human attestation')
        quoted = {t.terms_url for t in self.terms_evidence}
        terms = {f.official_url for f in review.findings if f.aspect == 'terms_of_use'}
        if self.terms_url not in terms:
            raise ValueError(f'{self.source_id}: the terms_of_use finding must cite the registered terms_url')
        if not terms <= quoted:
            raise ValueError(f'{self.source_id}: every terms_of_use finding must also be recorded as a quoted '
                             f'terms_evidence excerpt from the same URL')

    def human_attested(self) -> bool:
        """A named human sign-off backed by a human-checked excerpt. Never true for an assistant review."""
        return bool(self.attested_by and any(t.checked_by == 'human' for t in self.terms_evidence))

    def assistant_reviewed(self) -> bool:
        return self.source_review is not None and self.source_review.verdict == 'sufficient'

    def review_basis(self):
        """Which recorded review makes this entry usable, or None while it stays closed."""
        if self.declared_status != 'confirmed':
            return None
        if self.human_attested():
            return 'human_attestation'
        return 'assistant_source_review' if self.assistant_reviewed() else None

    def permits(self, permission: str) -> bool:
        if permission not in PERMISSIONS:
            raise ValueError('Unknown permission')
        return self.declared_status == 'confirmed' and getattr(self.scope, permission)

    def covers_endpoint(self, endpoint: str) -> bool:
        """Permissions belong to the reviewed dataset endpoint, never to a different URL."""
        return endpoint_in_scope(endpoint, self.endpoint_scope)

    def checked_at(self):
        """Latest recorded terms check; falls back to the registry update stamp."""
        stamps = [t.checked_at for t in self.terms_evidence]
        if self.source_review is not None:
            stamps += [f.checked_at for f in self.source_review.findings]
        return max(stamps, default=None)


class LicenseRegistry(Model):
    schema_version: Literal['1'] = '1'
    registry_version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    updated_at: AwareDatetime
    policy: str = Field(min_length=1, max_length=500)
    sources: list[SourceLicense] = Field(min_length=1, max_length=64)

    @model_validator(mode='after')
    def unique_source_ids(self):
        ids = [s.source_id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate source_id in license registry')
        return self

    def get(self, source_id: str) -> SourceLicense:
        for source in self.sources:
            if source.source_id == source_id:
                return source
        raise ValueError(f'Source {source_id} is absent from license registry {self.registry_version}; '
                         f'known sources: {sorted(s.source_id for s in self.sources)}')

    def snapshot(self, source_id: str) -> dict:
        """Resolved, artifact-safe license facts for one source."""
        source = self.get(source_id)
        checked_at = source.checked_at() or self.updated_at
        review = source.source_review
        return {'source_id': source.source_id, 'registry_version': self.registry_version,
                'publisher': source.publisher, 'source_class': source.source_class,
                'attribution': source.attribution,
                'terms_url': source.terms_url, 'status': source.declared_status,
                'endpoint_scope': source.endpoint_scope,
                'terms_evidence': [{'terms_url': t.terms_url, 'checked_at': t.checked_at.isoformat(),
                                    'checked_by': t.checked_by, 'checker': t.checker,
                                    'excerpt_covers': t.excerpt_covers} for t in source.terms_evidence],
                # `human_attested` is the human sign-off only; `source_reviewed` also accepts a
                # sufficient assistant review, and `review_basis` records which one applies.
                'human_attested': source.human_attested(),
                'source_reviewed': source.review_basis() is not None,
                'review_basis': source.review_basis(),
                'source_review': None if review is None else {
                    'reviewed_by': review.reviewed_by, 'reviewer': review.reviewer,
                    'reviewed_at': review.reviewed_at.isoformat(), 'authorization': review.authorization,
                    'verdict': review.verdict, 'use_basis': review.use_basis,
                    'findings': [{'aspect': f.aspect, 'official_url': f.official_url,
                                  'checked_at': f.checked_at.isoformat()} for f in review.findings],
                    'unresolved': list(review.unresolved)},
                'attested_by': source.attested_by, 'checked_at': checked_at.isoformat(),
                'terms_evidence_count': len(source.terms_evidence),
                'open_questions': list(source.open_questions),
                **{name: source.permits(name) for name in PERMISSIONS}}


def load_registry(path=None) -> LicenseRegistry:
    path = Path(path or os.environ.get(REGISTRY_PATH_ENV) or DEFAULT_REGISTRY_PATH)
    if not path.is_file():
        raise FileNotFoundError(f'License registry not found at {path}; real collection cannot proceed')
    return LicenseRegistry.model_validate(read_json(path))
