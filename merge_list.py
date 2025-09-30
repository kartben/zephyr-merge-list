#!/usr/bin/env python3

# Copyright 2024 Google LLC
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass, field
import argparse
import datetime
import html
import json
import os
import re
import requests
import sys
import tabulate
from tqdm import tqdm

token = os.environ["GITHUB_TOKEN"]

PER_PAGE = 100

HTML_OUT = "public/index.html"
HTML_PRE = "index.html.pre"
HTML_POST = "index.html.post"

CI_JSON_OUT = "public/ci.json"

PASS = "<span class=approved>&check;</span>"
FAIL = "<span class=blocked>&#10005;</span>"
CANCELLED = "<span class=unknown>&#10005;</span>"
UNKNOWN = "<span class=unknown>?</span>"

UTC = datetime.timezone.utc

CI_RUN_NAME = "Run tests with twister"
CI_RUN_MAX_AGE_DAYS = 31


@dataclass
class PRData:
    pr_node: dict
    assignee: str = field(default=None)
    approvers: set = field(default=None)
    time: bool = field(default=False)
    time_left: int = field(default=None)
    rebaseable: bool = field(default=False)
    hotfix: bool = field(default=False)
    trivial: bool = field(default=False)
    dnm: bool = field(default=False)
    ci_age_days: int = field(default=None)
    ci_run_recent: bool = field(default=False)
    debug: list = field(default=None)


def graphql_query(query, variables=None):
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(
        "https://api.github.com/graphql",
        json={"query": query, "variables": variables},
        headers=headers,
    )
    if response.status_code != 200:
        raise Exception(f"Query failed: {response.status_code} {response.text}")
    result = response.json()
    if "errors" in result:
        raise Exception(f"GraphQL errors: {result['errors']}")
    return result["data"]


def print_rate_limit():
    query = """
    {
      rateLimit {
        limit
        cost
        remaining
        resetAt
      }
    }
    """
    data = graphql_query(query)
    rate_limit = data["rateLimit"]
    print(
        f"Rate limit: {rate_limit['remaining']}/{rate_limit['limit']}, resets at {rate_limit['resetAt']}"
    )


def calc_biz_hours(ref, delta):
    biz_hours = 0

    for hours in range(int(delta.total_seconds() / 3600)):
        date = ref + datetime.timedelta(hours=hours + 1)
        if date.weekday() < 5:
            biz_hours += 1

    return biz_hours


def set_ci_age_data(org, repo_name, data):
    pr_node = data.pr_node
    number = pr_node["number"]

    pr_age = datetime.datetime.now(UTC) - datetime.datetime.fromisoformat(
        pr_node["createdAt"].replace("Z", "+00:00")
    )
    if pr_age < datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        print(f"ci age: skip {number}")
        data.ci_run_recent = True
        return

    head_sha = pr_node["headRefOid"]
    query = """
    query($owner: String!, $repo: String!, $headSha: String!) {
      repository(owner: $owner, name: $repo) {
        object(expression: $headSha) {
          ... on Commit {
            checkSuites(first: 10) {
              nodes {
                workflowRun {
                  workflow {
                    name
                  }
                  url
                  createdAt
                }
              }
            }
          }
        }
      }
    }
    """
    result = graphql_query(
        query, {"owner": org, "repo": repo_name, "headSha": head_sha}
    )

    if not result["repository"]["object"]:
        return

    check_suites = result["repository"]["object"]["checkSuites"]["nodes"]
    target_run = None
    for suite in check_suites:
        if (
            suite["workflowRun"]
            and suite["workflowRun"]["workflow"]["name"] == CI_RUN_NAME
        ):
            target_run = suite["workflowRun"]
            break

    if not target_run:
        return

    run_age = datetime.datetime.now(UTC) - datetime.datetime.fromisoformat(
        target_run["createdAt"].replace("Z", "+00:00")
    )
    print(f"ci age: {number}: {run_age} {target_run['url']}")
    if run_age > datetime.timedelta(days=CI_RUN_MAX_AGE_DAYS):
        data.ci_age_days = run_age.days
        data.ci_run_recent = False
        return

    data.ci_run_recent = True


