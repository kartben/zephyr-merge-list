#!/usr/bin/env python3

# Copyright 2024 Google LLC
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, field
import argparse
import datetime
import github
import json
import os
import re
import shutil
import sys
import tabulate
import gzip

token = os.environ.get("GITHUB_TOKEN")

PER_PAGE = 100

# Static frontend lives in web/ and is copied verbatim into the published
# public/ directory next to the generated data.json.
WEB_DIR = "web"
PUBLIC_DIR = "public"

DATA_JSON_OUT = "public/data.json"

# Kept for backward compatibility with any external consumers of the raw dumps.
PR_JSON_OUT = "public/pr.json.gz"
CI_JSON_OUT = "public/ci.json"
CI_IGNORE = ["Code Coverage with codecov"]

UTC = datetime.timezone.utc

CI_RUN_NAME = "Run tests with twister"
CI_RUN_MAX_AGE_DAYS = 31

HOTFIX_LABEL = "Hotfix"
TRIVIAL_LABEL = "Trivial"
OVERRIDE_REQUIRED_LABEL = "Override Required"


@dataclass
class PRData:
    pr_raw: dict
    pr: github.PullRequest
    assignee: str = field(default=None)
    approvers: set = field(default=None)
    time: bool = field(default=False)
    time_left: int = field(default=None)
    rebaseable: bool = field(default=False)
    hotfix: bool = field(default=False)
    trivial: bool = field(default=False)
    override_required: bool = field(default=False)
    dnm: bool = field(default=False)
    ci_age_days: int = field(default=None)
    ci_run_recent: bool = field(default=False)
    dismissed: bool = field(default=False)
    debug: list = field(default=None)


def print_rate_limit(gh, org):
    response = gh.get_organization(org)
    for header, value in response.raw_headers.items():
        if header.startswith("x-ratelimit"):
            print(f"{header}: {value}")


def calc_biz_hours(ref, delta):
    biz_hours = 0

    for hours in range(int(delta.total_seconds() / 3600)):
        date = ref + datetime.timedelta(hours=hours+1)
        if date.weekday() < 5:
            biz_hours += 1

    return biz_hours


def add_biz_hours(ref, biz_hours):
    """Return the wall-clock time `biz_hours` business hours after `ref`.

    Mirror of calc_biz_hours, walking forward one hour at a time and only
    counting hours that fall on a weekday. Used to turn a "hours left" review
    countdown into a concrete ETA the frontend can render as "ready ~Mon 14:00".
    """
    if biz_hours <= 0:
        return ref

    result = ref
    remaining = biz_hours
    # Bound the loop generously (a couple of calendar weeks) to avoid ever
    # spinning: 48 business hours is at most ~10 calendar days.
    for _ in range(24 * 30):
        result = result + datetime.timedelta(hours=1)
        if result.weekday() < 5:
            remaining -= 1
            if remaining <= 0:
                break

    return result


def set_ci_age_data(repo, data):
    pr = data.pr

    pr_age = datetime.datetime.now(UTC) - pr.created_at
    if pr_age < datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        print(f"ci age: skip {pr.number}")
        data.ci_run_recent = True
        return

    runs = repo.get_workflow_runs(head_sha=pr.head.sha)

    target_run = None
    for run in runs:
        if run.name == CI_RUN_NAME:
            target_run = run
            break

    if not target_run:
        return

    run_age = datetime.datetime.now(UTC) - run.run_started_at
    print(f"ci age: {pr.number}: {run_age} {run.html_url}")
    if run_age > datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        data.ci_age_days = run_age.days
        data.ci_run_recent = False
        return

    data.ci_run_recent = True


