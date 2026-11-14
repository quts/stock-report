"""Load historical interactive evaluation without inventing a fresh model run."""
from pathlib import Path
import hashlib
from .storage import read_json

CASES = {'normal', 'conflicting_evidence', 'missing_financials', 'missing_valuation', 'missing_all_ratings',
         'unverified_event', 'unknown_evidence_id', 'future_data', 'license_unknown', 'prompt_injection'}
ARTIFACTS = {'research_input.json', 'ai_proposal.json', 'decision.json', 'report.pdf'}


def load_evaluation(local, ledger, artifact_root):
    local, ledger, artifact_root = Path(local), Path(ledger), Path(artifact_root).resolve()
    if not local.exists() and not ledger.exists():
        return {'status': 'NOT_RUN', 'cases': [], 'reason': 'No actual interactive evaluation record is available'}
    result = read_json(local if local.exists() else ledger)
    rows = result.get('cases', [])
    if result.get('status') != 'PASSED' or len(rows) != 10 or {r['case'] for r in rows} != CASES:
        raise ValueError('Interactive evaluation does not contain the ten completed controlled cases')
    if not all(r.get('schema_valid') and r.get('matched_expected') for r in rows):
        raise ValueError('Interactive evaluation contains unvalidated cases')
    if local.exists():
        for row in rows:
            run = Path(row['run']).resolve()
            if not run.is_relative_to(artifact_root):
                raise ValueError('Evaluation artifacts must stay inside the local report directory')
            hashes = row.get('artifact_hashes', {})
            if set(hashes) != ARTIFACTS:
                raise ValueError('Interactive evaluation lacks required artifact hashes')
            for name, expected in hashes.items():
                if hashlib.sha256((run / name).read_bytes()).hexdigest() != expected:
                    raise ValueError('Interactive evaluation artifact integrity failed')
            decision = read_json(run / 'decision.json')
            if [p['outlook'] for p in decision['outlooks']] != row['actual_outlooks'] or row['actual_outlooks'] != row['expected_outlooks']:
                raise ValueError('Interactive evaluation result does not match its decision')
        result['artifact_integrity_verified'] = True
        result['artifact_availability'] = 'local_artifacts_verified'
    else:
        result['artifact_integrity_verified'] = False
        result['artifact_availability'] = 'versioned_summary_only; local artifacts not available'
    result['reexecuted_during_bundle_creation'] = False
    result['scope_notice'] = 'Historical single-session batch at implementation_commit, not a fresh run or ten independent model invocations'
    return result
