"""Supplement native attribution with sealed hook diagnostics, never acceptance."""
import hashlib
import json
import math
from pathlib import Path


def collect_startup(evidence, bindings, base):
    records, issues, covered = [], [], set()
    expected = {b['session_id'] for b in bindings if isinstance(b, dict)
                and isinstance(b.get('session_id'), str) and b['session_id']}
    if not isinstance(evidence, list):
        evidence = []
        issues.append({'code': 'invalid_startup_evidence'})
    seen = set()
    for ref in evidence:
        try:
            path = Path(base) / ref['path']
            content = path.read_bytes()
            file_hash = hashlib.sha256(content).hexdigest()
            if ref.get('sha256') != file_hash:
                raise ValueError('startup evidence hash mismatch')
            for number, line in enumerate(content.splitlines(), 1):
                row = json.loads(line)
                if not isinstance(row, dict) or row.get('schema_version') != 1 or row.get('event') != 'hook_health':
                    raise ValueError('invalid hook record')
                key = (file_hash, number)
                if key in seen:
                    continue
                seen.add(key)
                for field in ('serialized_bytes', 'injected_bytes', 'duration_ms'):
                    value = row.get(field)
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError('invalid hook metric')
                provider = {'claude': 'claude', 'codex': 'codex'}.get(row.get('host'))
                matches = [b for b in bindings if provider and isinstance(b, dict)
                           and row.get('session_id') and b.get('session_id') == row['session_id']
                           and b.get('provider') == provider
                           and (not b.get('run_id') or b['run_id'] == row.get('run_id'))]
                allocations = {b.get('allocation', b.get('enrollment_id')) for b in matches}
                if not matches or len(allocations) != 1:
                    issues.append({'code': 'unbound_or_ambiguous_startup_session', 'session_id': row.get('session_id')})
                    continue
                covered.add(row['session_id'])
                records.append(row | {'allocation': next(iter(allocations)),
                                      'independent_acceptance': None,
                                      'native_identity_verified': False,
                                      'evidence': {'path': str(path), 'sha256': file_hash, 'line': number}})
        except (OSError, ValueError, TypeError, KeyError) as error:
            issues.append({'code': 'invalid_startup_evidence', 'detail': str(error)})
    missing = sorted(expected - covered)
    invalid = any(i['code'] == 'invalid_startup_evidence' for i in issues)
    return {'coverage': 'invalid' if invalid else 'incomplete' if missing or issues else 'complete',
            'records': records, 'issues': issues, 'missing_sessions': missing,
            'identity_basis': 'hook diagnostic joined to declared native binding; not native model proof',
            'allowance_savings': None, 'independent_acceptance': None}
