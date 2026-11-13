"""Consistent exports, request evidence and source-grounded field comparison."""
import json
import os
from pathlib import Path
import tempfile
import uuid

from .core import ROOT, dumps, model_request


def atomic_write(path,text):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def export(store,directory):
    directory=Path(directory)
    # Serialize exporters against workers and other exporters on this database.
    with store.connect(write=True) as db:
        rows=[store.record(r) for r in db.execute('SELECT a.* FROM documents d JOIN attempts a ON d.current_attempt=a.id ORDER BY a.document_id')]
        snapshot=uuid.uuid4().hex
        for row in rows: row['snapshot_id']=snapshot
        atomic_write(directory/'extractions.jsonl',''.join(dumps(row)+'\n' for row in rows))
        queue={'snapshot_id':snapshot,'requests':[
            {key:row[key] for key in ('document_id','attempt_id','source_sha256','config_sha256','status','owner','next_action','error','snapshot_id')}
            for row in rows]}
        atomic_write(directory/'queue.json',json.dumps(queue,indent=2)+'\n')
    return len(rows)


def evidence(store,attempt,path):
    config=store.settings(attempt)
    # Use an innocuous synthetic message; never include API_KEY or an authorization header.
    payload=model_request('Request EVIDENCE-001 dated 2026-10-22. Please clarify my access needs.',config)
    value={'provider':config['provider'],'response_source':'mock' if config['provider']=='mock' else 'openai/'+config['model'],
           'live_call_observed':False,'attempt_id':attempt,'config_sha256_note':'See extraction record for exact settings hash.',
           'endpoint':config['api_url'],'headers':{'Authorization':'Bearer [REDACTED]','Content-Type':'application/json'},
           'request':payload,
           'handling':[
               'Reject missing/invalid choices, refusals and finish_reason other than stop before parsing extraction JSON.',
               'Validate all schema fields/types/enums, ISO calendar dates and exact evidence substrings.',
               'HTTP 408/429/5xx and transport timeouts retry with bounded backoff and shared per-database rate limiting.',
               'Persist final failures with null extraction and retry command; retain successful prior attempts in history.',
               'No raw provider error bodies, credentials or exception details are logged.',
           ]}
    atomic_write(path,json.dumps(value,indent=2)+'\n')


def accuracy(store,expected_path,path):
    expected=json.loads(Path(expected_path).read_text(encoding='utf-8'))
    rows={r['document_id']:r for r in store.current()}
    correct=total=0; failures=[]; missing=[]
    table=[]
    configs={}
    for name,case in expected.items():
        row=rows.get(name)
        if row is None: missing.append(name)
        comparisons={**case['fields'],**case['route']}
        matched=0
        for field,target in comparisons.items():
            total+=1
            actual=(row.get('extraction') or {}).get(field) if row and field in case['fields'] else row.get(field) if row else None
            # A missing/failed extraction never earns points for an expected null.
            eligible=bool(row and row.get('extraction') is not None)
            if eligible and actual==target:
                correct+=1; matched+=1
            else:
                failures.append({'document_id':name,'field':field,'expected':target,'actual':actual,'record_state':row['status'] if row else 'missing'})
        table.append(f"| {name} | {matched}/{len(comparisons)} | {row['response_source'] if row else 'missing'} |")
        if row: configs[row['config_sha256']]=store.settings(row['attempt_id'])
    extra=sorted(set(rows)-set(expected))
    lines=['# Accuracy procedure and observations','',
           'Expected scalar values and routing were authored from the message text and office policy before application outputs. The full source message is retained as evidence beside each case in `evaluation/expected.json`. Business purpose uses a selected exact excerpt; alternate correct summaries can fail this deliberately strict metric.','',
           'Count 12 extraction scalar fields plus status, owner and next_action per message: 15 comparisons each, 315 for the 20 supplied exports and the authored PARA-001 paraphrase. Null equals null only for an existing successful extraction. Missing/error/pending records score zero, including expected nulls. No case folding, whitespace trimming or date normalization. Quotes and issues are not in the denominator: schema/evidence checks and targeted tests cover them. Extra records are listed and excluded.','',
           'Mocks were constructed from these expectations to exercise plumbing and routing. A mock score demonstrates fixture fidelity and integration behavior; it does not measure LLM extraction accuracy. Provider scores, when present, are observed exact-match scores rather than claims of general accuracy. Response sources in the table identify which observations used a provider.','',
           f'Exact matches: **{correct}/{total}**.',f'Missing records: {", ".join(missing) or "none"}.',f'Extra records excluded: {", ".join(extra) or "none"}.','',
           '| Document | Exact matches | Response source |','|---|---:|---|',*table,'',
           '## Factual differences','', '```json',json.dumps(failures,indent=2),'```','',
           '## Expected facts and source evidence','',
           'The following is the source-authored reference, not values copied from extraction results. Policy rules are in `materials/office-policy.md`: overrides (1), withdrawals (2), clarification (3), risk review (4), staging/read (5).','']
    for name,case in expected.items():
        lines.extend([f'### {name}','', 'Source message: '+case['evidence'][0],'','```json',json.dumps({'fields':case['fields'],'route':case['route'],'issues_to_check':case['expected_issues']},indent=2),'```',''])
    lines.extend(['## Configurations',''])
    for fingerprint,config in configs.items():
        summary={k:v for k,v in config.items() if k not in ('mock_replies','policy','prompt','schema')}
        summary['policy_sha256']=config['policy_sha256']
        lines.extend([f'Settings SHA-256: `{fingerprint}`','', '```json',json.dumps(summary,indent=2),'```',''])
    atomic_write(path,'\n'.join(lines)+'\n')
    return 1 if failures else 0
