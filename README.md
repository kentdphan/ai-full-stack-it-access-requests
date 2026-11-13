# Northstar IT access-request intake

A Python CLI turns exported messages into validated extractions and a persistent human review queue. SQLite stores current work and immutable source/settings snapshots for each attempt. Approval remains a human decision.

## Setup

Run from this repository's root with **Python 3.12** (tested with 3.12.3). No third-party packages or installation step are needed; `requirements.lock` records the dependency-free runtime. The repository and its schema/policy files must remain available.

```bash
python3 -m access_intake --help
```

`--db` is a global option and goes **before** the subcommand. Its default is `var/intake.sqlite3`. Use the same database path for all operators sharing an intake. Local runtime files are ignored by git.

## Reproducible example without credentials

```bash
python3 -m access_intake intake materials/requests evaluation/PARA-001.json --provider mock
python3 -m access_intake queue
python3 -m access_intake queue --json
python3 -m access_intake history IT-004.json
python3 -m access_intake export --out deliverables
python3 -m access_intake accuracy --out deliverables/accuracy.md
python3 -m access_intake evidence --attempt 1 --out deliverables/request-evidence.json
```

The first command processes the 20 supplied exports plus the authored paraphrase. Every mock result has `response_source: "mock"`. Fixtures contain a message hash and reject changed message text, rather than silently claiming to extract new facts. Changes to export metadata alone still produce a new source revision, with the same message facts. To experiment with new messages, author a matching fixture under a separate `--mock-dir` or use the provider integration.

`accuracy` compares all 21 documents against `evaluation/expected.json`. To evaluate only a subset, supply an expectations JSON with that subset via `--expected`. A missing document counts as failed comparisons. Expected facts are source-authored; the mocks are constructed from those facts. A perfect mock score checks plumbing and routing, not model intelligence.

## Provider configuration

Supported integration: OpenAI Chat Completions over HTTPS, strict JSON Schema responses, non-streaming. Default model: `gpt-4o-mini-2024-07-18`, temperature 0, maximum completion tokens 2500. A different `--model` must support these parameters and strict structured output; incompatible configurations become visible errors. API reference: [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) and [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat).

The facilitator supplies **`API_KEY`**, the only environment variable the application reads. Set it securely in your shell. The CLI does not auto-load `.env` files. Never commit the key. It appears only in the outgoing Authorization header, never in saved settings, request evidence or logs.

```bash
python3 -m access_intake --db var/provider.sqlite3 intake materials/requests evaluation/PARA-001.json --provider openai --model gpt-4o-mini-2024-07-18 --workers 4 --rpm 60 --timeout 45 --retries 2
python3 -m access_intake --db var/provider.sqlite3 queue --json
python3 -m access_intake --db var/provider.sqlite3 accuracy --out var/provider-accuracy.md
python3 -m access_intake --db var/provider.sqlite3 evidence --attempt 1 --out var/provider-request-evidence.json
```

Options include `--policy PATH`, `--schema PATH`, `--api-url HTTPS_URL`, `--mock-dir PATH`, `--model`, `--timeout SECONDS`, `--rpm REQUESTS_PER_MINUTE`, and `--retries N` (0–5 retries in addition to the first call). Configure the same rate and provider across operators using one database. `--rpm 0` disables throttling; mock mode defaults to 0, OpenAI to 60. Timeouts default to 45 seconds. The rate limiter spaces call starts across workers/processes using that database. HTTP 408/429/5xx and connection/timeouts get bounded exponential backoff; numeric Retry-After is honored up to 30 seconds. Final failure is saved. There is no unlimited automatic retry loop.

The committed outputs use mocks. No paid/provider calls were made to build or verify this implementation. Provider HTTP request construction and response handling are tested with a fake transport.

## Intake, progress and recovery

Register a batch without starting extraction, then process it:

```bash
python3 -m access_intake intake materials/requests --provider mock --enqueue-only
python3 -m access_intake resume --workers 4
```

While intake/resume is running, use another terminal:

```bash
python3 -m access_intake queue
python3 -m access_intake queue --status error --json
python3 -m access_intake history IT-001.json
```

`queue` reads the database directly; completed requests are visible immediately alongside `pending` and `processing` work. Failed rows include a reproducible `retry_command`. Retry uses the original bytes and saved settings, even when the input file was moved or edited:

```bash
python3 -m access_intake retry IT-001.json
python3 -m access_intake retry IT-001.json --mock-dir tests/mock_responses
```

