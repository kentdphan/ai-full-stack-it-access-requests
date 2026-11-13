"""Run from repository root: python3 -m access_intake --help."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .core import ROOT, Store, make_config, check_config, process
from .reports import accuracy, evidence, export


def provider_options(parser,optional=False):
    parser.add_argument('--provider',choices=['mock','openai'],default=None if optional else 'mock')
    parser.add_argument('--model')
    parser.add_argument('--mock-dir',type=Path)
    parser.add_argument('--policy',type=Path)
    parser.add_argument('--schema',type=Path)
    parser.add_argument('--api-url')
    parser.add_argument('--timeout',type=float,default=None if optional else 45)
    parser.add_argument('--rpm',type=float)
    parser.add_argument('--retries',type=int,default=None if optional else 2)


def execution_options(parser):
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--enqueue-only',action='store_true')


def parser():
    root=argparse.ArgumentParser(description='Durable IT access-request intake and human review queue.')
    root.add_argument('--db',type=Path,default=Path('var/intake.sqlite3'),help='Shared local SQLite database (before subcommand).')
    sub=root.add_subparsers(dest='command',required=True)
    p=sub.add_parser('intake',help='Register new/updated JSON exports; run unfinished work.')
    p.add_argument('inputs',nargs='+',type=Path)
    p.add_argument('--force',action='store_true',help='Create fresh attempts even when settings/source match.')
    provider_options(p); execution_options(p)
    p=sub.add_parser('resume',help='Resume pending work and reclaim expired leases, using saved settings.')
    execution_options(p)
    p=sub.add_parser('retry',help='Retry a document using its stored source/settings; optional setting overrides.')
    p.add_argument('document_id'); provider_options(p,optional=True); execution_options(p)
    p=sub.add_parser('queue',help='Inspect current queue, including pending work and failures.')
    p.add_argument('--json',action='store_true'); p.add_argument('--status')
    p=sub.add_parser('history',help='Inspect every attempt for a document, oldest first.')
    p.add_argument('document_id')
    p=sub.add_parser('export',help='Write extractions.jsonl and queue.json from one database snapshot.')
    p.add_argument('--out',type=Path,default=Path('deliverables'))
    p=sub.add_parser('accuracy',help='Compare current results with independently authored expectations.')
    p.add_argument('--expected',type=Path,default=ROOT/'evaluation/expected.json')
    p.add_argument('--out',type=Path,default=Path('deliverables/accuracy.md'))
    p=sub.add_parser('evidence',help='Write redacted request example and response-handling details.')
    p.add_argument('--attempt',type=int,required=True)
    p.add_argument('--out',type=Path,default=Path('deliverables/request-evidence.json'))
    return root


def settings(args,old=None):
    names=('provider','model','mock_dir','policy','schema','timeout','rpm','retries','api_url')
    supplied={k:getattr(args,k) for k in names if getattr(args,k,None) is not None}
    if old is None: return make_config(**supplied)
    if not supplied: return old
    # Keep the original prompt, policy and schema for reproducible retries unless overridden.
    merged={**old}
    for field in ('model','provider','timeout','rpm','retries','api_url'):
        if field in supplied: merged[field]=supplied[field]
    if 'mock_dir' in supplied or merged['provider']!=old['provider']:
        fresh=make_config(provider=merged['provider'],mock_dir=supplied.get('mock_dir'))
        if 'mock_replies' in fresh: merged['mock_replies']=fresh['mock_replies']
        if 'rpm' not in supplied: merged['rpm']=fresh['rpm']
    if supplied.get('policy'):
        merged['policy']=Path(supplied['policy']).read_text(encoding='utf-8')
        from .core import digest
        merged['policy_sha256']=digest(merged['policy'].encode())
    if supplied.get('schema'):
        merged['schema']=json.loads(Path(supplied['schema']).read_text(encoding='utf-8'))
    check_config(merged)
    if merged['provider']!='mock': merged.pop('mock_replies',None)
    return merged


def main(argv=None):
    args=parser().parse_args(argv)
    if getattr(args,'workers',1)<1 or getattr(args,'workers',1)>32:
        parser().error('--workers must be between 1 and 32')
    try:
        store=Store(args.db)
        if args.command=='intake':
            paths=[]
            for path in args.inputs:
                if path.is_dir(): paths.extend(sorted(path.glob('*.json')))
                else: paths.append(path)
            if not paths: raise ValueError('no-json-inputs')
            if len({p.name for p in paths})!=len(paths): raise ValueError('duplicate-document-filenames-in-batch')
            sources=[(p.name,p.read_bytes()) for p in paths]
            attempts=store.register(sources,settings(args),force=args.force)
            print(f'Registered or reused {len(attempts)} documents.',file=sys.stderr)
        elif args.command=='retry':
            history=store.history(args.document_id)
            if not history: raise ValueError('unknown-document')
            old=store.settings(history[-1]['attempt_id'])
            store.retry(args.document_id,settings(args,old))
        elif args.command=='queue':
            rows=store.current()
            if args.status: rows=[r for r in rows if r['status']==args.status]
            if args.json: print(json.dumps(rows,indent=2))
            else:
                print('DOCUMENT\tATTEMPT\tSTATUS\tOWNER\tNEXT ACTION')
                for row in rows:
                    print('\t'.join(str(row[k]) for k in ('document_id','attempt_id','status','owner','next_action')))
                    if row['error']: print(f"  Error: {row['error']}\n  Retry: {row['retry_command']}")
            return 0
        elif args.command=='history':
            print(json.dumps(store.history(args.document_id),indent=2)); return 0
        elif args.command=='export':
            print(f'Exported {export(store,args.out)} current records to {args.out}.'); return 0
        elif args.command=='accuracy':
            result=accuracy(store,args.expected,args.out)
            print(f'Accuracy report: {args.out}'); return result
        elif args.command=='evidence':
            evidence(store,args.attempt,args.out); print(f'Request evidence: {args.out}'); return 0
        if args.enqueue_only: return 0
        return process(store,args.workers,lambda doc,status:print(f'{doc}: {status}',file=sys.stderr))
    except KeyboardInterrupt:
        print('Interrupted. Use resume; abruptly terminated workers become reclaimable after 120 seconds.',file=sys.stderr)
        return 130
    except (ValueError,OSError,sqlite3.Error):
        # Settings and filesystem exceptions can contain sensitive values; avoid raw tracebacks.
        print('Command failed: check paths, configuration and database availability. Run --help for usage.',file=sys.stderr)
        return 2


if __name__=='__main__': sys.exit(main())
