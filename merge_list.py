#!/usr/bin/env python3

# Copyright 2024 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Generate the Zephyr merge list, a static HTML page listing every pull
request that is ready (or nearly ready) to be merged.

The pipeline is:

1. get_prs() fetches a summary of every open PR with a few GraphQL queries.
2. should_skip() drops the PRs that have no chance of being merged soon:
   drafts, not approved, failing CI, "do not merge" labels.
3. The few remaining PRs are fetched in full and evaluate_criteria()
   computes the three merge gates for each: conflict-free, assignee
   approval, and minimum review time.
4. render_html() fills index.html.tmpl with one table row per PR plus page
   metadata (CI status of main, release phase, counters).

Outputs land in public/: the page itself, a JSON dump of the open PRs and a
JSON summary of CI on main.
"""

from dataclasses import dataclass
import argparse
import datetime
import gzip
import html
import json
import os
import re
import sys

import github
import tabulate

HTML_TEMPLATE = "index.html.tmpl"
HTML_ROWS_TOKEN = "<!-- PR_ROWS -->"

HTML_OUT = "public/index.html"
PR_JSON_OUT = "public/pr.json.gz"
CI_JSON_OUT = "public/ci.json"

PER_PAGE = 100

CI_RUN_NAME = "Run tests with twister"
CI_IGNORE = ["Code Coverage with codecov"]
CI_RUN_MAX_AGE_DAYS = 31

HOTFIX_LABEL = "Hotfix"
TRIVIAL_LABEL = "Trivial"
OVERRIDE_REQUIRED_LABEL = "Override Required"

# Minimum time a PR must stay open for review before it can be merged.
REVIEW_WINDOW_BIZ_HOURS = 48
REVIEW_WINDOW_TRIVIAL_HOURS = 4

UTC = datetime.timezone.utc


@dataclass
class PRData:
    """One open PR plus everything evaluate_criteria() derived about it."""

    pr_raw: dict                 # GraphQL summary node
    pr: github.PullRequest

    # The three merge gates (see merge_status()).
    rebaseable: bool = False     # no merge conflicts; None = not computed yet
    assignee: bool = False       # an assignee approved, or none is needed
    time: bool = False           # the minimum review window has elapsed
    time_left: int = None        # hours until the review window elapses

    approvers: set = None

    # Labels.
    hotfix: bool = False
    trivial: bool = False
    override_required: bool = False
    dnm: bool = False

    # Warnings shown as tags next to the title.
    ci_run_recent: bool = False  # last full CI run is fresh enough to trust
    ci_age_days: int = None      # age of that run; None = no run found
    dismissed: bool = False      # a "changes requested" review was dismissed

    debug: list = None


def print_rate_limit(gh, org):
    """Log the current API quota, to help diagnose rate limit issues."""
    response = gh.get_organization(org)
    for header, value in response.raw_headers.items():
        if header.startswith("x-ratelimit"):
            print(f"{header}: {value}")


def calc_biz_hours(ref, delta):
    """Count the hours in [ref, ref+delta] that fall on a weekday."""
    biz_hours = 0

    for hours in range(int(delta.total_seconds() / 3600)):
        date = ref + datetime.timedelta(hours=hours + 1)
        if date.weekday() < 5:
            biz_hours += 1

    return biz_hours


def evaluate_ci_age(repo, data):
    """Check whether the PR's last full CI run is recent enough to trust.

    Only PRs older than CI_RUN_MAX_AGE_DAYS are worth the extra API calls:
    younger PRs cannot have a CI run older than that.
    """
    pr = data.pr

    pr_age = datetime.datetime.now(UTC) - pr.created_at
    if pr_age < datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        print(f"ci age: skip {pr.number}")
        data.ci_run_recent = True
        return

    target_run = None
    for run in repo.get_workflow_runs(head_sha=pr.head.sha):
        if run.name == CI_RUN_NAME:
            target_run = run
            break

    if not target_run:
        # No run for the head commit: CI is outdated, age unknown.
        return

    run_age = datetime.datetime.now(UTC) - target_run.run_started_at
    print(f"ci age: {pr.number}: {run_age} {target_run.html_url}")
    if run_age > datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        data.ci_age_days = run_age.days
        data.ci_run_recent = False
        return

    data.ci_run_recent = True


def evaluate_criteria(repo, number, data):
    """Compute the merge gates and warning tags for one PR."""
    print(f"process: {number}")

    pr = data.pr
    author = pr.user.login
    labels = [label.name for label in pr.labels]
    assignees = [a.login for a in pr.assignees]

    hotfix = HOTFIX_LABEL in labels
    trivial = TRIVIAL_LABEL in labels
    override_required = OVERRIDE_REQUIRED_LABEL in labels
    data.dnm = any("DNM" in label for label in labels)

    # Gate 1: no merge conflicts. GitHub computes mergeability lazily, so
    # retry once if it was not available in the first response.
    rebaseable = pr.rebaseable
    if rebaseable is None:
        print(f"re-fetch: {number}")
        pr = repo.get_pull(number)
        rebaseable = pr.rebaseable

    # Gate 2: approval by an assignee. Walk the reviews in chronological
    # order so that only approvals still standing count.
    approvers = set()
    reviews = {}
    for review in data.pr.get_reviews():
        reviews[review.id] = review
        if review.user:
            if review.state == 'APPROVED':
                approvers.add(review.user.login)
            elif review.state in ['DISMISSED', 'CHANGES_REQUESTED']:
                approvers.discard(review.user.login)

    assignee_approved = (hotfix or
                         not assignees or
                         author in assignees or
                         bool(approvers.intersection(assignees)))

    # Gate 3: minimum review time, counted from creation or from the moment
    # the PR left draft state. While walking the events, also flag PRs where
    # someone dismissed another reviewer's "changes requested" review.
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
        time_left = REVIEW_WINDOW_TRIVIAL_HOURS - delta_hours
    else:
        time_left = REVIEW_WINDOW_BIZ_HOURS - delta_biz_hours

    evaluate_ci_age(repo, data)

    data.rebaseable = rebaseable
    data.assignee = assignee_approved
    data.time = time_left <= 0
    data.time_left = time_left
    data.approvers = approvers
    data.hotfix = hotfix
    data.trivial = trivial
    data.override_required = override_required
    data.dismissed = dismissed

    data.debug = [number, author, assignees, approvers, delta_hours,
                  delta_biz_hours, time_left, rebaseable, hotfix, trivial,
                  override_required, data.ci_run_recent, dismissed]


def merge_status(data):
    """Collapse the three merge gates into one status for display.

    Returns (status, label, hint) where status is one of "ready", "waiting",
    "blocked" or "unknown", label is the short text shown in the status pill
    and hint its tooltip.
    """
    if data.rebaseable is False:
        return ("blocked", "Merge conflict",
                "Does not apply cleanly on its target branch: "
                "the author needs to rebase")
    if not data.assignee:
        return ("blocked", "Needs assignee",
                "Waiting for an assignee to approve the PR")
    if not data.time:
        return ("waiting", f"{data.time_left}h left",
                "All that is missing is for the minimum review window "
                "to elapse")
    if data.rebaseable is None:
        return ("unknown", "Checking",
                "GitHub has not reported yet whether this PR applies "
                "cleanly on its target branch")
    return ("ready", "Ready", "Passes all three merge gates: "
            "will be picked up in the next merge round")


def gate_cell(css, symbol, hint):
    """One table cell in the merge gate columns: a symbol plus a tooltip."""
    return (f'<td class="gate"><span class="{css}" '
            f'title="{hint}">{symbol}</span></td>')


def render_gates(data):
    """Render the three merge gate cells: conflicts, approval, review time."""
    if data.rebaseable is None:
        conflict = gate_cell("gate-unknown", "?",
                             "Mergeability not reported by GitHub yet")
    elif data.rebaseable:
        conflict = gate_cell("gate-pass", "&check;", "No merge conflicts")
    else:
        conflict = gate_cell("gate-fail", "&#10005;",
                             "Has merge conflicts: needs a rebase")

    if data.assignee:
        approval = gate_cell("gate-pass", "&check;",
                             "Approved by an assignee, or no assignee "
                             "approval required")
    else:
        approval = gate_cell("gate-fail", "&#10005;",
                             "No approval from an assignee yet")

    if data.time:
        time = gate_cell("gate-pass", "&check;",
                         "The minimum review window has elapsed")
    else:
        time = gate_cell("gate-wait", f"{data.time_left}h",
                         "Time before the minimum review window elapses: "
                         "48 business hours, or 4 hours for trivial PRs")

    return conflict + approval + time


def render_tags(data):
    """Render the label chips displayed next to the PR title."""
    tags = []
    if data.hotfix:
        tags.append('<span class="tag tag-hotfix">hotfix</span>')
    if data.trivial:
        tags.append('<span class="tag tag-trivial">trivial</span>')
    if data.override_required:
        tags.append('<span class="tag tag-override">override required</span>')
    if not data.ci_run_recent:
        age = f"{data.ci_age_days}d" if data.ci_age_days else "stale"
        tags.append(f'<span class="tag tag-oldci">ci {age}</span>')
    if data.dismissed:
        tags.append('<span class="tag tag-dismissed">review dismissed</span>')
    if data.dnm:
        tags.append('<span class="tag tag-dnm">dnm</span>')
    return ' '.join(tags)


def table_entry(number, data):
    """Render one PR as a table row."""
    pr = data.pr
    url = pr.html_url
    title = html.escape(pr.title)
    author = html.escape(pr.user.login)
    assignees = html.escape(', '.join(sorted(a.login for a in pr.assignees)))
    approvers = html.escape(', '.join(sorted(data.approvers)))

    base = pr.base.ref
    target = base
    if pr.milestone:
        milestone = html.escape(pr.milestone.title)
        target += f' <span class="muted">{milestone}</span>'

    status, label, hint = merge_status(data)

    return f"""
        <tr class="status-{status}" data-status="{status}" data-base="{base}">
            <td class="num"><a href="{url}">{number}</a></td>
            <td><a href="{url}">{title}</a> {render_tags(data)}</td>
            <td><span class="pill pill-{status}" title="{hint}">{label}</span></td>
            {render_gates(data)}
            <td>{author}</td>
            <td>{assignees}</td>
            <td>{approvers}</td>
            <td>{target}</td>
        </tr>
        """


def render_html(pr_data, ci_status, freeze_mode, latest_tag, repo_path):
    """Fill the HTML template with one table row per PR plus page metadata.

    Rows are ordered ready first, then waiting (shortest wait first), then
    blocked; newest PR first within each group.
    """
    with open(HTML_TEMPLATE) as f:
        template = f.read()

    status_order = {"ready": 0, "waiting": 1, "unknown": 2, "blocked": 3}

    def sort_key(item):
        number, data = item
        status = merge_status(data)[0]
        wait = data.time_left if status == "waiting" else 0
        return (status_order[status], wait, -number)

    rows = ""
    for number, data in sorted(pr_data.items(), key=sort_key):
        rows += table_entry(number, data)

    ready_count = sum(1 for data in pr_data.values()
                      if merge_status(data)[0] == "ready")

    if freeze_mode:
        phase = "Feature freeze"
        phase_detail = f"next release: {latest_tag}"
        phase_hint = ("Only bug fixes and release-blocking changes are "
                      "merged until the release is tagged")
    else:
        phase = "Integration"
        phase_detail = f"latest release: {latest_tag}"
        phase_hint = "New features and fixes are merged normally"

    html_out = template.replace(HTML_ROWS_TOKEN, rows)
    html_out = html_out.replace("UPDATE_TIMESTAMP",
                                datetime.datetime.now(UTC).isoformat())
    html_out = html_out.replace("CI_STATUS", ci_status)
    html_out = html_out.replace("READY_COUNT", str(ready_count))
    html_out = html_out.replace("TOTAL_COUNT", str(len(pr_data)))
    # Longest token first: the other two start with "RELEASE_PHASE".
    html_out = html_out.replace("RELEASE_PHASE_DETAIL", phase_detail)
    html_out = html_out.replace("RELEASE_PHASE_HINT", phase_hint)
    html_out = html_out.replace("RELEASE_PHASE", phase)
    if repo_path:
        html_out = html_out.replace("REPOSITORY_PATH", repo_path)

    return html_out


def detect_feature_freeze_tag(repo):
    """Detect the release phase from the repository tags.

    The latest vX.Y.0 tag missing means the project is between the feature
    freeze (when the version bump lands) and the release. Returns
    (freeze_mode, latest_x_y_0_tag).
    """
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


def twister_missing(runs):
    return not any(run.name == CI_RUN_NAME for run in runs)


def twister_canceled(runs):
    return any(run.name == CI_RUN_NAME and run.conclusion == "cancelled"
               for run in runs)


def ci_badge(css, url, text):
    """One CI status badge shown in the "CI on main" summary card."""
    return f'<a class="ci-badge {css}" href="{url}">{text}</a>'


def get_ci_status(repo):
    """Summarize the workflow runs on the tip of main as HTML badges.

    If the main CI run was cancelled on the latest commit (e.g. superseded
    by a newer push), walk back a few commits to find a meaningful run.
    Also writes a machine-readable summary to CI_JSON_OUT.
    """
    commit = repo.get_branch('main').commit
    runs = repo.get_workflow_runs(branch="main", event="push",
                                  head_sha=commit.sha)

    if twister_canceled(runs):
        print(f"twister run canceled on {commit.sha}")
        search_commit = commit
        for _ in range(10):
            search_commit = search_commit.parents[0]
            print(f"try {search_commit.sha}")
            search_runs = repo.get_workflow_runs(branch="main", event="push",
                                                 head_sha=search_commit.sha)

            if twister_missing(search_runs) or twister_canceled(search_runs):
                continue

            print(f"using commit {search_commit.sha}")
            runs = search_runs
            break

    status = []
    runs_data = []
    for run in runs:
        name = run.name
        if name in CI_IGNORE:
            continue

        if run.status == "completed":
            if run.conclusion == "success":
                status.append(ci_badge("ci-pass", run.html_url, name))
                runs_data.append({"name": name, "status": "pass"})
            elif run.conclusion == "failure":
                status.append(ci_badge("ci-fail", run.html_url, name))
                runs_data.append({"name": name, "status": "fail"})
            elif run.conclusion == "cancelled":
                status.append(ci_badge("ci-cancelled", run.html_url, name))
                runs_data.append({"name": name, "status": "cancelled"})
            else:
                print(f"ignoring conclusion: {run.conclusion}")
        elif run.status in ["in_progress", "queued", "waiting", "pending"]:
            delta = datetime.datetime.now(UTC) - \
                run.run_started_at.astimezone(UTC)
            delta_mins = int(delta.total_seconds() / 60)
            jobs = list(run.jobs())
            completed = sum(1 for job in jobs if job.status == "completed")
            status.append(ci_badge(
                "ci-running", run.html_url,
                f"{name} {completed}/{len(jobs)} &middot; {delta_mins}m"))
            runs_data.append({"name": name, "status": "running"})
        else:
            print(f"ignoring status: {run.status}")

    with open(CI_JSON_OUT, "w") as f:
        json.dump({"runs": runs_data}, f, indent=4)

    if not status:
        return '<span class="muted">no data</span>'
    return ' '.join(sorted(status))


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
    """Fetch a summary of every open PR with paginated GraphQL queries."""
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


def should_skip(pr):
    """Decide from the GraphQL summary alone whether a PR can be ignored.

    Anything that is a draft, not approved, failing CI (unless a check
    override was requested) or labeled "do not merge" has no chance of
    being merged soon, and is dropped before the expensive per-PR fetches.
    """
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
        # Defensive: a malformed node is dropped rather than fatal.
        print(f"data error, skipping: {e}, {pr}")
        return True

    return False


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("-o", "--org", default="zephyrproject-rtos",
                        help="Target Github organisation")
    parser.add_argument("-r", "--repo", default="zephyr",
                        help="Target Github repository")
    parser.add_argument("--self", default=None, help="Self repository path")

    return parser.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    auth = github.Auth.Token(os.environ.get('GITHUB_TOKEN', None))
    gh = github.Github(auth=auth, per_page=PER_PAGE)

    print_rate_limit(gh, args.org)

    repo = gh.get_repo(f"{args.org}/{args.repo}")
    freeze_mode, latest_tag = detect_feature_freeze_tag(repo)
    print(f"Latest tag: {latest_tag}, freeze mode: {freeze_mode}")

    ci_status = get_ci_status(repo)
    print(f"CI status: {ci_status}")

    all_prs = get_prs(gh, args.org, args.repo)

    with gzip.open(PR_JSON_OUT, "wt") as f:
        json.dump(all_prs, f, indent=4)

    pr_data = {}
    for pr_raw in all_prs:
        if should_skip(pr_raw):
            continue

        number = pr_raw["number"]
        milestone = pr_raw["milestone"]

        # In freeze mode, PRs milestoned for the next release wait.
        if freeze_mode and milestone and milestone["title"] > latest_tag:
            print(f"ignoring: {number} milestone={milestone['title']} "
                  f"> {latest_tag}")
            continue

        print(f"fetch: {number}")
        pr = repo.get_pull(number)

        if not (pr.base.ref == "main" or
                (pr.base.ref.startswith("v") and
                 pr.base.ref.endswith("-branch"))):
            print(f"ignoring: {number} ref={pr.base.ref}")
            continue

        pr_data[number] = PRData(pr_raw=pr_raw, pr=pr)

    for number, data in pr_data.items():
        evaluate_criteria(repo, number, data)

    debug_headers = ["number", "author", "assignees", "approvers",
                     "delta_hours", "delta_biz_hours", "time_left",
                     "Mergeable", "Hotfix", "Trivial", "Override Required",
                     "Dismissed"]
    print(tabulate.tabulate([data.debug for data in pr_data.values()],
                            headers=debug_headers))

    html_out = render_html(pr_data, ci_status, freeze_mode, latest_tag,
                           args.self)

    with open(HTML_OUT, "w") as f:
        f.write(html_out)

    print_rate_limit(gh, args.org)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
