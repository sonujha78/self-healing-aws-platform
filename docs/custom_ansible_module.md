# Custom Ansible Module: `health_check`

## Why not the built-in `uri` module?

The built-in `uri` module can fetch a URL and check a status code, but for
this project's self-healing use case it falls short in three concrete ways:

1. **No first-class extraction of payload fields.** `uri` returns the raw
   response body as a string (or under `.json` if you're lucky with content
   type). To act on specific fields like `uptime_seconds` or `hostname`,
   you'd need a separate `set_fact` + Jinja filter step after every `uri`
   call, duplicated across every health-check task in the codebase.

2. **No single "healthy" verdict.** Determining whether an instance is
   truly healthy requires combining several conditions — correct HTTP
   status, valid JSON, presence of required fields. With `uri`, this means
   chaining `uri` + `assert` + `set_fact` tasks together for every check,
   which is exactly the "shell-command-wrapper with extra steps" pattern
   this project's spec explicitly asked to avoid.

3. **No precise, reusable response-time fact.** `uri` does not expose a
   clean round-trip response time as a numeric fact that can be logged or
   used directly in alerting/reporting logic (e.g. by Part C's CloudWatch
   push script or Part D's self-healing daemon).

## What `health_check` adds

`health_check` is a real, hand-written Ansible module with a proper module
contract: it accepts its arguments via Ansible's standard JSON-over-stdin
mechanism (through `AnsibleModule`), and returns a single structured JSON
result containing `changed`/`failed` status plus:

- `healthy` — a single boolean verdict combining HTTP status, JSON
  validity, and required-field presence
- `http_code` — the actual HTTP status returned
- `response_time_ms` — precise round-trip timing
- `payload` — the parsed JSON body, so downstream tasks can key off any
  field returned by the app (e.g. `health_result.payload.uptime_seconds`)
- `reason` — a short human-readable explanation when unhealthy (used
  directly in the self-healing daemon's structured logs)

This turns "is this instance healthy" into one reliable, testable task
instead of a multi-task chain repeated across every playbook that needs
a health check — which is exactly what Part D's self-healing daemon needs
to make a single go/no-go decision per instance per check interval.

## Interview-ready summary

> "I wrote a custom Ansible module because the built-in `uri` module can
> hit an endpoint and check a status code, but can't extract specific
> fields from a JSON payload as first-class Ansible facts, can't combine
> multiple health conditions into one boolean verdict, and doesn't expose
> a clean response-time metric. My module wraps all of that into a single
> atomic module call with a proper JSON-in/JSON-out contract, which the
> self-healing daemon relies on to make health decisions."
