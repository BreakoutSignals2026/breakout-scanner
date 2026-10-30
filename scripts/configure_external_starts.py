#!/usr/bin/env python3
"""Create/update the three external 09:20 NY dispatch jobs without logging keys.

Run locally, not in GitHub Actions. Tokens are read from environment or getpass.
Without --apply, prints a redacted reviewable configuration and makes no requests.
"""
import argparse
import getpass
import json
import os
import time
import urllib.error
import urllib.request

REPO = "BreakoutSignals2026/breakout-scanner"
WORKFLOWS = ("scanner.yml", "fast_scanner.yml", "fast_micro_scanner.yml")


def job(workflow, github_token):
    return {"title": f"BreakoutSignals2026 {workflow} 09:20 NY",
            "url": f"https://api.github.com/repos/{REPO}/actions/workflows/{workflow}/dispatches",
            "enabled": True, "saveResponses": False, "requestMethod": 1,
            "requestTimeout": 30, "redirectSuccess": False,
            "notification": {"onFailure": False, "onSuccess": False,
                             "onDisable": False, "onSslCertExpiry": False},
            "schedule": {"timezone": "America/New_York", "expiresAt": 0,
                         "hours": [9], "minutes": [20, 25, 30],
                         "mdays": [-1], "months": [-1], "wdays": [1,2,3,4,5]},
            "extendedData": {"headers": {
                "Authorization": f"Bearer {github_token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json"},
                "body": json.dumps({"ref": "main"})}}


def request(method, path, token, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request("https://api.cron-job.org"+path, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as result:
            return json.load(result)
    except urllib.error.HTTPError as exc:
        # API error bodies can contain submitted headers; never print them.
        raise RuntimeError(f"cron-job.org {method} {path}: HTTP {exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("cron-job.org network request failed") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create/update external jobs")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps([job(w, "REDACTED") for w in WORKFLOWS], indent=2))
        return
    token = os.getenv("GITHUB_WORKFLOW_TOKEN") or getpass.getpass("GitHub token (Actions write, this repo only): ")
    cronkey = os.getenv("CRONJOB_API_KEY") or getpass.getpass("cron-job.org API key: ")
    if not token.strip() or not cronkey.strip():
        raise SystemExit("Both tokens are required; nothing changed")
    existing = request("GET", "/jobs", cronkey)
    if existing.get("someFailed"):
        raise SystemExit("Incomplete job inventory; refusing to create duplicates")
    for workflow in WORKFLOWS:
        desired = job(workflow, token.strip())
        matches = [x for x in existing.get("jobs", []) if x.get("title")==desired["title"]]
        if len(matches)>1:
            raise SystemExit(f"Duplicate external jobs for {workflow}; inspect the console")
        if matches:
            job_id = matches[0]["jobId"]
            request("PATCH", f"/jobs/{job_id}", cronkey, {"job": desired})
        else:
            job_id = request("PUT", "/jobs", cronkey, {"job": desired})["jobId"]
        print(f"Configured {workflow}: external job {job_id}; 09:20 NY + 09:25/09:30 retries")
        time.sleep(1.1)  # Respect create API's one-request-per-second limit.


if __name__ == "__main__":
    main()