def evaluate_criteria(repo, number, data):
    print(f"process: {number}")

    pr = data.pr
    author = pr.user.login
    labels = [l.name for l in pr.labels]
    assignees = [a.login for a in pr.assignees]
    rebaseable = pr.rebaseable
    hotfix = HOTFIX_LABEL in labels
    trivial = TRIVIAL_LABEL in labels
    override_required = OVERRIDE_REQUIRED_LABEL in labels

    for label in labels:
        if "DNM" in label:
            data.dnm = True
            break

    if rebaseable is None:
        print(f"re-fetch: {number}")
        pr = repo.get_pull(number)
        rebaseable = pr.rebaseable

    approvers = set()
    reviews = {}
    for review in data.pr.get_reviews():
        reviews[review.id] = review
        if review.user:
            if review.state == 'APPROVED':
                approvers.add(review.user.login)
            elif review.state in ['DISMISSED', 'CHANGES_REQUESTED']:
                approvers.discard(review.user.login)

    assignee_approved = False

    if (hotfix or
        not assignees or
        author in assignees):
        assignee_approved = True

    for approver in approvers:
        if approver in assignees:
            assignee_approved = True

    dismissed = False

    reference_time = pr.created_at
    for event in data.pr.get_issue_events():
        if event.event == 'ready_for_review':
            reference_time = event.created_at
        elif event.event == 'review_dismissed':
            dismissed_review = event.dismissed_review
            review = reviews[dismissed_review['review_id']]

            # Do not trigger for approval dismissal via push.
            if ('dismissal_commit_id' not in dismissed_review and
                dismissed_review['state'] == 'changes_requested' and
                event.actor.login != review.user.login and
                review.user.login not in approvers):
                dismissed = True

    now = datetime.datetime.now(UTC)

    delta = now - reference_time.astimezone(UTC)
    delta_hours = int(delta.total_seconds() / 3600)
    delta_biz_hours = calc_biz_hours(reference_time.astimezone(UTC), delta)

    if hotfix:
        time_left = 0
    elif trivial:
        time_left = 4 - delta_hours
    else:
        time_left = 48 - delta_biz_hours

    set_ci_age_data(repo, data)

    data.assignee = assignee_approved
    data.approvers = approvers
    data.time = time_left <= 0
    data.time_left = time_left
    data.rebaseable = rebaseable
    data.hotfix = hotfix
    data.trivial = trivial
    data.override_required = override_required
    data.dismissed = dismissed

    data.debug = [number, author, assignees, approvers, delta_hours,
                  delta_biz_hours, time_left, rebaseable, hotfix, trivial,
                  override_required, data.ci_run_recent, dismissed]


def pr_core(pr):
    """Extract the plain data the frontend needs from a github PullRequest."""
    return {
        "title": pr.title,
        "url": pr.html_url,
        "author": pr.user.login,
        "assignees": sorted(a.login for a in pr.assignees),
        "base": pr.base.ref,
        "milestone": pr.milestone.title if pr.milestone else None,
    }


def is_backport_branch(base):
    return base.startswith("v") and base.endswith("-branch")


def serialize_pr(number, core, data, now):
    """Turn evaluated PR criteria into the structured record the UI renders.

    This is the single place the merge policy is translated into the concepts
    the page speaks in: a `state` bucket, a plain-language `blockers` list, and
    a concrete `ready_at` ETA. Kept independent of any github object (works off
    `core` + the computed fields on `data`) so it can be exercised offline.
    """
    base = core["base"]
    targets_main = base == "main"

    kind = "hotfix" if data.hotfix else "trivial" if data.trivial else "normal"

    time_elapsed = bool(data.time)
    time_left_hours = max(0, data.time_left if data.time_left is not None else 0)

    conflicts = data.rebaseable is False
    unknown_mergeability = data.rebaseable is None
    needs_assignee = not data.assignee

    # State bucket. Order matters: anything needing a human action is "blocked"
    # (a.k.a. "needs attention") regardless of the review clock; a healthy PR
    # merely serving out the mandatory window is "waiting".
    if conflicts or unknown_mergeability or needs_assignee:
        state = "blocked"
    elif not time_elapsed:
        state = "waiting"
    elif targets_main:
        state = "ready"
    else:
        state = "ready_backport"

    # Plain-language, actionable reasons a PR is not ready to merge.
    blockers = []
    if conflicts:
        blockers.append("Has merge conflicts — needs a rebase")
    if unknown_mergeability:
        blockers.append("Mergeability is still being computed by GitHub")
    if needs_assignee:
        blockers.append("Needs an approval from one of its assignees")

    # ETA the PR becomes eligible (only meaningful while still waiting).
    ready_at = None
    if not time_elapsed and time_left_hours > 0:
        if kind == "trivial":
            eta = now + datetime.timedelta(hours=time_left_hours)
        else:
            eta = add_biz_hours(now, time_left_hours)
        ready_at = eta.isoformat()

    tags = {
        "hotfix": bool(data.hotfix),
        "trivial": bool(data.trivial),
        "override_required": bool(data.override_required),
        "review_dismissed": bool(data.dismissed),
        "dnm": bool(data.dnm),
        "old_ci": not data.ci_run_recent,
        "ci_age_days": data.ci_age_days,
    }

    return {
        "number": number,
        "title": core["title"],
        "url": core["url"],
        "author": core["author"],
        "assignees": core["assignees"],
        "approvers": sorted(data.approvers) if data.approvers else [],
        "base": base,
        "targets_main": targets_main,
        "is_backport": is_backport_branch(base),
        "milestone": core["milestone"],
        "rebaseable": data.rebaseable,
        "assignee_approved": bool(data.assignee),
        "time_elapsed": time_elapsed,
        "time_left_hours": time_left_hours,
        "ready_at": ready_at,
        "kind": kind,
        "tags": tags,
        "state": state,
        "blockers": blockers,
    }


