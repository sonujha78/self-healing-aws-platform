#!/usr/bin/python3
"""
Custom Ansible module: health_check

Checks an application's HTTP health endpoint and returns detailed,
structured status - response time, HTTP status code, and specific
fields extracted from the JSON payload (e.g. uptime_seconds, hostname).

Why not the built-in `uri` module?
The `uri` module can fetch a URL and check a status code, but it does not:
  - parse specific fields out of a JSON body and expose them as first-class
    return values that downstream tasks can key conditionals on
    (e.g. `when: health_result.uptime_seconds < 5` to detect a fresh/just-
    restarted, possibly-still-warming-up process)
  - compute and return a precise round-trip response_time_ms as a numeric
    fact usable directly in reporting/alerting logic
  - apply domain-specific "is this instance healthy" business rules
    (e.g. treat a non-200 OR a missing/invalid JSON body OR a payload
    missing a required field as unhealthy in one boolean) without
    stacking multiple `uri` + `set_fact` + `assert` tasks together

This module folds all of that into one atomic, testable unit with a
proper module contract (JSON in via stdin, JSON out with changed/failed),
which is exactly what the self-healing daemon (Part D) needs to make a
single reliable go/no-go decision per instance per check interval.
"""

import json
import time
import urllib.request
import urllib.error

from ansible.module_utils.basic import AnsibleModule


def run_module():
    module_args = dict(
        url=dict(type="str", required=True),
        timeout=dict(type="int", required=False, default=5),
        expected_status=dict(type="int", required=False, default=200),
        required_fields=dict(type="list", elements="str", required=False, default=["status"]),
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True,
    )

    url = module.params["url"]
    timeout = module.params["timeout"]
    expected_status = module.params["expected_status"]
    required_fields = module.params["required_fields"]

    result = {
        "changed": False,
        "url": url,
        "healthy": False,
        "http_code": None,
        "response_time_ms": None,
        "payload": {},
        "reason": None,
    }

    if module.check_mode:
        module.exit_json(**result)

    start = time.monotonic()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed_ms = round((time.monotonic() - start) * 1000, 2)
            body = resp.read().decode("utf-8", errors="replace")
            http_code = resp.getcode()

            result["response_time_ms"] = elapsed_ms
            result["http_code"] = http_code

            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                result["reason"] = "response body is not valid JSON"
                module.exit_json(**result)
                return

            result["payload"] = payload

            missing = [f for f in required_fields if f not in payload]
            if missing:
                result["reason"] = f"missing required fields in payload: {missing}"
                module.exit_json(**result)
                return

            if http_code != expected_status:
                result["reason"] = f"expected HTTP {expected_status}, got {http_code}"
                module.exit_json(**result)
                return

            result["healthy"] = True
            result["reason"] = "ok"
            module.exit_json(**result)

    except urllib.error.HTTPError as e:
        result["http_code"] = e.code
        result["response_time_ms"] = round((time.monotonic() - start) * 1000, 2)
        result["reason"] = f"HTTP error: {e.code} {e.reason}"
        module.exit_json(**result)

    except (urllib.error.URLError, TimeoutError) as e:
        result["response_time_ms"] = round((time.monotonic() - start) * 1000, 2)
        result["reason"] = f"connection failed: {e}"
        module.exit_json(**result)

    except Exception as e:
        module.fail_json(msg=f"unexpected error in health_check module: {e}", **result)


def main():
    run_module()


if __name__ == "__main__":
    main()
