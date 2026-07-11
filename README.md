# Zephyr merge list

This project produces a static web page listing the pull requests that are
approved and ready (or nearly ready) for merge into the main Zephyr repository.
It is meant to be run periodically by GitHub Actions and published with GitHub
Pages.

The page is designed to answer two questions at a glance:

- **Release engineers:** which PRs can I merge right now?
- **Contributors:** what is blocking my PR, and when will it be eligible?

PRs are sorted into three plain-language states — **Ready to merge now**,
**Waiting on review window** (with a concrete ETA), and **Needs attention**
(with the specific blocker spelled out). A built-in legend ("What do these
mean?") explains every state, tag, review window and release phase.

## Architecture

The backend and frontend are cleanly separated:

- **`merge_list.py`** talks to GitHub, evaluates the merge policy for each PR,
  and writes a single structured **`public/data.json`**. It does not generate
  any HTML.
- **`web/`** is a static, no-build client-side app (`index.html`, `styles.css`,
  `app.js`) that fetches `data.json` and renders it. Interactivity (search,
  sort, responsive collapse, grouped rows) is handled by
  [DataTables](https://datatables.net/) loaded from a CDN.

On each run `merge_list.py` writes `public/data.json` and copies the contents of
`web/` next to it, so the whole published site is produced by a single command.

For backward compatibility the script also still writes the raw dumps
`public/pr.json.gz` and `public/ci.json`.

### `data.json` schema

```jsonc
{
  "generated_at": "2026-07-11T14:00:00Z",
  "repo": "zephyrproject-rtos/zephyr",
  "self_repo": "kartben/zephyr-merge-list",   // this repo, for the Actions badge
  "release": { "phase": "integration" | "freeze", "latest_tag": "v4.1.0" },
  "ci": {
    "overall": "pass" | "fail" | "running" | "cancelled" | "no_data",
    "runs": [ { "name": "...", "url": "...", "status": "pass",
                "progress": "6/9", "age_mins": 12 } ]
  },
  "prs": [
    {
      "number": 84210,
      "title": "...", "url": "...", "author": "...",
      "assignees": ["..."], "approvers": ["..."],
      "base": "main", "targets_main": true, "is_backport": false,
      "milestone": "v4.2.0" | null,
      "rebaseable": true | false | null,
      "assignee_approved": true,
      "time_elapsed": true,
      "time_left_hours": 0,               // business hours remaining
      "ready_at": "2026-07-14T13:00:00Z" | null,  // ETA once eligible
      "kind": "normal" | "trivial" | "hotfix",
      "tags": { "hotfix": false, "trivial": false, "override_required": false,
                "review_dismissed": false, "dnm": false,
                "old_ci": false, "ci_age_days": null },
      "state": "ready" | "ready_backport" | "waiting" | "blocked",
      "blockers": ["Has merge conflicts — needs a rebase"]  // plain language
    }
  ]
}
```

The `state` field is where the merge policy is turned into the concepts the page
speaks in:

- **ready** — targets `main`, no conflicts, assignee-approved, review window met.
- **ready_backport** — same, but targeting a `vX.Y-branch` backport branch.
- **waiting** — approved and conflict-free, just serving out the review window.
- **blocked** — needs a human (conflicts, or a missing assignee approval).

## Running locally

Install the required Python packages:

```console
pip3 install -U -r requirements.txt
```

### Preview the UI without a GitHub token

The `--sample` flag writes a representative `public/data.json` covering every
state and tag, then copies the frontend — no network or token needed:

```console
$ python3 merge_list.py --sample
$ python3 -m http.server --directory public
# open http://localhost:8000/
```

This is the quickest way to work on `web/` styling and behaviour.

### Run against live GitHub data

Create a GitHub access token and set it in the `GITHUB_TOKEN` environment
variable, then:

```console
$ mkdir -p public
$ ./merge_list.py --self <owner>/<this-repo>
```

`--self` is the path of *this* repository and is used to render the GitHub
Actions build badge.

## Resource usage

The script normally issues 3 RPC per PR (more if the mergeability status is
stale): one for fetching the pull request, one for the reviews and one for the
events. This can limit how often the script can be run before hitting the
GitHub ratelimit. To help identify potential issues the current RPC quota is
logged at the start and end of the script.
