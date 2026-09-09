import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))


def test_missing_hook_coverage_is_explicit():
    from startup_evidence import collect_startup
    result=collect_startup([], [{'session_id':'s','provider':'codex'}],Path('.'))
    assert result['coverage']=='incomplete'
    assert result['missing_sessions']==['s']


def test_sealed_hook_evidence_joins_without_acceptance(tmp_path):
    from startup_evidence import collect_startup
    record={'schema_version':1,'event':'hook_health','session_id':'s','host':'codex','run_id':'r',
            'task_id':'t','serialized_bytes':9500,'injected_bytes':9000,'duration_ms':12.5,
            'status':'unknown','exit_status':0,'cache_freshness':'stale','model':None,'effort':None}
    path=tmp_path/'hooks.jsonl'; path.write_text(json.dumps(record)+'\n')
    evidence=[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]
    result=collect_startup(evidence,[{'session_id':'s','provider':'codex','run_id':'r','allocation':'e'}],tmp_path)
    assert result['coverage']=='complete'
    assert result['records'][0]['allocation']=='e'
    assert result['records'][0]['independent_acceptance'] is None
    path.write_text('{}\n')
    assert collect_startup(evidence,[],tmp_path)['coverage']=='invalid'


def test_unbound_sessions_and_malformed_rows_never_count(tmp_path):
    from startup_evidence import collect_startup
    path=tmp_path/'hooks.jsonl'; path.write_text('null\n')
    result=collect_startup([{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}],[],tmp_path)
    assert result['coverage']=='invalid'


def test_unknown_host_cannot_join_providerless_binding(tmp_path):
    from startup_evidence import collect_startup
    row={'schema_version':1,'event':'hook_health','session_id':'s','host':'kimi',
         'serialized_bytes':1,'injected_bytes':1,'duration_ms':1}
    path=tmp_path/'hooks.jsonl';path.write_text(json.dumps(row)+'\n')
    refs=[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}]
    result=collect_startup(refs,[{'session_id':'s','allocation':'e'}],tmp_path)
    assert result['coverage']=='incomplete' and result['records']==[]