Retry optionally overrides provider settings. It refuses a pending/running document: use `resume` for those. `resume` processes pending work and reclaims leases that expired after a crash. Leases last 120 seconds and renew every 10 seconds while the process lives. After a forced stop, wait up to 120 seconds before resuming. A late response from a worker whose lease was reclaimed cannot publish. External calls are at least once: a crash after the provider response but before saving may cause a repeated call and cost.

Exit codes: **0** command finished (or enqueue succeeded); **1** at least one current record is in error (or accuracy comparisons failed); **2** invalid command/configuration/filesystem/database, or processing remains active/pending elsewhere; **130** keyboard interruption. Processing exit status summarizes the entire current queue, including earlier errors outside this batch. `needs-information` is a successful extraction and exits 0. Inspect `queue` to distinguish active work from errors.

## Revisions, settings and shared operation

The filename is `document_id`; the ID in the message remains `request_id`. Duplicate filenames within a batch are rejected. Use a stable filename for revisions; renaming a file creates a separate document. Each source hash is SHA-256 of the original JSON bytes, not reserialized JSON. Missing files in a later batch do not withdraw existing requests.

An unchanged source/settings pair reuses its current attempt, including an error; explicitly retry errors. Changed bytes or relevant settings create a new attempt. `--force` creates a new attempt even when unchanged. Source bytes, policy, prompt, schema, model/provider settings and mock fixtures are saved per attempt. History remains separate from current output and never becomes a fallback current result after a failed retry. The current queue immediately points to the newly registered attempt. An older job finishing later cannot replace it. Newly registered pending revisions supersede older pending work.

Rerun `intake` with changed settings to refresh affected inputs. Policy/schema/prompt changes and model configuration changes alter the settings hash. Changing any mock fixture conservatively refreshes the whole mock configuration. `resume` and an unmodified `retry` use their saved settings, not edited files. Policy routing is implemented explicitly in `access_intake/core.py:route`: changing policy rules requires updating that function and tests (and bumping `implementation_version`), then rerunning intake. Editing prose alone does not install new executable rules. The flat schema validator supports the supplied schema; introducing nested schemas requires code changes.

SQLite transactions serialize registration, claims and publication; WAL allows queue reads during processing. Use **one host with a local filesystem** and one shared database. Network-mounted databases and multi-host coordination are outside the supported configuration. Overlapping operators can safely intake/resume. Simultaneously supplied different revisions are ordered by registration transaction, since exports provide no authoritative revision sequence. The last registered revision wins; operators must submit revisions in intended source order. Threads are bounded (default 4, maximum 32); each message failure is isolated.

The expected workload is 200–400 messages/day, with bursts around 2,000. Intake registration happens as a batch, then extraction is per message. At 60 call starts/minute, 2,000 provider calls need at least about 33 minutes, plus latency and retries. Throughput/load at 2,000 messages, real provider quotas and multi-host behavior have not been benchmarked. Sources and configuration snapshots are retained without pruning; provision disk space and back up the database while preserving SQLite WAL consistency.

## Exports and verification

`export` writes one current extraction record per document and a queue object containing `snapshot_id` and `requests`. Each queue entry links by `document_id`, `attempt_id` and source/settings hashes. Pending work is exportable and explicitly labeled. Each file is atomically replaced, and concurrent exporters for the same database are serialized. A crash between the two replacements can leave different snapshot IDs: rerun export before consuming the pair. SQLite is authoritative; files are reporting artifacts. Never export different databases concurrently into the same directory.

```bash
env -u API_KEY python3 -m unittest discover -s tests -v
python3 -m access_intake accuracy --expected evaluation/expected.json --out deliverables/accuracy.md
```

Tests use saved mock replies and fake HTTP transport, with no network or actual key. They cover all required message types, exact evidence/schema validation, changes and corrections, failures/refusals/truncation, retries, concurrency, interruptions, settings refresh and progress. See `deliverables/verification.md` for assertions and limits. The selected exact business-purpose excerpt is intentionally stricter than semantic equivalence; human review is needed for factual interpretation, conflict handling and novel prompt injection attempts. Injection detection combines the extraction prompt and model-reported issues with conservative source-pattern checks; it is not a proof against all hostile messages.

Keep `CHALLENGE.md` as the assignment brief. This implementation does not record human approval decisions, grant access, or contact reviewers. Follow the facilitator's session-capture/submission instructions when committing and pushing to the default branch.
