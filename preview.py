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
        number=number,
        html_url=f"https://github.com/zephyrproject-rtos/zephyr/pull/{number}",
        title=title,
        user=SimpleNamespace(login=author),
        assignees=[SimpleNamespace(login=a) for a in assignees],
        base=SimpleNamespace(ref=base),
        milestone=SimpleNamespace(title=milestone) if milestone else None,
    )


def mock_data(number, title, author, assignees, approvers, *, base="main",
              milestone=None, **fields):
    """Build a PRData; extra keyword arguments override its fields."""
    values = dict(rebaseable=True, assignee=True, time=True, time_left=0,
                  approvers=set(approvers), ci_run_recent=True)
    values.update(fields)
    return merge_list.PRData(
        pr_raw={},
        pr=mock_pr(number, title, author, assignees, base, milestone),
        **values)


# One PR per interesting state: ready, waiting on time, blocked on
# conflicts, blocked on assignee approval, mergeability still unknown,
# tagged variants, backport.
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
    mock_data(90900, "cmake: check CI_STATUS and READY_COUNT env overrides",
              "nadiam", ["frankh"], ["frankh"], rebaseable=None),
    mock_data(90800, "west: update hal_nxp to fix build with new SDK",
              "kevinb", ["davidm"], ["erikw"], assignee=False,
              ci_run_recent=False, ci_age_days=42),
    mock_data(90700, "samples: sensor: add bme680 polling sample",
              "lisaw", [], ["frankh"], base="v4.2-branch",
              dismissed=True, time=False, time_left=31),
]

MOCK_CI_STATUS = ' '.join([
    merge_list.ci_badge("ci-pass", "#", "Build with vs code"),
    merge_list.ci_badge("ci-pass", "#", "Run tests with twister"),
    merge_list.ci_badge("ci-fail", "#", "Documentation build"),
    merge_list.ci_badge("ci-running", "#", "Coding guidelines 3/12 \N{MIDDLE DOT} 25m"),
])


def main():
    pr_data = {data.pr.number: data for data in MOCK_PRS}

    html_out = merge_list.render_html(pr_data, MOCK_CI_STATUS,
                                      freeze_mode=False, latest_tag="v4.2.0",
                                      repo_path="kartben/zephyr-merge-list")

    os.makedirs("public", exist_ok=True)
    with open(merge_list.HTML_OUT, "w") as f:
        f.write(html_out)
    print(f"wrote {merge_list.HTML_OUT}")


if __name__ == "__main__":
    main()
