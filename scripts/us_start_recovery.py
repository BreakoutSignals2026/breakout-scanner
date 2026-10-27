#!/usr/bin/env python3
"""Guarded, GitHub-native recovery for missed US session workflow starts.

Run only from the trusted default-branch GitHub Actions watchdog. No broker
orders in this file: it may dispatch the repository's existing, separately
guarded PAPER workflow with its standard full_paper_session input.
A missed cron is not itself a trading signal; account risk/strategy stays in
the existing scanner. Never bypass account-specific order safeguards.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time
from zoneinfo import ZoneInfo

NY=ZoneInfo("America/New_York")
API="https://api.github.com"
BROKER="https://paper-api.alpaca.markets/v2/calendar"
TARGETS={
    "fast_scanner.yml":{"full_paper_session":"true"},
    "scanner.yml":{"full_paper_session":"true"},
    "fast_micro_scanner.yml":{},
    "orb_scanner.yml":{},
}
START=time(9,40)
END=time(12,30)


def http_json(method,url,payload=None,headers=None):
    hdr={"User-Agent":"breakout-2026-safe-startup-watchdog",
         "Accept":"application/vnd.github+json"}
    hdr.update(headers or {})
    if payload is not None:
        hdr["Content-Type"]="application/json"
        body=json.dumps(payload).encode("utf-8")
    else:
        body=None
    request=urllib.request.Request(url,headers=hdr,method=method,data=body)
    with urllib.request.urlopen(request,timeout=18) as response:
        data=response.read()
        return json.loads(data) if data else None


def check_exchange_open(now,get=http_json):
    key=os.environ.get("ALPACA_API_KEY","").strip()
    sec=os.environ.get("ALPACA_SECRET_KEY","").strip()
    if not key or not sec:
        raise RuntimeError("Missing PAPER market calendar keys; recovery disabled")
    day=now.date().isoformat()
    url=BROKER+"?"+urllib.parse.urlencode({"start":day,"end":day})
    rows=get("GET",url,headers={"APCA-API-KEY-ID":key,"APCA-API-SECRET-KEY":sec})
    if not isinstance(rows,list):
        raise RuntimeError("Invalid Alpaca calendar payload; no dispatch")
    if not any(row.get("date")==day for row in rows):
        print("US calendar holiday; NO recovery",flush=True)
        return False
    return True


def safe_window(now):
    if now.weekday()>=5:
        return False
    return START<=now.time()<END


def run_has_started(run,jobs):
    if run.get("status") in ("queued","pending","waiting","in_progress","requested"):
        return True
    # Green 'Success' is insufficient; ORB previously skipped all work.
    for job in jobs:
        if job.get("name") in ("first-session","second-session") and (
             job.get("status") in ("queued","waiting","in_progress") or
             job.get("conclusion")=="success"):
            return True
    return False


def recover(now=None,get=http_json,post=http_json,verify_calendar=True):
    now=now or datetime.now(NY)
    now=now.astimezone(NY)
    if not safe_window(now):
        print(f"US start recovery outside conservative NY {START}-{END}: {now.isoformat()}",
              flush=True)
        return {"dispatched":[],"skipped":"outside_window"}
    token=os.environ.get("GITHUB_TOKEN","").strip()
    repo=os.environ.get("GITHUB_REPOSITORY","").strip()
    if not token or not repo or "/" not in repo or not repo.replace("/","").replace("-","").replace("_","").isalnum():
        raise RuntimeError("GitHub recovery missing or invalid scoped repository token")
    if verify_calendar and not check_exchange_open(now,get=get):
        return {"dispatched":[],"skipped":"calendar"}
    gh={"Authorization":"Bearer "+token,"X-GitHub-Api-Version":"2022-11-28"}
    started=datetime.combine(now.date(),time(9,0),NY).astimezone(ZoneInfo("UTC"))
    statuses={}
    dispatched=[]
    for filename,inputs in TARGETS.items():
        path=f"/repos/{repo}/actions/workflows/{filename}/runs?per_page=60"
        result=get("GET",API+path,headers=gh)
        if not isinstance(result,dict) or not isinstance(result.get("workflow_runs"),list):
            raise RuntimeError(f"Could not inspect workflow {filename}; NO dispatch")
        relevant=[]
        for r in result["workflow_runs"]:
            created=r.get("created_at")
            try:
                if datetime.fromisoformat(created.replace("Z","+00:00"))>=started:
                    relevant.append(r)
            except (AttributeError,ValueError,TypeError):
                raise RuntimeError("Malformed GitHub workflow timestamp")
        active=False
        for r in relevant:
            if r.get("status") in ("queued","pending","waiting","in_progress","requested"):
                active=True
                break
            # Check actual scanner job: skipped preflight is NOT enough.
            jid=r.get("id")
            if not isinstance(jid,int):
                raise RuntimeError("GitHub workflow run missing ID")
            data=get("GET",API+f"/repos/{repo}/actions/runs/{jid}/jobs?per_page=100",headers=gh)
            if not isinstance(data,dict) or not isinstance(data.get("jobs"),list):
                raise RuntimeError("Cannot verify GitHub job completion")
            if run_has_started(r,data["jobs"]):
                active=True
                break
        if active:
            statuses[filename]="existing session / queued start verified"
            continue
        request={"ref":"main","inputs":inputs}
        dispatch=API+f"/repos/{repo}/actions/workflows/{filename}/dispatches"
        post("POST",dispatch,payload=request,headers=gh)
        dispatched.append(filename)
        statuses[filename]="requested safe workflow_dispatch"
    print("US startup recovery "+json.dumps({"NY":now.isoformat(),
          "statuses":statuses,"dispatched":dispatched}),flush=True)
    return {"dispatched":dispatched,"statuses":statuses}


if __name__=="__main__":
    recover()
