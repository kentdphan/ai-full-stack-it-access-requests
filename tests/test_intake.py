"""Behavioral tests use source-authored expectations and saved replies; no live calls."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/mock_responses'
EXPECTED = json.loads((ROOT / 'evaluation/expected.json').read_text())

class CLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.db = self.path / 'state.sqlite3'

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, '-m', 'access_intake', '--db', str(self.db), *map(str,args)], cwd=ROOT, capture_output=True, text=True, env={k:v for k,v in os.environ.items() if k!='API_KEY'})
        self.assertIn(result.returncode, code if isinstance(code, tuple) else (code,), result.stdout + result.stderr)
        return result.stdout

    def intake(self, *paths, code=0, extra=()):
        return self.cli('intake', *paths, '--provider', 'mock', '--mock-dir', FIXTURES, *extra, code=code)

    def queue(self):
        return json.loads(self.cli('queue', '--json'))

    def history(self, doc):
        return json.loads(self.cli('history', doc))

    def test_complete_batch_and_source_derived_routes(self):
        self.intake(ROOT/'materials/requests', ROOT/'evaluation/PARA-001.json')
        rows = self.queue()
        self.assertEqual(len(rows), 21)
        for row in rows:
            case = EXPECTED[row['document_id']]
            self.assertEqual({k:row[k] for k in case['route']},case['route'],row['document_id'])
            self.assertEqual({k:row['extraction'][k] for k in case['fields']},case['fields'])
            self.assertEqual(row['response_source'],'mock')
        output = self.path/'out'
        self.cli('export','--out',output)
        records = [json.loads(x) for x in (output/'extractions.jsonl').read_text().splitlines()]
        queue = json.loads((output/'queue.json').read_text())
        self.assertEqual({r['snapshot_id'] for r in records},{queue['snapshot_id']})
        self.assertEqual(len(queue['requests']),21)
        self.cli('accuracy','--expected',ROOT/'evaluation/expected.json','--out',output/'accuracy.md')
        self.assertIn('315/315',(output/'accuracy.md').read_text())

    def test_unchanged_reuse_changed_bytes_and_batch_absence(self):
        src = ROOT/'materials/requests/IT-001.json'
        dest = self.path/src.name
        dest.write_bytes(src.read_bytes())
        self.intake(dest)
        first = self.queue()[0]
        self.intake(dest)
        self.assertEqual(len(self.history(src.name)),1)
        dest.write_bytes(src.read_bytes()+b'\n')
        self.intake(dest)
        current = self.queue()[0]
        self.assertEqual(len(self.history(src.name)),2)
        self.assertNotEqual(first['source_sha256'], current['source_sha256'])
        self.assertEqual(current['source_sha256'],hashlib.sha256(dest.read_bytes()).hexdigest())
        self.intake(ROOT/'materials/requests/IT-002.json')
        self.assertEqual(len(self.queue()),2)

    def test_failure_refusal_interruption_and_failed_retry_preserve_history(self):
        src = ROOT/'materials/requests/IT-001.json'
        self.intake(src)
        for name in ('provider-error','refusal','interrupted','malformed'):
            fixtures = self.path/name
            fixtures.mkdir()
            (fixtures/src.name).write_bytes((FIXTURES/(name+'.json')).read_bytes())
            self.cli('retry',src.name,'--mock-dir',fixtures,code=1)
            row=self.queue()[0]
            self.assertEqual(row['status'],'error')
            self.assertEqual(row['next_action'],'retry-extraction')
            self.assertIsNone(row['extraction'])
            self.assertIn('retry_command',row)
        self.assertEqual(len(self.history(src.name)),5)
        self.assertIsNotNone(self.history(src.name)[0]['extraction'])
        self.cli('retry',src.name,'--mock-dir',FIXTURES)
        self.assertEqual(self.queue()[0]['status'],'review')

    def test_enqueue_resume_and_expired_lease(self):
        self.intake(ROOT/'materials/requests/IT-001.json',extra=('--enqueue-only',))
        self.assertEqual(self.queue()[0]['status'],'pending')
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE attempts SET state='running', lease_until=0, worker='dead'")
        self.cli('resume')
        self.assertEqual(self.queue()[0]['status'],'review')
        self.assertEqual(len(self.history('IT-001.json')),1)

    def test_two_operators_and_stale_completion(self):
        def operator(_):
            return self.intake(ROOT/'materials/requests',extra=('--workers','3'),code=(0,2))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(operator,range(2)))
        self.assertEqual(len(self.queue()),20)
        self.assertEqual(len(self.history('IT-001.json')),1)
        # Publish completion for an older source after the newer one is queued.
        from access_intake.core import Store, make_config
        store=Store(self.db)
        src=ROOT/'materials/requests/IT-001.json'
        config=make_config('mock',mock_dir=FIXTURES)
        old=self.history(src.name)[0]
        store.register([(src.name,src.read_bytes())],config,force=True)
        running=store.claim('stale-worker')
        changed=src.read_bytes()+b'\n'
        store.register([(src.name,changed)],config,force=True)
        newer=self.queue()[0]['attempt_id']
        store.finish(running['id'],'stale-worker',old)
        current=next(r for r in self.queue() if r['document_id']==src.name)
        self.assertEqual(current['attempt_id'],newer)
        self.assertEqual(current['status'],'pending')

    def test_invalid_input_is_isolated_and_retry_uses_stored_source(self):
        bad=self.path/'bad.json'; bad.write_text('{')
        self.intake(bad,ROOT/'materials/requests/IT-001.json',code=1)
        self.assertEqual({r['status'] for r in self.queue()},{'error','review'})
        self.cli('retry','IT-001.json',code=1)
        self.assertEqual(len(self.history('IT-001.json')),2)

    def test_settings_refresh_and_accuracy_missing_record_fails(self):
        src=ROOT/'materials/requests/IT-001.json'
        self.intake(src)
        self.intake(src,extra=('--model','different-config',))
        self.assertEqual(len(self.history(src.name)),2)
        output=self.path/'accuracy.md'
        self.cli('accuracy','--out',output,code=1)
        self.assertIn('Missing records',output.read_text())

class ProviderBoundary(unittest.TestCase):
    def test_real_request_and_transport_without_key_or_network(self):
        from access_intake.core import make_config, extract, model_request
        config=make_config('openai')
        source=json.loads((ROOT/'materials/requests/IT-001.json').read_text())
        payload=model_request(source['message'],config)
        self.assertEqual(payload['response_format']['json_schema']['strict'],True)
        self.assertEqual(payload['messages'][-1]['role'],'user')
        response=mock.MagicMock()
        reply=json.loads((FIXTURES/'IT-001.json').read_text()); reply['model']='returned-model'
        response.read.return_value=json.dumps(reply).encode()
        response.__enter__.return_value=response
        with mock.patch('urllib.request.urlopen',return_value=response) as send:
            with mock.patch.dict(os.environ,{'API_KEY':'fake-secret'}):
                value, provenance=extract('IT-001.json',source['message'],config)
        self.assertEqual(value['requester'],'Maya Chen')
        self.assertEqual(provenance,'openai/returned-model')
        self.assertEqual(send.call_args.args[0].get_header('Authorization'),'Bearer fake-secret')

    def test_schema_evidence_and_dates_rejected(self):
        from access_intake.core import validate, make_config
        config=make_config('mock',mock_dir=FIXTURES)
        text=EXPECTED['IT-001.json']['evidence'][0]
        good=json.loads(json.loads((FIXTURES/'IT-001.json').read_text())['choices'][0]['message']['content'])
        for patch in ({'permission':'superuser'},{'supporting_quotes':['invented quote']},{'request_date':'10/22/2026'},{'surprise':1}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                validate({**good,**patch},text,config['schema'])

class Operations(unittest.TestCase):
    def setUp(self):
        from access_intake.core import Store,make_config
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)
        self.store=Store(self.path/'state.sqlite3')
        self.config=make_config('mock',mock_dir=FIXTURES)
        self.source=ROOT/'materials/requests/IT-001.json'

    def test_slow_message_leaves_other_completed_work_inspectable(self):
        import threading
        from access_intake.core import process,extract
        started=threading.Event(); release=threading.Event(); fast_done=threading.Event()
        self.store.register([(p.name,p.read_bytes()) for p in (self.source,ROOT/'materials/requests/IT-002.json')],self.config)
        def slow(name,message,config):
            if name=='IT-001.json':
                started.set()
                if not release.wait(10): raise ValueError('test-timeout')
            return extract(name,message,config)
        def progress(doc,status):
            if doc=='IT-002.json': fast_done.set()
        with mock.patch('access_intake.core.extract',side_effect=slow):
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                job=pool.submit(process,self.store,2,progress)
                try:
                    self.assertTrue(started.wait(5)); self.assertTrue(fast_done.wait(5))
                    rows={r['document_id']:r for r in self.store.current()}
                    self.assertEqual(rows['IT-001.json']['status'],'processing')
                    self.assertEqual(rows['IT-002.json']['status'],'review')
                finally: release.set()
                self.assertEqual(job.result(timeout=10),0)

    def test_changed_message_uses_new_facts_and_old_source_remains_in_history(self):
        from access_intake.core import process, digest
        self.store.register([(self.source.name,self.source.read_bytes())],self.config)
        self.assertEqual(process(self.store,1),0)
        raw=self.source.read_bytes().replace(b'Maya Chen',b'Mara Chen')
        config=json.loads(json.dumps(self.config))
        fixture=config['mock_replies'][self.source.name]
        # Author the second canned response for the explicitly changed source.
        fixture['message_sha256']=digest(json.loads(raw)['message'].encode())
        fixture['choices'][0]['message']['content']=fixture['choices'][0]['message']['content'].replace('Maya Chen','Mara Chen')
        self.store.register([(self.source.name,raw)],config)
        self.assertEqual(process(self.store,1),0)
        self.assertEqual(self.store.current()[0]['extraction']['requester'],'Mara Chen')
        self.assertEqual(self.store.history(self.source.name)[0]['extraction']['requester'],'Maya Chen')

    def test_policy_refresh_reprocesses_and_resume_keeps_queued_policy(self):
        from access_intake.core import make_config,process
        self.store.register([(self.source.name,self.source.read_bytes())],self.config)
        process(self.store,1)
        policy=self.path/'policy.md'
        policy.write_text((ROOT/'materials/office-policy.md').read_text()+'\nPolicy revision for provenance testing.\n')
        config=make_config('mock',mock_dir=FIXTURES,policy=policy)
        self.store.register([(self.source.name,self.source.read_bytes())],config)
        policy.write_text('Changed again after enqueue; resume must use saved text.')
        process(self.store,1)
        self.assertEqual(len(self.store.history(self.source.name)),2)
        self.assertEqual(self.store.current()[0]['policy_sha256'],config['policy_sha256'])

    def test_transient_retry_then_success_and_http_error_redaction(self):
        import io
        import urllib.error
        from access_intake.core import process,extract,ProviderError,make_config
        self.store.register([(self.source.name,self.source.read_bytes())],self.config)
        reply=extract(self.source.name,json.loads(self.source.read_text())['message'],self.config)
        with mock.patch('access_intake.core.extract',side_effect=[ProviderError('provider-http-429',True),reply]) as call:
            self.assertEqual(process(self.store,1),0)
            self.assertEqual(call.call_count,2)
        config=make_config('openai')
        error=urllib.error.HTTPError(config['api_url'],429,'secret-value',{'Retry-After':'3'},io.BytesIO(b'secret-value'))
        with mock.patch.dict(os.environ,{'API_KEY':'secret-value'}),mock.patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(ProviderError) as caught:
                extract(self.source.name,'message',config)
        self.assertEqual(str(caught.exception),'provider-http-429')
        self.assertTrue(caught.exception.transient)
        self.assertEqual(caught.exception.retry_after,3)

    def test_live_lease_and_reclaimed_worker_cannot_publish(self):
        self.store.register([(self.source.name,self.source.read_bytes())],self.config)
        row=self.store.claim('old',lease=120)
        self.assertIsNone(self.store.claim('new'))
        with self.store.connect(write=True) as db:
            db.execute('UPDATE attempts SET lease_until=0')
        reclaimed=self.store.claim('new')
        self.assertEqual(reclaimed['id'],row['id'])
        self.store.finish(row['id'],'old',{'status':'error','error':'stale'})
        self.assertEqual(self.store.current()[0]['status'],'processing')


if __name__ == "__main__":
    unittest.main()
