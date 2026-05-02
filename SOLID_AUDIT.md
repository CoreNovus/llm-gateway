# SOLID Audit — `llm-gateway`

> Companion to [`README.md`](README.md). The README explains what the
> gateway *does*; this doc explains how the package *holds together*
> against the SOLID principles, so a contributor reading the source
> can match every pattern they see to the principle it serves. Same
> teaching shape used elsewhere in the project: principle → trap →
> clean example in this codebase → any dirty finding → accepted
> trade-off.

The headline result of this audit pass: **no P1 or P2 violations
found**. The package was designed against the principles from the
start (the source docstrings cite SRP / DIP / ISP / OCP by name in
several places). What follows is the evidence, plus the small set of
observations and accepted trade-offs that need to be on the record so
they don't drift.

---

## 0. Scope and method

What was audited (everything under `llm_gateway/`):

| Area | Files | What lives there |
|------|-------|------------------|
| Composition root | `app.py`, `__main__.py`, `config.py` | `create_app(settings, backend)` factory, production wiring, pydantic-settings |
| HTTP surface | `api/{chat_completions,health,models_listing,schemas}.py` | OpenAI-compatible endpoints, request schemas |
| Inference layer | `inference/{base,vllm_http,circuit_breaker,noop,errors}.py` | `InferenceBackend` Protocol + concrete backends + decorator + typed errors |
| Middleware | `middleware/{auth,rate_limit,body_limit,logging,metrics,correlation}.py` | Bearer auth, token-bucket rate-limit, body cap, structured logging, per-request metrics |
| Observability | `observability/{metrics,router}.py` | `Metrics` container + `/metrics` endpoint |
| Domain helpers | `models/registry.py`, `tool_calling/{parsers,validation}.py` | Read-only model registry, tool-call parser enum, pre-flight tool validator |

The **trade-off lens** (consistent with how SOLID is applied across
the wider project): a violation is only a real problem if the change
it doesn't support is a change you actually need.

- *Pure ISP* (every interface has one method) is rarely correct. Fat
  interfaces are fine when callers genuinely consume the whole
  surface. The smell is implementations forced to stub out methods
  they never use.
- *Pure OCP* (close everything to modification) is impossible. The
  line is "does adding a known kind of thing require editing
  unrelated code?".
- *DI everywhere* (every dependency abstracted) burns context for no
  gain. Inject across module / process / process-cluster boundaries
  (transport, model provider, store). Don't inject the dataclass you
  constructed two lines ago.

A finding only goes into §6 if it crosses that bar.

---

## 1. S — Single Responsibility