def evaluate_criteria(org, repo_name, number, data):
    print(f"process: {number}")

    pr_node = data.pr_node
    author = pr_node["author"]["login"]
    labels = [label_edge["node"]["name"] for label_edge in pr_node["labels"]["edges"]]
    assignees = [a["node"]["login"] for a in pr_node["assignees"]["edges"]]
    rebaseable = pr_node["mergeable"] == "MERGEABLE"
    hotfix = "Hotfix" in labels
    trivial = "Trivial" in labels

    for label in labels:
        if "DNM" in label:
            data.dnm = True
            break

    approvers = set()
    for review_edge in pr_node["reviews"]["edges"]:
        review = review_edge["node"]
        if review["author"]:
            if review["state"] == "APPROVED":
                approvers.add(review["author"]["login"])
            elif review["state"] in ["DISMISSED", "CHANGES_REQUESTED"]:
                approvers.discard(review["author"]["login"])

    assignee_approved = False

    if hotfix or not assignees or author in assignees:
        assignee_approved = True

    for approver in approvers:
        if approver in assignees:
            assignee_approved = True

    reference_time = datetime.datetime.fromisoformat(
        pr_node["createdAt"].replace("Z", "+00:00")
    )

    for event_edge in pr_node["timelineItems"]["edges"]:
        event = event_edge["node"]
        if event.get("__typename") == "ReadyForReviewEvent":
            reference_time = datetime.datetime.fromisoformat(
                event["createdAt"].replace("Z", "+00:00")
            )

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

    set_ci_age_data(org, repo_name, data)

    data.assignee = assignee_approved
    data.approvers = approvers
    data.time = time_left <= 0
    data.time_left = time_left
    data.rebaseable = rebaseable
    data.hotfix = hotfix
    data.trivial = trivial

    data.debug = [
        number,
        author,
        assignees,
        approvers,
        delta_hours,
        delta_biz_hours,
        time_left,
        rebaseable,
        hotfix,
        trivial,
        data.ci_run_recent,
    ]


def table_entry(number, data):
    pr_node = data.pr_node
    url = pr_node["url"]
    title = html.escape(pr_node["title"])
    author = html.escape(pr_node["author"]["login"])
    assignees = html.escape(
        ", ".join(sorted(a["node"]["login"] for a in pr_node["assignees"]["edges"]))
    )
    approvers = html.escape(", ".join(sorted(data.approvers)))

    base = pr_node["baseRefName"]
    if pr_node["milestone"]:
        milestone = pr_node["milestone"]["title"]
    else:
        milestone = ""

    if data.rebaseable is None:
        rebaseable = UNKNOWN
    elif data.rebaseable == True:
        rebaseable = PASS
    else:
        rebaseable = FAIL
    assignee = PASS if data.assignee else FAIL
    time = PASS if data.time else FAIL + f" {data.time_left}h left"

    # Determine if PR is mergeable (targets main and has three green checkmarks)
    is_mergeable = base == "main" and data.rebaseable and data.assignee and data.time

    if is_mergeable:
        tr_class = "mergeable"
    elif (data.rebaseable is None or data.rebaseable) and data.assignee and data.time:
        tr_class = ""
    else:
        tr_class = "draft"

    tags = []
    if data.hotfix:
        tags.append("<span class='tag tag-hotfix'>hotfix</span>")
    if data.trivial:
        tags.append("<span class='tag tag-trivial'>trivial</span>")
    if not data.ci_run_recent:
        tags.append(f"<span class='tag tag-oldci'>ci {data.ci_age_days}d</span>")
    if data.dnm:
        tags.append("<span class='tag tag-dnm'>dnm</span>")
    tags_text = " ".join(tags)

    return f"""
        <tr class="{tr_class}">
            <td><a href="{url}">{number}</a></td>
            <td><a href="{url}">{title}</a></td>
            <td>{tags_text}</td>
            <td>{author}</td>
            <td>{assignees}</td>
            <td>{approvers}</td>
            <td>{base}</td>
            <td>{milestone}</td>
            <td>{rebaseable}</td>
            <td>{assignee}</td>
            <td>{time}</td>
        </tr>
        """


