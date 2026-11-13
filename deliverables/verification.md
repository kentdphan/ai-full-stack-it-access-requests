# Verification

## Commands

Run from the repository root with Python 3.12.3:

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
python3 -m access_intake --db var/intake.sqlite3 intake materials/requests evaluation/PARA-001.json --provider mock
python3 -m access_intake --db var/intake.sqlite3 queue --json
python3 -m access_intake --db var/intake.sqlite3 export --out deliverables
python3 -m access_intake --db var/intake.sqlite3 accuracy --out deliverables/accuracy.md
python3 -m access_intake --db var/intake.sqlite3 evidence --attempt 1 --out deliverables/request-evidence.json
```

The test suite creates isolated temporary databases and uses saved replies or an in-memory fake HTTP transport. It does not need a key or network. The example intake includes 20 supplied files and one authored paraphrase. `accuracy.md` explains the 315-comparison denominator and contains the independently source-authored expectations and configuration. The mocks are built from the expectations; the mock score is not evidence of model extraction ability.

## Assertions and intended comparisons

| Test | Assertion and comparison |
|---|---|
| `CLI.test_complete_batch_and_source_derived_routes` | Compare all 12 extracted scalar fields and all three routing fields against `evaluation/expected.json` for all 21 messages. Cover complete requests, missing facts, conflicting environment/manager, instruction override, withdrawal, explicit corrections, change permissions, relative/reversed dates and identifier mismatch. Check export snapshot agreement and 315/315 mock comparisons. |
| `CLI.test_unchanged_reuse_changed_bytes_and_batch_absence` | Same source/settings produces no extra attempt; changed original bytes produces a new SHA-256 and retains history. A later partial batch does not remove earlier documents. |
| `CLI.test_failure_refusal_interruption_and_failed_retry_preserve_history` | Saved provider error/refusal/truncated/malformed replies produce visible current error/null extraction and retry commands. Initial success survives in history; later successful retry becomes current. |
| `CLI.test_enqueue_resume_and_expired_lease` | Enqueue shows pending; simulate a crashed worker by expiring its persisted lease, then resume to completion without an extra attempt. |
| `CLI.test_two_operators_and_stale_completion` | Run two independent CLI processes against one SQLite database; ensure one current entry and no duplicate attempt per unchanged document. Finish an older running extraction after registering a newer revision; ensure the newer pending attempt remains current. |
| `CLI.test_invalid_input_is_isolated_and_retry_uses_stored_source` | Invalid JSON fails one record while a valid document completes. A retry uses saved source and preserves the global error exit code while another document remains failed. |
| `CLI.test_settings_refresh_and_accuracy_missing_record_fails` | Model configuration changes create new attempts despite unchanged bytes; missing expected records cause a nonzero accuracy result. |
| `ProviderBoundary.test_real_request_and_transport_without_key_or_network` | Construct strict schema request, keep source data in user role, use a fake credential/transport, inspect Authorization header and returned model provenance. No actual provider call. |
| `ProviderBoundary.test_schema_evidence_and_dates_rejected` | Reject unknown properties, invalid permission enums, fabricated supporting quotes and non-ISO dates. |
| `Operations.test_slow_message_leaves_other_completed_work_inspectable` | Block one extraction while another finishes; read the database mid-run and observe processing plus completed review work. |
| `Operations.test_changed_message_uses_new_facts_and_old_source_remains_in_history` | Change requester explicitly in source and the authored canned reply; current shows Mara Chen while history retains Maya Chen. |
| `Operations.test_policy_refresh_reprocesses_and_resume_keeps_queued_policy` | Changed policy text creates new provenance/attempt; editing the policy again after enqueue does not change the queued settings. |
| `Operations.test_transient_retry_then_success_and_http_error_redaction` | Simulate transient 429 then success within a bounded attempt. Fake HTTP 429 exposes a safe error code and numeric Retry-After, without exposing secret error bodies. |
| `Operations.test_live_lease_and_reclaimed_worker_cannot_publish` | Active leases cannot be claimed twice; expire/reclaim the lease and reject publication by the stale worker token. |
| Three starter tests | Supplied export count/hashability, schema field presence and a saved reply without network/key. |

## Observations and repairs

These are development observations, not independent human validation or live provider observations.

- Tests were authored before the application. The initial run failed because the module/CLI did not yet exist (7 failures and 2 errors; the three starter tests passed).
- The first integrated run found two mismatches in test expectations: a retry of one document still correctly returns exit 1 if another current document remains in error, and one concurrent operator can return exit 2 while another is finishing work. The assertions were corrected to reflect the documented global queue exit semantics.
- The synthetic document was named `PARA-001.json` to match its stated ID, keeping it a clean paraphrase rather than introducing an identifier mismatch.
- The final test results are recorded in `deliverables/test-run.txt`.
- The committed mock example has 21 successful extractions: 9 need clarification and 12 await other human review actions. Routing owners: 11 IT Service Desk, 5 Security Review, 5 System Owner. No automatic approvals.
- The source-authored exact comparison produces 315/315 on mocks. This checks fixture fidelity and integration, not live model accuracy. No live provider observations were collected.

## Operating assumptions and verification limits

One host, local SQLite database shared by operators, WAL and transactional writes. Input revisions are ordered by registration; filenames identify documents and absence in a new batch does not delete documents. Workers save each completion independently. Pending work and expired leases resume; a provider call may repeat after a crash. Lease renewal is 10 seconds, expiry is 120 seconds. Late workers cannot replace current revisions or publish after their claim was reassigned.

Settings snapshots include policy/schema/prompt, provider/model, request parameters, timeouts/retries/rate and mock fixtures. Unchanged current results are reused; changes refresh records; previous attempts remain in SQLite. Arbitrary new policy rules require updating deterministic routing and tests, then refreshing intake. The flat validator supports the supplied schema, not a general JSON Schema implementation.

The default four workers and a configurable shared rate limiter address variable latency and quotas. Daily 200–400 messages and spikes of 2,000 are design assumptions. No 2,000-message load benchmark, real provider throughput/rate-limit experiment, multi-host/network-filesystem test or exhaustive crash-at-every-write experiment was performed. Worker death is tested by persisted lease manipulation; graceful Ctrl+C and OS-level power loss are not separately automated. Live provider semantics/accuracy require an API_KEY and separately labeled observations. Pattern checks and model issues do not prove detection of every prompt injection or semantic contradiction.

SQLite is authoritative. Exports are atomically replaced per file and carry matching snapshot IDs; an interruption between files requires re-export. Backups, retention/pruning and distributed deployment are operator responsibilities outside this implementation. Human review is still required for factual ambiguity and all access decisions. This file is an engineering report; the facilitator's classroom validation and session-capture/submission procedure remain human steps.