> *A class has one reason to change.* If two independent change-drivers
> ("the upstream HTTP contract changed" and "the failure-budget policy
> changed") both edit the same file, that file has two responsibilities.

**Trap**: "this class has too many methods" is not an SRP violation.
`pathlib.Path` has dozens of methods all serving one responsibility
(filesystem path manipulation). SRP is about *change drivers*, not
method count.

### Clean examples

- `inference/vllm_http.py:VLLMHTTPBackend` — one responsibility:
  speak OpenAI-format HTTP to a local vLLM container. Owns nothing
  else; the circuit breaker is a separate Decorator. Adding HTTP
  retry semantics would edit this file; adding fail-fast policy
  would edit `circuit_breaker.py` only.
- `inference/circuit_breaker.py:CircuitBreakerBackend` — one
  responsibility: convert "consecutive upstream failures" into
  "fail fast for `cooldown_s`". The file's docstring lists the design
  notes (decorator pattern, what counts toward the failure budget,
  why `ping()` always probes). Adding a new failure policy would
  add a *new* decorator; this one doesn't grow.
- `observability/metrics.py:Metrics` — every gateway metric in one
  container. The class's own docstring spells out *why* it's not
  a module-level singleton: per-instance registry for tests,
  constructor-injection for DIP, a single grep target for "what
  does the gateway expose?". Three reasons, one class.
- `tool_calling/validation.py:validate_tool_request` — function-level
  SRP: "given a request body and resolved model, decide if the tool
  combination is safe to forward". The docstring tells future
  contributors to add new checks as separate predicates.

### Dirty examples

None at this audit pass. The package is small (~30 source files),
each file's docstring states its responsibility in one sentence, and
nothing currently bundles unrelated change-drivers.

---

## 2. O — Open / Closed

> *Open for extension, closed for modification.* Adding a new variant
> of a known kind (a new inference backend, a new model, a new
> middleware) should be new files + a registration call — not edits
> to existing consumers.

**Trap**: OCP is not "never edit anything". The framework itself
changes; that's fine. What can't change is *every caller / sibling
/ consumer* when you add a variant.

### Clean examples

- `inference/base.py:InferenceBackend` (Protocol) +
  `app.py:create_app(backend=…)` — adding a new backend (e.g. a
  Bedrock proxy, an Ollama bridge) is one new module implementing
  four async methods + one `create_app` argument. The API layer
  never imports concrete backend classes; FastAPI `Depends`
  resolves the backend dependency through the Protocol.
- `models/registry.py:DEFAULT_REGISTRY` — `MappingProxyType` over a
  literal dict. Adding a model = one new `ModelDefinition` literal
  + one entry. Existing entries don't change; consumers look up by
  `served_name` so caller sites are unaffected. The dict is
  read-only at runtime so a future bug cannot mutate the registry.
- `tool_calling/parsers.py:ToolCallParser` — enum closed against
  arbitrary string typos. Adding a new vLLM parser family is one
  new enum member; the registry's typo-at-import-time guarantee
  catches mistakes before production.

### Accepted trade-off

- `api/schemas.py:ChatCompletionRequest` uses
  `model_config = ConfigDict(extra="allow")`. On a quick scan this
  *looks* like a strict-typing violation. It is deliberate OCP: vLLM
  forwards OpenAI fields the gateway does not gate (`logprobs`,
  `frequency_penalty`, `response_format`, …). Modeling every one
  would bind the package to a specific OpenAI version — every new
  field would be a code change. The schema validates only what the
  gateway *uses for routing decisions* (`model`, `stream`, `tools`),
  and lets the rest pass through. Documented inline at L9-13 of
  `schemas.py`.

---

## 3. L — Liskov Substitution

> *Subclasses (or Protocol implementations) must be substitutable for
> the base type without breaking callers.* Same arguments, same return
> shape, same exception types, same observable behaviour on edge
> cases.

**Trap**: Liskov is not just polymorphism. It includes the *error
contract*. If `BaseFoo.do()` is documented to raise `FooError` and
one implementation raises `ValueError`, every caller has to widen
its `except` clause and the abstraction has leaked.

### Clean examples

- `inference/errors.py` — single `InferenceError` ancestor with two
  semantically distinct subclasses (`UpstreamUnavailableError`,
  `UpstreamClientError`). Every concrete backend
  (`VLLMHTTPBackend`, `NoopBackend`, `CircuitBreakerBackend`) raises
  these and only these for the documented conditions. Callers
  (`api/chat_completions.py`) `except UpstreamUnavailableError` /
  `UpstreamClientError` exhaustively without per-implementation
  branches; everything else falls through to a generic 500 logged
  by `RequestLoggingMiddleware`.
- `inference/circuit_breaker.py:CircuitBreakerBackend` is a perfect
  Liskov example: it IS-A `InferenceBackend`, swappable into any
  position the bare `VLLMHTTPBackend` occupies, and respects every
  error-contract clause of the base Protocol — including the
  decision NOT to count `UpstreamClientError` toward the failure
  budget (4xx is the caller's fault and breaking the circuit on the
  engine's behalf would violate the contract).
- `inference/noop.py:NoopBackend` is a frozen dataclass implementing
  the same Protocol; the test suite swaps it in for `VLLMHTTPBackend`
  and the surrounding code does not branch on type.

### Dirty examples

None.

---

## 4. I — Interface Segregation

> *Clients shouldn't depend on interface methods they don't use.* A
> fat abstract base that forces every subclass to implement methods
> their workload never touches is an ISP violation.

**Trap**: "split every interface into one-method protocols" makes
the call graph unreadable. ISP is satisfied when each implementation
pays only for the surface it cares about — Template Method, default
implementations, and free functions are all valid solutions.

### Clean examples

- `inference/base.py:InferenceBackend` — four methods (`ping`,
  `complete`, `stream`, `aclose`). The `complete` / `stream` split
  is the canonical worked example of ISP: non-streaming callers do
  not import streaming machinery, and the docstring at L46-48 cites
  ISP by name as the rationale. `aclose` is a no-op for
  state-less backends (see `NoopBackend`) so they pay no
  implementation cost.
- `middleware/rate_limit.py:RateLimiter` (Protocol) — one async
  method (`check`). A future Redis-backed limiter implements only
  that, no transport / metric / TTL methods bolted on. Today's
  `InMemoryTokenBucket` exposes private helpers (`_evict_idle`,
  `_upsert`) for its own implementation and *only* the Protocol
  surface to callers.

### Dirty examples

None.

---

## 5. D — Dependency Inversion

> *High-level modules should not depend on low-level modules; both
> should depend on abstractions.* Inject across module / process
> boundaries (transport, model provider, store), not at function-call
> boundaries inside one module.

**Trap**: every dependency abstracted behind a Protocol is both
costly (boilerplate) and often wrong. A dataclass you constructed
two lines above doesn't need a Protocol.

### Clean examples

- `app.py:create_app(settings, backend, *, rate_limiter=None, metrics=None)` —
  the package's composition root. Pure function, no module-level
  globals, accepts the four boundary dependencies (settings,
  inference backend, rate limiter, metrics) by argument. Tests
  construct an app per case with `NoopBackend`; production wires
  `CircuitBreakerBackend(VLLMHTTPBackend(...))`. The factory's
  comment at L83-85 cites DIP explicitly.
- `__main__.py:_build_backend(settings, metrics)` — the production
  composition is itself a pure function so tests can call it
  without booting uvicorn. The circuit breaker's
  `on_state_change` callback closes over `metrics.circuit_breaker_state`
  so the gauge mirrors current breaker state without coupling
  `CircuitBreakerBackend` to Prometheus.
- `middleware/auth.py:BearerAuthMiddleware`,
  `middleware/body_limit.py:BodySizeLimitMiddleware`,
  `middleware/metrics.py:MetricsMiddleware` — every middleware takes
  its dependencies (token, byte cap, `Metrics` instance) by
  constructor injection. None reads environment variables. None
  imports a module-level singleton. Reconfiguring is a single
  argument change at `create_app` time.

### Dirty examples

None.

---

## 6. Findings summary

| # | Module | Principle | Severity | Status |
|---|--------|-----------|----------|--------|
| — | — | — | — | **No P1 or P2 violations found.** |

The audit pass walked all source files in `llm_gateway/`. Every
class/function either has one obvious reason to change, sits behind
a Protocol or registry that absorbs new variants without source
edits, respects its declared error contract, or is small enough that
ISP / DIP do not apply (free functions, frozen dataclasses).

---

## 7. Minor observations (not violations)

These are worth noting so they don't drift over time, but none meets
the trade-off-lens bar in §0.

### 7.1 `_DEFAULT_SKIP_PATHS` is repeated across four middleware classes

`("/metrics", "/health", "/ready")` appears as `_DEFAULT_SKIP_PATHS`
on `BearerAuthMiddleware`, `RateLimitMiddleware`,
`BodySizeLimitMiddleware`, and `MetricsMiddleware`. Looks like a
DRY smell.

It is **incidental duplication** rather than structural: each
middleware owns its skip-paths concern (auth ≠ rate-limit ≠
metrics-recording semantically), and a shared constant would couple
the four together — the day one of them needs different skip paths
(say, only `/health` and `/ready`, not `/metrics`) the shared
abstraction breaks. The duplication cost (one tuple per file) is
lower than the coupling cost. Keep separate. If a fifth middleware
ever joins, re-evaluate.

### 7.2 `BearerAuthMiddleware` docstring vs. `_DEFAULT_SKIP_PATHS` — fixed in this pass

The module docstring at `auth.py:12-13` previously listed only
`/health` and `/ready` as skip paths; the `_DEFAULT_SKIP_PATHS`
tuple includes `/metrics` too (correct — a Prometheus scraper needs
to bypass auth so it does not need a bearer token). Fixed in this
audit pass: the docstring now enumerates all three skip paths and
notes the `127.0.0.1`-bind + SSH-tunnel boundary that already gates
network access to those endpoints. Recorded here as the canonical
example of "doc-drift surfaced by audit, fixed in the same pass" so
future audits know what kind of finding belongs in §7 vs. §6.

### 7.3 `chat_completions.py` accepts `metrics=None`

`make_chat_completions_router(backend_dep, metrics=None)` accepts an
optional `Metrics`. Production wires a real instance from
`create_app`; tests that don't care about token-usage instrumentation
pass `None`. Defensive typing rather than a DIP smell — the function
gates the metric calls with `if metrics is not None` so the dependency
is genuinely optional.

---

## 8. Accepted trade-offs (recap)

A flat list so they're easy to find later. Each is also documented
in the source.

| Choice | Where | Why it's not a violation |
|--------|-------|--------------------------|
| `ChatCompletionRequest(extra="allow")` | `api/schemas.py:11-13, 39` | Validates only what we route on; lets unknown OpenAI fields pass through. Modeling every field would bind us to one OpenAI version. |
| Dev-mode auth bypass on empty `bearer_token` | `middleware/auth.py:49-52`, `__main__.py:_require_bearer_token` | Tests + local NoopBackend don't need a token; production entry refuses to start with empty token, so the bypass is unreachable in deploy. |
| `chat_completions.py` does not model the *response* shape | `api/schemas.py:6-8` | vLLM is the source of truth for response shape; modeling it would bind us to one OpenAI version (and require updates every time vLLM passes a new field). |
| Per-request `Metrics` is constructor-injected, not a global | `observability/metrics.py:5-15` | Tests get fresh registries per case; production wires once at `create_app`. Same DIP rationale as the inference backend. |
| `circuit_breaker.py:ping()` always probes inner backend | `inference/circuit_breaker.py:88-91` | Readiness must reflect actual reachability, not historical breaker state. A cached ping would lie. |

---

## 9. How to keep this audit honest

This file goes stale the moment someone lands a new module without
re-reading it. The maintenance contract:

1. **Adding a new module** — re-read each principle here; either
   add a new clean example in §1–§5 if the module demonstrates one,
   or open §6 with a finding if it violates one. "I added X but
   didn't update the audit" is a code-review red flag.
2. **Adding a new accepted trade-off** — record in §8 with the
   source citation. "Looks like a violation but isn't" without a
   recorded rationale will get re-flagged in the next audit pass.
3. **Promoting a finding** — if §7 (minor observations) escalates
   into something the trade-off lens bites on, move it to §6 with
   a severity tag. Don't quietly leave it in §7 once a third caller
   is involved.
4. **Removing a clean example** — if a refactor breaks one of the
   §1–§5 cited patterns, update both the source and this doc in
   the same PR. Stale citations are worse than missing citations.

The audit earns its keep by being maintained, not by being thorough
once.
