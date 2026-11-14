"""Versioned source-license registry.

Accessibility of an endpoint never establishes any right to reuse or redistribute
its contents. Every source starts at `unknown`, and `unknown` is non-shareable and
not even cleared for local persistence. `confirmed` is only representable when the
registry carries a quoted terms excerpt that was checked by a human, the exact
terms URL and check date, and a named attestation. Nothing here reads credentials
or derives contact details from login data.
"""
import os
from pathlib import Path
import re
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .models import Model
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


class Scope(Model):
    internal_use_allowed: bool = False
    pdf_share_allowed: bool = False
    store_response_hash: bool = False
    store_raw_response: bool = False


class SourceLicense(Model):
    source_id: str = Field(pattern=r'^[a-z0-9][a-z0-9.\-]{2,63}$')
    publisher: str = Field(min_length=1, max_length=80)
    attribution: str = Field(min_length=1, max_length=200)
    endpoint_scope: str = Field(min_length=1, max_length=300)
    terms_url: str = Field(min_length=8, max_length=300)
    declared_status: Literal['confirmed', 'unknown', 'restricted'] = 'unknown'
    scope: Scope = Field(default_factory=Scope)
    terms_evidence: list[TermsEvidence] = Field(default_factory=list, max_length=12)
    attested_by: str | None = Field(default=None, max_length=120)
    attested_at: AwareDatetime | None = None
    open_questions: list[str] = Field(default_factory=list, max_length=12)
    notes: str = Field(default='', max_length=1000)

    @model_validator(mode='after')
    def confirmation_requires_specific_evidence(self):
        if self.declared_status == 'confirmed':
            if not any(t.checked_by == 'human' for t in self.terms_evidence):
                raise ValueError(f'{self.source_id}: confirmed terms require a human-checked quoted excerpt')
            if not self.attested_by or not self.attested_at:
                raise ValueError(f'{self.source_id}: confirmed terms require a named attestation and date')
        elif any(getattr(self.scope, name) for name in PERMISSIONS):
            raise ValueError(f'{self.source_id}: permissions may only be granted by a confirmed, attested entry')
        return self

    def permits(self, permission: str) -> bool:
        if permission not in PERMISSIONS:
            raise ValueError('Unknown permission')
        return self.declared_status == 'confirmed' and getattr(self.scope, permission)

    def covers_endpoint(self, endpoint: str) -> bool:
        """Permissions belong to the reviewed dataset endpoint, never to a different URL."""
        return endpoint_in_scope(endpoint, self.endpoint_scope)

    def checked_at(self):
        """Latest recorded terms check; falls back to the registry update stamp."""
        return max((t.checked_at for t in self.terms_evidence), default=None)


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
        return {'source_id': source.source_id, 'registry_version': self.registry_version,
                'publisher': source.publisher, 'attribution': source.attribution,
                'terms_url': source.terms_url, 'status': source.declared_status,
                'endpoint_scope': source.endpoint_scope,
                'terms_evidence': [{'terms_url': t.terms_url, 'checked_at': t.checked_at.isoformat(),
                                    'checked_by': t.checked_by, 'checker': t.checker,
                                    'excerpt_covers': t.excerpt_covers} for t in source.terms_evidence],
                'human_attested': bool(source.attested_by and any(t.checked_by == 'human' for t in source.terms_evidence)),
                'attested_by': source.attested_by, 'checked_at': checked_at.isoformat(),
                'terms_evidence_count': len(source.terms_evidence),
                'open_questions': list(source.open_questions),
                **{name: source.permits(name) for name in PERMISSIONS}}


def load_registry(path=None) -> LicenseRegistry:
    path = Path(path or os.environ.get(REGISTRY_PATH_ENV) or DEFAULT_REGISTRY_PATH)
    if not path.is_file():
        raise FileNotFoundError(f'License registry not found at {path}; real collection cannot proceed')
    return LicenseRegistry.model_validate(read_json(path))
