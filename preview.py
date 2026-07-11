#!/usr/bin/env python3

# Copyright 2024 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Render the merge list page with mock data for local UI development.

Produces public/index.html without talking to GitHub, exercising the same
rendering code as merge_list.py. Run it, then open the output in a browser:

    ./preview.py && python3 -m http.server -d public
"""

from types import SimpleNamespace
import os

import merge_list


def mock_pr(number, title, author, assignees, base="main", milestone=None):
    """Build an object that looks enough like github.PullRequest to render."""
    return SimpleNamespace(
        html_url=f"https://github.com/zephyrproject-rtos/zephyr/pull/{number}",
        title=title,
        user=SimpleNamespace(login=author),
        assignees=[SimpleNamespace(login=a) for a in assignees],
        base=SimpleNamespace(ref=base),
        milestone=SimpleNamespace(title=milestone) if milestone else None,
    )


def mock_data(number, title, author, assignees, approvers, *, base="main",
              milestone=None, assignee=True, time=True, time_left=0,
              rebaseable=True, hotfix=False, trivial=False,
              override_required=False, dnm=False, ci_recent=True,
              ci_age=None, dismissed=False):
    data = merge_list.PRData(
        pr_raw={},
        pr=mock_pr(number, title, author, assignees, base, milestone))
    data.assignee = assignee
    data.approvers = set(approvers)
    data.time = time
    data.time_left = time_left
    data.rebaseable = rebaseable
    data.hotfix = hotfix
    data.trivial = trivial
    data.override_required = override_required
    data.dnm = dnm
    data.ci_run_recent = ci_recent
    data.ci_age_days = ci_age
    data.dismissed = dismissed
    return data


# One PR per interesting state: ready, waiting on time, blocked on
# conflicts, blocked on assignee approval, tagged variants, backport.
MOCK_PRS = [
    mock_data(91234, "drivers: i2c: stm32: fix bus recovery on timeout",
              "marting", ["alicej"], ["alicej", "bobk"], milestone="v4.3.0"),
    mock_data(91180, "Bluetooth: Controller: Fix ISO stream teardown race",
              "carolz", ["davidm"], ["davidm", "erikw", "frankh"]),
    mock_data(91300, "boards: nucleo_h563zi: enable ethernet by default",
              "gracel", ["marting"], ["marting"], trivial=True,
              time=False, time_left=2),
    mock_data(91290, "kernel: sched: hotfix priority inversion in k_mutex",
              "henryp", ["alicej"], ["alicej"], hotfix=True),
    mock_data(91100, "net: lwm2m: rework registration update handling",
              "irenek", ["bobk"], ["bobk", "carolz"], time=False,
              time_left=17),
    mock_data(90950, "doc: release notes: add 4.3 migration guide",
              "jackt", ["gracel"], ["gracel"], rebaseable=False,
              milestone="v4.3.0"),
    mock_data(90800, "west: update hal_nxp to fix build with new SDK",
              "kevinb", ["davidm"], ["erikw"], assignee=False,
              ci_recent=False, ci_age=42),
    mock_data(90700, "samples: sensor: add bme680 polling sample",
              "lisaw", [], ["frankh"], base="v4.2-branch",
              dismissed=True, time=False, time_left=31),
]

MOCK_CI_STATUS = (
    '<a href=#>Build with vs code &check;</a> - '
    '<a href=#>Run tests with twister <span class=approved>&check;</span></a> - '
    '<a href=#>Documentation build <span class=blocked>&#10005;</span></a>'
)


def main():
    pr_data = {int(d.pr.html_url.rsplit("/", 1)[1]): d for d in MOCK_PRS}

    html_out = merge_list.render_html(pr_data, MOCK_CI_STATUS,
                                      "integration (latest: v4.2.0)",
                                      "kartben/zephyr-merge-list")

    os.makedirs("public", exist_ok=True)
    with open(merge_list.HTML_OUT, "w") as f:
        f.write(html_out)
    print(f"wrote {merge_list.HTML_OUT}")


if __name__ == "__main__":
    main()