def detect_feature_freeze_tag(org, repo_name):
    query = """
    query($owner: String!, $repo: String!) {
      repository(owner: $owner, name: $repo) {
        refs(refPrefix: "refs/tags/", first: 100, orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
          nodes {
            name
          }
        }
      }
    }
    """
    result = graphql_query(query, {"owner": org, "repo": repo_name})

    latest_version = (0, 0, 0)
    tags = []
    for tag_node in result["repository"]["refs"]["nodes"]:
        tag_name = tag_node["name"]
        match = re.match(r"^v([0-9]+)\.([0-9]+)\.([0-9]+)", tag_name)
        if not match:
            continue

        tags.append(tag_name)

        tag_version = tuple(map(int, match.groups()))
        if tag_version > latest_version:
            latest_version = tag_version

    latest_tag = "v%d.%d.%d" % latest_version
    if latest_tag in tags:
        return False, latest_tag

    return True, latest_tag


def run_twister_not_found(runs):
    for run in runs:
        if (
            run["workflowRun"]
            and run["workflowRun"]["workflow"]["name"] == "Run tests with twister"
        ):
            return False
    return True


def run_twister_canceled(runs):
    for run in runs:
        if (
            run["workflowRun"]
            and run["workflowRun"]["workflow"]["name"] == "Run tests with twister"
            and run["conclusion"] == "CANCELLED"
        ):
            return True
    return False


