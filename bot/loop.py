"""Long-running scanner for GitHub Actions.

Runs for ~5h40m: scans every minute, saves logs to the repo every 10 minutes,
runs learning once a day and the report every hour, then re-launches itself
via workflow_dispatch so the bot never depends on GitHub's unreliable cron.
"""
import calendar
import os
import subprocess
import time
import traceback

import common as C
import learn
import report
import rotation
import multi
import trade

RUN_SECONDS = 5 * 3600 + 40 * 60
SCAN_EVERY = 60
SAVE_EVERY = 600
REPORT_EVERY = 3600
LEARN_EVERY_HOURS = 24


def sh(cmd):
    return subprocess.run(cmd, shell=True, cwd=C.ROOT, capture_output=True, text=True)


def save(msg):
    sh("git add data reports bot/config.json")
    if sh("git diff --cached --quiet").returncode == 0:
        return
    sh(f'git commit -q -m "{msg}"')
    for _ in range(3):
        sh("git pull -q --rebase --autostash -X theirs")
        if sh("git push -q").returncode == 0:
            return
        time.sleep(5)
    print("[SAVE] push failed")


def models_age_hours():
    m = C.load_json(C.MODELS, {})
    ts = m.get("updated")
    if not ts:
        return 999
    t = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    return (time.time() - t) / 3600


def safe(name, fn):
    try:
        fn()
    except SystemExit:
        pass
    except Exception as e:
        trade.log("ERROR", f"{name}: {str(e)[:250]}")
        traceback.print_exc()


def relaunch():
    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GH_TOKEN")
    if not (repo and token):
        print("[LOOP] no token, not relaunching")
        return
    r = sh(f'gh workflow run scanner.yml --repo {repo} --ref main')
    print("[LOOP] relaunch:", r.returncode, r.stderr.strip()[:200])


def main():
    start = time.time()
    last_save = last_report = 0
    trade.log("LOOP", "scanner started")
    while time.time() - start < RUN_SECONDS:
        t0 = time.time()
        sh("git pull -q --rebase --autostash -X theirs")  # pick up config/model changes
        cfg_iv = C.load_json(C.CONFIG, {}).get("interval")
        if models_age_hours() >= LEARN_EVERY_HOURS or C.load_json(C.MODELS, {}).get("interval") != cfg_iv:
            trade.log("LOOP", "running daily learning")
            safe("learn", learn.main)
            safe("report", report.main)
            save(f"learn {C.now_iso()}")
            last_report = time.time()
        safe("scan", trade.main)
        safe("rotation", rotation.main)
        safe("multi", multi.main)
        if time.time() - last_report >= REPORT_EVERY:
            safe("report", report.main)
            last_report = time.time()
        if time.time() - last_save >= SAVE_EVERY:
            save(f"scans {C.now_iso()}")
            last_save = time.time()
        time.sleep(max(5, SCAN_EVERY - (time.time() - t0)))
    safe("report", report.main)
    trade.log("LOOP", "scanner finished, relaunching")
    save(f"loop end {C.now_iso()}")
    relaunch()


if __name__ == "__main__":
    main()
