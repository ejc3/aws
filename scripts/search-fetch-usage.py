"""Poll the search and fetch providers' usage and balance APIs and publish CloudWatch metrics (search-fetch-usage.tf).

DeepSeek: GET /user/balance. ScrapingDog: GET /account. Browserbase: GET /v1/projects/{id}/usage, /v1/projects and
/v1/sessions?status=RUNNING. Exa has no balance endpoint, and its usage endpoint needs a team service key we do not
hold, so it is not polled.

Every metric goes to namespace SearchFetch with a Provider dimension, plus ProviderUp (1 or 0) per provider. Responses
are never logged: ScrapingDog's echoes the API key and the account's address. Only metric names and numbers are.
A provider that fails is reported as ProviderUp 0 and makes the run fail at the end, after the others are published,
so the Lambda Errors alarm fires.
"""
import json
import os
import urllib.parse
import urllib.request

import boto3

NAMESPACE = os.environ.get("METRIC_NAMESPACE", "SearchFetch")
TIMEOUT = 20


def _secret(client, secret_id):
    return json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])


def _get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "search-fetch-usage/1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _num(value):
    """A number from an int, float or numeric string; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def deepseek(key, get=_get_json):
    data = get("https://api.deepseek.com/user/balance", {"Authorization": f"Bearer {key}"})
    out = [("Available", 1.0 if data.get("is_available") else 0.0, {})]
    for info in data.get("balance_infos") or []:
        currency = str(info.get("currency") or "unknown")
        for field, name in (("total_balance", "BalanceTotal"), ("granted_balance", "BalanceGranted"), ("topped_up_balance", "BalanceToppedUp")):
            v = _num(info.get(field))
            if v is not None:
                out.append((name, v, {"Currency": currency}))
    return out


def scrapingdog(key, get=_get_json):
    data = get("https://api.scrapingdog.com/account?" + urllib.parse.urlencode({"api_key": key}))
    limit, used = _num(data.get("requestLimit")), _num(data.get("requestUsed"))
    out = []
    if limit is not None:
        out.append(("CreditsLimit", limit, {}))
    if used is not None:
        out.append(("CreditsUsed", used, {}))
    if limit is not None and used is not None:
        out.append(("CreditsRemaining", max(limit - used, 0.0), {}))
        if limit > 0:
            out.append(("CreditsRemainingFraction", max(limit - used, 0.0) / limit, {}))
    for field, name in (("threadCount", "ThreadsInFlight"), ("concurrency_limit", "ConcurrencyLimit"), ("validity", "ValidityDays")):
        v = _num(data.get(field))
        if v is not None:
            out.append((name, v, {}))
    if not out:
        raise ValueError("ScrapingDog /account returned none of the expected fields")
    return out


def browserbase(key, project_id, get=_get_json):
    h = {"X-BB-API-Key": key}
    usage = get(f"https://api.browserbase.com/v1/projects/{urllib.parse.quote(project_id)}/usage", h)
    out = []
    for field, name in (("browserMinutes", "BrowserMinutes"), ("proxyBytes", "ProxyBytes")):
        v = _num(usage.get(field))
        if v is not None:
            out.append((name, v, {}))
    projects = get("https://api.browserbase.com/v1/projects", h)
    for p in projects if isinstance(projects, list) else []:
        if p.get("id") == project_id and _num(p.get("concurrency")) is not None:
            out.append(("ConcurrencyLimit", _num(p["concurrency"]), {}))
    running = get("https://api.browserbase.com/v1/sessions?status=RUNNING", h)
    if isinstance(running, list):
        out.append(("SessionsRunning", float(len(running)), {}))
    if not out:
        raise ValueError("Browserbase returned none of the expected fields")
    return out


def _datum(provider, name, value, extra):
    dims = [{"Name": "Provider", "Value": provider}] + [{"Name": k, "Value": v} for k, v in extra.items()]
    return {"MetricName": name, "Dimensions": dims, "Value": value, "Unit": "None"}


def lambda_handler(event, context, sm=None, cw=None, fetchers=None):
    sm = sm or boto3.client("secretsmanager")
    cw = cw or boto3.client("cloudwatch")
    fetchers = fetchers or {
        "deepseek": lambda: deepseek(_secret(sm, os.environ["DEEPSEEK_SECRET_ID"])["DEEPSEEK_API_KEY"]),
        "scrapingdog": lambda: scrapingdog(_secret(sm, os.environ["SCRAPINGDOG_SECRET_ID"])["SCRAPINGDOG_API_KEY"]),
        "browserbase": lambda: (lambda s: browserbase(s["BROWSERBASE_API_KEY"], s["BROWSERBASE_PROJECT_ID"]))(
            _secret(sm, os.environ["BROWSERBASE_SECRET_ID"])),
    }
    data, failed = [], []
    for provider, fetch in fetchers.items():
        try:
            metrics = fetch()
            data.extend(_datum(provider, n, v, d) for n, v, d in metrics)
            data.append(_datum(provider, "ProviderUp", 1.0, {}))
            print(json.dumps({"provider": provider, "metrics": {n + "".join(f"[{v}]" for v in d.values()): v for n, v, d in metrics}}))
        except Exception as exc:  # report and carry on; the run fails at the end
            failed.append(provider)
            data.append(_datum(provider, "ProviderUp", 0.0, {}))
            print(json.dumps({"provider": provider, "error": type(exc).__name__}))
    for i in range(0, len(data), 20):
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=data[i:i + 20])
    if failed:
        raise RuntimeError("usage poll failed for: " + ", ".join(failed))
    return {"published": len(data)}