def get_ci_status(org, repo_name):
    query = """
    query($owner: String!, $repo: String!) {
      repository(owner: $owner, name: $repo) {
        ref(qualifiedName: "refs/heads/main") {
          target {
            ... on Commit {
              oid
              checkSuites(first: 20) {
                nodes {
                  conclusion
                  status
                  createdAt
                  workflowRun {
                    workflow {
                      name
                    }
                    url
                  }
                  checkRuns(first: 100) {
                    nodes {
                      status
                    }
                  }
                }
              }
              parents(first: 10) {
                nodes {
                  oid
                }
              }
            }
          }
        }
      }
    }
    """
    result = graphql_query(query, {"owner": org, "repo": repo_name})

    commit = result["repository"]["ref"]["target"]
    commit_sha = commit["oid"]
    runs = [suite for suite in commit["checkSuites"]["nodes"] if suite["workflowRun"]]

    if run_twister_canceled(runs):
        print(f"twister run canceled on {commit_sha}")
        parent_commits = commit["parents"]["nodes"]
        for i, parent in enumerate(parent_commits):
            parent_sha = parent["oid"]
            print(f"try {parent_sha}")

            parent_query = """
            query($owner: String!, $repo: String!, $sha: String!) {
              repository(owner: $owner, name: $repo) {
                object(expression: $sha) {
                  ... on Commit {
                    checkSuites(first: 20) {
                      nodes {
                        conclusion
                        status
                        createdAt
                        workflowRun {
                          workflow {
                            name
                          }
                          url
                        }
                        checkRuns(first: 100) {
                          nodes {
                            status
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """
            parent_result = graphql_query(
                parent_query, {"owner": org, "repo": repo_name, "sha": parent_sha}
            )
            search_runs = [
                suite
                for suite in parent_result["repository"]["object"]["checkSuites"][
                    "nodes"
                ]
                if suite["workflowRun"]
            ]

            if run_twister_not_found(search_runs) or run_twister_canceled(search_runs):
                continue

            print(f"using commit {parent_sha}")
            runs = search_runs
            break

    status = []
    runs_data = []
    for run in runs:
        html_url = run["workflowRun"]["url"]
        name = run["workflowRun"]["workflow"]["name"]
        if run["status"] == "COMPLETED":
            if run["conclusion"] == "SUCCESS":
                status.append(f"<a href={html_url}>{name} {PASS}</a>")
                runs_data.append({"name": name, "status": "pass"})
            elif run["conclusion"] == "FAILURE":
                status.append(f"<a href={html_url}>{name} {FAIL}</a>")
                runs_data.append({"name": name, "status": "fail"})
            elif run["conclusion"] == "CANCELLED":
                status.append(f"<a href={html_url}>{name} {CANCELLED}</a>")
                runs_data.append({"name": name, "status": "cancelled"})
            else:
                print(f"ignoring conclusion: {run['conclusion']}")
        elif run["status"] in ["IN_PROGRESS", "QUEUED", "WAITING", "PENDING"]:
            delta = datetime.datetime.now(UTC) - datetime.datetime.fromisoformat(
                run["createdAt"].replace("Z", "+00:00")
            )
            delta_mins = int(delta.total_seconds() / 60)
            jobs = run["checkRuns"]["nodes"]
            total = len(jobs)
            completed = sum(1 for j in jobs if j["status"] == "COMPLETED")
            status.append(
                f"<a href={html_url}>{name} ({UNKNOWN} {completed}/{total} {delta_mins}m)</a>"
            )
            runs_data.append({"name": name, "status": "running"})
        else:
            print(f"ignoring status: {run['status']}")

    with open(CI_JSON_OUT, "w") as f:
        json.dump({"runs": runs_data}, f, indent=4)

    if not status:
        return "no data"
    else:
        return " - ".join(sorted(status))


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "-o", "--org", default="zephyrproject-rtos", help="Target Github organisation"
    )
    parser.add_argument(
        "-r", "--repo", default="zephyr", help="Target Github repository"
    )
    parser.add_argument(
        "-i",
        "--ignore-milestones",
        default="future",
        help="Comma separated list of milestones to ignore",
    )
    parser.add_argument(
        "-l",
        "--ignore-labels",
        default="",
        help="Comma separated list of labels to ignore",
    )
    parser.add_argument("--self", default=None, help="Self repository path")

    return parser.parse_args(argv)