def detect_feature_freeze_tag(repo):
    latest_version = (0, 0, 0)
    tags = []
    for tag in repo.get_tags():
        match = re.match(r"^v([0-9]+)\.([0-9]+)\.([0-9]+)", tag.name)
        if not match:
            continue

        tag_version = tuple(map(int, match.groups()))
        if tag_version[2] != 0:
            continue

        tags.append(tag.name)

        if tag_version > latest_version:
            latest_version = tag_version

    latest_tag = "v%d.%d.%d" % latest_version
    if latest_tag in tags:
        return False, latest_tag

    return True, latest_tag


def run_twister_not_found(runs):
    for run in runs:
        if run.name == "Run tests with twister":
            return False
    return True


def run_twister_canceled(runs):
    for run in runs:
        if run.name == "Run tests with twister" and run.conclusion == "cancelled":
            return True
    return False


def ci_overall(runs_data):
    """Collapse individual workflow results into one headline status."""
    statuses = {r["status"] for r in runs_data}
    if not statuses:
        return "no_data"
    if "fail" in statuses:
        return "fail"
    if "running" in statuses:
        return "running"
    if "cancelled" in statuses:
        return "cancelled"
    return "pass"


def get_ci_status(repo):
    commit = repo.get_branch('main').commit
    runs = repo.get_workflow_runs(branch="main", event="push", head_sha=commit.sha)

    if run_twister_canceled(runs):
        print(f"twister run canceled on {commit.sha}")
        search_commit = commit
        for i in range(10):
            search_commit = search_commit.parents[0]
            print(f"try {search_commit.sha}")
            search_runs = repo.get_workflow_runs(branch="main", event="push", head_sha=search_commit.sha)

            if run_twister_not_found(search_runs) or run_twister_canceled(search_runs):
                continue

            print(f"using commit {search_commit.sha}")
            runs = search_runs
            break

    runs_data = []
    for run in runs:
        name = run.name

        if name in CI_IGNORE:
            continue

        entry = {"name": name, "url": run.html_url}

        if run.status == "completed":
            if run.conclusion == "success":
                entry["status"] = "pass"
            elif run.conclusion == "failure":
                entry["status"] = "fail"
            elif run.conclusion == "cancelled":
                entry["status"] = "cancelled"
            else:
                print(f"ignoring conclusion: {run.conclusion}")
                continue
        elif run.status in ["in_progress", "queued", "waiting", "pending"]:
            delta = datetime.datetime.now(UTC) - run.run_started_at.astimezone(UTC)
            delta_mins = int(delta.total_seconds() / 60)
            jobs = list(run.jobs())
            total = len(jobs)
            completed = sum(1 for j in jobs if j.status == "completed")
            entry["status"] = "running"
            entry["progress"] = f"{completed}/{total}"
            entry["age_mins"] = delta_mins
        else:
            print(f"ignoring status: {run.status}")
            continue

        runs_data.append(entry)

    runs_data.sort(key=lambda r: r["name"])

    with open(CI_JSON_OUT, "w") as f:
        json.dump({"runs": runs_data}, f, indent=4)

    return {"runs": runs_data, "overall": ci_overall(runs_data)}


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("-o", "--org", default="zephyrproject-rtos",
                        help="Target Github organisation")
    parser.add_argument("-r", "--repo", default="zephyr",
                        help="Target Github repository")
    parser.add_argument("--self", default=None, help="Self repository path")
    parser.add_argument("--sample", action="store_true",
                        help="Write a representative sample data.json without "
                             "contacting GitHub (for previewing/testing the UI)")

    return parser.parse_args(argv)


QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 50, states: OPEN, after: $cursor) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        isDraft
        milestone {
          title
        }
        labels(first: 30) {
          nodes {
            name
          }
        }
        reviewDecision
        statusCheckRollup {
          state
        }
      }
    }
  }
}
"""

def get_prs(gh, org, repo):
    variables = {
            "owner": org,
            "name": repo,
            "cursor": None,
    }

    all_prs = []
    has_next_page = True

    while has_next_page:
        _, resp = gh.requester.graphql_query(QUERY, variables)

        prs = resp["data"]["repository"]["pullRequests"]

        all_prs.extend(prs["nodes"])
        has_next_page = prs["pageInfo"]["hasNextPage"]
        variables["cursor"] = prs["pageInfo"]["endCursor"]

        print(f"query: {len(all_prs)} PRs")

    return all_prs

def we_dont_care(pr):
    try:
        if pr["isDraft"]:
            return True

        override_required = False
        for label in pr["labels"]["nodes"]:
            if "DNM" in label["name"]:
                return True
            if label["name"] == OVERRIDE_REQUIRED_LABEL:
                override_required = True

        if pr['reviewDecision'] != "APPROVED":
            return True

        status_check_rollup = pr["statusCheckRollup"]
        ci_state = status_check_rollup["state"] if status_check_rollup else None
        if ci_state != "SUCCESS" and not override_required:
            return True
    except Exception as e:
        print(f"data error, skipping: {e}, {pr}")
        return True

    return False


def write_outputs(data):
    """Write data.json and copy the static frontend into public/."""
    os.makedirs(PUBLIC_DIR, exist_ok=True)

    with open(DATA_JSON_OUT, "w") as f:
        json.dump(data, f, indent=2)

    for name in os.listdir(WEB_DIR):
        src = os.path.join(WEB_DIR, name)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(PUBLIC_DIR, name))

    print(f"wrote {DATA_JSON_OUT} ({len(data['prs'])} PRs) and copied {WEB_DIR}/*")


def make_sample(now, number, title, author, assignees, approvers, base,
                milestone, rebaseable, assignee_ok, time_left, kind="normal",
                override_required=False, dismissed=False, dnm=False,
                ci_age_days=None):
    """Build one serialized PR record from primitives, no github object needed."""
    data = PRData(pr_raw={}, pr=None)
    data.approvers = set(approvers)
    data.rebaseable = rebaseable
    data.assignee = assignee_ok
    data.time_left = time_left
    data.time = time_left <= 0
    data.hotfix = kind == "hotfix"
    data.trivial = kind == "trivial"
    data.override_required = override_required
    data.dismissed = dismissed
    data.dnm = dnm
    data.ci_age_days = ci_age_days
    data.ci_run_recent = ci_age_days is None

    core = {
        "title": title,
        "url": f"https://github.com/zephyrproject-rtos/zephyr/pull/{number}",
        "author": author,
        "assignees": sorted(assignees),
        "base": base,
        "milestone": milestone,
    }
    return serialize_pr(number, core, data, now)


def sample_data(self_repo):
    """A representative data.json covering every state and tag, for previews."""
    now = datetime.datetime.now(UTC)

    prs = [
        make_sample(now, 84210, "drivers: spi: add DMA support for nRF54L",
                    "alice", ["bob"], ["bob", "carol"], "main", "v4.2.0",
                    True, True, 0),
        make_sample(now, 84115, "doc: fix typo in kernel scheduling guide",
                    "dave", ["erin"], ["erin"], "main", "v4.2.0",
                    True, True, 0, kind="trivial"),
        make_sample(now, 83999, "Bluetooth: host: guard against NULL conn",
                    "frank", ["grace"], ["grace"], "main", None,
                    True, True, 0, kind="hotfix"),
        make_sample(now, 83880, "boards: arm: backport regulator fix",
                    "heidi", ["ivan"], ["ivan"], "v4.1-branch", "v4.1.1",
                    True, True, 0),
        make_sample(now, 84240, "net: lwm2m: support for composite operations",
                    "judy", ["mallory"], ["mallory"], "main", "v4.2.0",
                    True, True, 34),
        make_sample(now, 84255, "samples: sensor: tidy up console output",
                    "niaj", ["olivia"], ["olivia"], "main", "v4.2.0",
                    True, True, 3, kind="trivial"),
        make_sample(now, 84260, "arch: riscv: enable PMP for user mode",
                    "peggy", ["sybil"], ["sybil"], "main", "v4.2.0",
                    True, True, 12, override_required=True),
        make_sample(now, 84090, "drivers: clock_control: rework STM32 tree",
                    "trent", ["walter"], ["walter"], "main", "v4.2.0",
                    False, True, 0),
        make_sample(now, 84131, "kernel: mem_slab: add runtime statistics",
                    "victor", ["wendy"], [], "main", "v4.2.0",
                    True, False, 0),
        make_sample(now, 84188, "dts: bindings: document new sensor props",
                    "craig", ["dan"], ["dan"], "main", "v4.2.0",
                    None, True, 0),
        make_sample(now, 83777, "logging: backend: fix dropped message count",
                    "faythe", ["gwen"], ["gwen"], "main", "v4.2.0",
                    True, False, 20, dismissed=True),
        make_sample(now, 84300, "manifest: bump hal_nordic to latest",
                    "sybil", ["trudy"], ["trudy"], "main", "v4.2.0",
                    True, True, 0, ci_age_days=40),
    ]

    return {
        "generated_at": now.isoformat(),
        "repo": "zephyrproject-rtos/zephyr",
        "self_repo": self_repo or "kartben/zephyr-merge-list",
        "release": {"phase": "integration", "latest_tag": "v4.1.0"},
        "ci": {
            "overall": "pass",
            "runs": [
                {"name": "Run tests with twister",
                 "url": "https://github.com/zephyrproject-rtos/zephyr/actions",
                 "status": "pass"},
                {"name": "Documentation Build",
                 "url": "https://github.com/zephyrproject-rtos/zephyr/actions",
                 "status": "pass"},
                {"name": "BabbleSim Tests",
                 "url": "https://github.com/zephyrproject-rtos/zephyr/actions",
                 "status": "running", "progress": "6/9", "age_mins": 12},
            ],
        },
        "prs": prs,
    }


def main(argv):
    args = parse_args(argv)

    if args.sample:
        data = sample_data(args.self)
        write_outputs(data)
        return 0

    auth = github.Auth.Token(os.environ.get('GITHUB_TOKEN', None))
    gh = github.Github(auth=auth, per_page=PER_PAGE)

    print_rate_limit(gh, args.org)

    pr_data = {}

    repo = gh.get_repo(f"{args.org}/{args.repo}")
    freeze_mode, latest_tag = detect_feature_freeze_tag(repo)
    print(f"Latest tag: {latest_tag}, freeze mode: {freeze_mode}")

    ci = get_ci_status(repo)
    print(f"CI status: {ci['overall']}")

    all_prs = get_prs(gh, args.org, args.repo)

    with gzip.open(PR_JSON_OUT, "wt") as f:
        json.dump(all_prs, f, indent=4)

    for pr_raw in all_prs:
        if we_dont_care(pr_raw):
            continue

        number = pr_raw["number"]
        milestone = pr_raw["milestone"]

        if freeze_mode and milestone and milestone["title"] > latest_tag:
            print(f"ignoring: {number} milestone={milestone['title']} > {latest_tag}")
            continue

        print(f"fetch: {number}")
        pr = repo.get_pull(number)

        if not (pr.base.ref == "main" or
                (pr.base.ref.startswith("v") and pr.base.ref.endswith("-branch"))):
            print(f"ignoring: {number} ref={pr.base.ref}")
            continue

        pr_data[number] = PRData(pr_raw=pr_raw, pr=pr)

    for number, data in pr_data.items():
        evaluate_criteria(repo, number, data)

    debug_headers = ["number", "author", "assignees", "approvers",
                     "delta_hours", "delta_biz_hours", "time_left", "Mergeable",
                     "Hotfix", "Trivial", "Override Required", "Dismissed"]
    debug_data = [data.debug for _, data in pr_data.items()]
    print(tabulate.tabulate(debug_data, headers=debug_headers))

    now = datetime.datetime.now(UTC)
    prs_out = []
    for number, data in pr_data.items():
        prs_out.append(serialize_pr(number, pr_core(data.pr), data, now))

    # A stable, sensible default order (the frontend re-sorts): ready first,
    # then by how soon a PR becomes eligible, then by number.
    state_rank = {"ready": 0, "ready_backport": 1, "waiting": 2, "blocked": 3}
    prs_out.sort(key=lambda p: (state_rank.get(p["state"], 9),
                                p["time_left_hours"], -p["number"]))

    if freeze_mode:
        phase = "freeze"
    else:
        phase = "integration"

    data = {
        "generated_at": now.isoformat(),
        "repo": f"{args.org}/{args.repo}",
        "self_repo": args.self,
        "release": {"phase": phase, "latest_tag": latest_tag},
        "ci": ci,
        "prs": prs_out,
    }

    write_outputs(data)

    print_rate_limit(gh, args.org)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