def fetch_prs_graphql(org, repo_name):
    """Fetch all PRs with all necessary data in one GraphQL query"""
    query = """
    query($searchQuery: String!, $cursor: String) {
      search(query: $searchQuery, type: ISSUE, first: 10, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          ... on PullRequest {
            number
            title
            url
            createdAt
            author {
              login
            }
            baseRefName
            headRefOid
            mergeable
            milestone {
              title
            }
            labels(first: 20) {
              edges {
                node {
                  name
                }
              }
            }
            assignees(first: 20) {
              edges {
                node {
                  login
                }
              }
            }
            reviews(first: 100, states: [APPROVED, CHANGES_REQUESTED, DISMISSED]) {
              edges {
                node {
                  author {
                    login
                  }
                  state
                }
              }
            }
            timelineItems(first: 50, itemTypes: [READY_FOR_REVIEW_EVENT]) {
              edges {
                node {
                  __typename
                  ... on ReadyForReviewEvent {
                    createdAt
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    search_query = f"is:pr is:open repo:{org}/{repo_name} review:approved status:success -label:DNM draft:false"
    all_prs = []
    cursor = None

    with tqdm(desc="Fetching PRs", unit=" PRs") as pbar:
        while True:
            result = graphql_query(
                query, {"searchQuery": search_query, "cursor": cursor}
            )
            batch = result["search"]["nodes"]
            all_prs.extend(batch)
            pbar.update(len(batch))

            if not result["search"]["pageInfo"]["hasNextPage"]:
                break
            cursor = result["search"]["pageInfo"]["endCursor"]

    return all_prs


def main(argv):
    args = parse_args(argv)

    print_rate_limit()

    pr_data = {}

    if args.ignore_milestones:
        ignore_milestones = args.ignore_milestones.split(",")
        print(f"ignored milestones: {ignore_milestones}")
    else:
        ignore_milestones = []

    if args.ignore_labels:
        ignore_labels = args.ignore_labels.split(",")
        print(f"ignored labels: {ignore_labels}")
    else:
        ignore_labels = []

    freeze_mode, latest_tag = detect_feature_freeze_tag(args.org, args.repo)
    print(f"Latest tag: {latest_tag}, freeze mode: {freeze_mode}")

    ci_status = get_ci_status(args.org, args.repo)
    print(f"CI status: {ci_status}")

    pr_nodes = fetch_prs_graphql(args.org, args.repo)
    print(f"Fetched {len(pr_nodes)} PRs")

    for pr_node in pr_nodes:
        number = pr_node["number"]

        milestone_title = (
            pr_node["milestone"]["title"] if pr_node["milestone"] else None
        )
        if milestone_title and milestone_title in ignore_milestones:
            print(f"ignoring: {number} milestone={milestone_title}")
            continue

        if freeze_mode and milestone_title and milestone_title > latest_tag:
            print(f"ignoring: {number} milestone={milestone_title} > {latest_tag}")
            continue

        skip = False
        for label_edge in pr_node["labels"]["edges"]:
            if label_edge["node"]["name"] in ignore_labels:
                print(f"ignoring: {number} label={label_edge['node']['name']}")
                skip = True
                break
        if skip:
            continue

        base_ref = pr_node["baseRefName"]
        if not (
            base_ref == "main"
            or (base_ref.startswith("v") and base_ref.endswith("-branch"))
        ):
            print(f"ignoring: {number} ref={base_ref}")
            continue

        pr_data[number] = PRData(pr_node=pr_node)

    for number, data in pr_data.items():
        evaluate_criteria(args.org, args.repo, number, data)

    with open(HTML_PRE) as f:
        html_out = f.read()
        timestamp = datetime.datetime.now(UTC).isoformat()

    debug_headers = [
        "number",
        "author",
        "assignees",
        "approvers",
        "delta_hours",
        "delta_biz_hours",
        "time_left",
        "Mergeable",
        "Hotfix",
        "Trivial",
    ]
    debug_data = []
    for _, data in pr_data.items():
        debug_data.append(data.debug)
    print(tabulate.tabulate(debug_data, headers=debug_headers))

    data_out = []
    for number, data in pr_data.items():
        data_out.append(((data.assignee and data.time, number), data))

    for (_, number), data in sorted(data_out, key=lambda x: x[0], reverse=True):
        html_out += table_entry(number, data)

    with open(HTML_POST) as f:
        html_out += f.read()

    html_out = html_out.replace("UPDATE_TIMESTAMP", timestamp)
    html_out = html_out.replace("CI_STATUS", ci_status)

    milestones_text = ", ".join(ignore_milestones) if ignore_milestones else "none"
    html_out = html_out.replace("IGNORED_MILESTONES", milestones_text)

    labels_text = ", ".join(ignore_labels) if ignore_labels else "none"
    html_out = html_out.replace("IGNORED_LABELS", labels_text)

    if freeze_mode:
        phase_text = f"feature freeze (next: {latest_tag})"
    else:
        phase_text = f"integration (latest: {latest_tag})"
    html_out = html_out.replace("RELEASE_PHASE", phase_text)

    if args.self:
        html_out = html_out.replace("REPOSITORY_PATH", args.self)

    with open(HTML_OUT, "w") as f:
        f.write(html_out)

    print_rate_limit()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
