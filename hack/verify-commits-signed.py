#!/usr/bin/env python3
"""Verify that every commit in a pull request is signed and verified by GitHub.

Queries the GitHub REST API for the PR's commits and checks each commit's
verification status (https://docs.github.com/rest/pulls/pulls#list-commits-on-a-pull-request).
Exits non-zero if any commit is unsigned, listing the offending commits and
guidance for the author. Outside a presubmit context (no PULL_NUMBER), the
check is a no-op.

With HEAD_ONLY=true, only the pull request's head commit is checked. This is
for repositories that rebase large sets of upstream commits, where requiring
every carried commit to be signed is not practical, but the downstream author
can still sign the commit at the tip of the branch.

When COMMENT_ON_UNSIGNED is "true" and a token is available, the first failing
run also leaves a single reminder comment on the pull request explaining how to
set up commit signing. Later runs find the existing comment and do not repeat it.
Pull requests opened by bots are skipped, since nobody would read the reminder.

See DPTP-4919: OCP is migrating repos that promote into the product to
require signed commits.
"""

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("GITHUB_API_BASE", "https://api.github.com")
PER_PAGE = 100

# Bot accounts that are regular GitHub users rather than GitHub Apps, so their
# pull requests cannot be recognized by the author's account type.
BOT_USERS = {"openshift-bot", "openshift-ci-robot"}

# Hidden marker that identifies the reminder comment, so it is posted only once.
COMMENT_MARKER = "<!-- commits-signed-reminder -->"
COMMENT_TEMPLATE = COMMENT_MARKER + """
Hi! {subject} not signed with a signature GitHub can verify.

OpenShift is gradually moving to require signed commits (DPTP-4919). The commits-signed check is **informational for now** and does not block merging, but it will become required in the future, so setting up signing now will save you trouble later.

**One-time setup with an SSH key:**
```sh
ssh-keygen -t ed25519 -f ~/.ssh/github_signing_ed25519
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/github_signing_ed25519
git config --global commit.gpgsign true
```
Then upload `~/.ssh/github_signing_ed25519.pub` at https://github.com/settings/keys as a **Signing Key** (not an Authentication Key), and consider enabling vigilant mode on the same page. GPG keys work too: https://docs.github.com/authentication/managing-commit-signature-verification

**To re-sign this branch:**
```sh
{resign}
git push --force-with-lease
```

This reminder is posted once per pull request.
"""


def github_request(url, token, data=None):
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
        request.data = json.dumps(data).encode("utf-8")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def get_pull(owner, repo, number, token):
    return github_request(f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}", token)


def get_commit(owner, repo, sha, token):
    return github_request(f"{API_BASE}/repos/{owner}/{repo}/commits/{sha}", token)


def is_bot(user):
    return user.get("type") == "Bot" or user.get("login") in BOT_USERS


def list_pr_commits(owner, repo, number, token):
    commits = []
    page = 1
    while True:
        url = f"{API_BASE}/repos/{owner}/{repo}/pulls/{number}/commits?per_page={PER_PAGE}&page={page}"
        batch = github_request(url, token)
        commits.extend(batch)
        if len(batch) < PER_PAGE:
            return commits
        page += 1


def comment_body(head_only):
    if head_only:
        return COMMENT_TEMPLATE.format(
            subject="The head commit of this pull request is",
            resign="git commit --amend --no-edit -S",
        )
    return COMMENT_TEMPLATE.format(
        subject="Some commits in this pull request are",
        resign="git rebase --exec 'git commit --amend --no-edit -S' main",
    )


def post_reminder(owner, repo, number, token, body):
    """Comment on the pull request once, unless a reminder is already there."""
    page = 1
    while True:
        url = f"{API_BASE}/repos/{owner}/{repo}/issues/{number}/comments?per_page={PER_PAGE}&page={page}"
        batch = github_request(url, token)
        if any(COMMENT_MARKER in (c.get("body") or "") for c in batch):
            print("A signing reminder is already on this pull request.")
            return
        if len(batch) < PER_PAGE:
            break
        page += 1
    github_request(f"{API_BASE}/repos/{owner}/{repo}/issues/{number}/comments", token, {"body": body})
    print("Posted a signing reminder on this pull request.")


def main():
    number = os.environ.get("PULL_NUMBER")
    if not number:
        print("PULL_NUMBER is not set; not a presubmit context, nothing to verify.")
        return 0

    owner = os.environ["REPO_OWNER"]
    repo = os.environ["REPO_NAME"]

    token = ""
    token_path = os.environ.get("GITHUB_TOKEN_PATH", "")
    if token_path and os.path.exists(token_path):
        with open(token_path, encoding="utf-8") as f:
            token = f.read().strip()
    else:
        print("warning: no GitHub token available, using unauthenticated requests")

    head_only = os.environ.get("HEAD_ONLY") == "true"

    try:
        pull = get_pull(owner, repo, number, token)
        if head_only:
            # Prefer the SHA prow checked out, so the result matches the code
            # under test even if the branch moved while the job was queued.
            head = os.environ.get("PULL_PULL_SHA") or pull["head"]["sha"]
            commits = [get_commit(owner, repo, head, token)]
        else:
            commits = list_pr_commits(owner, repo, number, token)
    except urllib.error.HTTPError as e:
        print(f"error: GitHub API request failed: {e.code} {e.reason}")
        return 1

    # The pull request commits endpoint returns at most 250 commits, and a
    # truncated last page is indistinguishable from the end of the list, so
    # compare against the count the pull request itself reports and fail
    # rather than pass a pull request we could not fully inspect.
    expected = pull["commits"]
    if not head_only and len(commits) < expected:
        print(f"error: GitHub returned {len(commits)} of the {expected} commits in this pull")
        print("request; the API lists at most 250. Reduce the number of commits (squash or")
        print("rebase the branch) so every commit can be verified.")
        return 1

    report = []
    unsigned = []
    for commit in commits:
        sha = commit["sha"]
        verification = commit["commit"].get("verification", {})
        verified = verification.get("verified", False)
        reason = verification.get("reason", "unknown")
        report.append({"sha": sha, "verified": verified, "reason": reason})
        marker = "ok" if verified else "UNSIGNED"
        print(f"{marker:>8}  {sha}  ({reason})")
        if not verified:
            unsigned.append(sha)

    artifact_dir = os.environ.get("ARTIFACT_DIR")
    if artifact_dir:
        with open(os.path.join(artifact_dir, "commit_report.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

    if unsigned and head_only:
        print()
        print("The head commit of this pull request is not signed with a signature GitHub can")
        print("verify. Only the head commit is checked, so earlier commits (such as those carried")
        print("from upstream) may stay unsigned. Sign the head commit and force-push the branch:")
        print("  https://docs.github.com/authentication/managing-commit-signature-verification/signing-commits")
        print("The head commit can be re-signed in place with:")
        print("  git commit --amend --no-edit -S")
    elif unsigned:
        print()
        print(f"{len(unsigned)} of {len(commits)} commits in this pull request are not signed with a")
        print("signature GitHub can verify. Sign your commits and force-push the branch:")
        print("  https://docs.github.com/authentication/managing-commit-signature-verification/signing-commits")
        print("An existing branch can be re-signed in place with:")
        print("  git rebase --exec 'git commit --amend --no-edit -S' main")
    if unsigned:
        if os.environ.get("COMMENT_ON_UNSIGNED") == "true" and token and not is_bot(pull["user"]):
            # The reminder is best-effort: failing to post it must not change
            # the result of the check.
            try:
                post_reminder(owner, repo, number, token, comment_body(head_only))
            except urllib.error.URLError as e:
                print(f"warning: could not post the signing reminder: {e}")
        return 1

    if head_only:
        print("\nThe head commit is signed and verified.")
    else:
        print(f"\nAll {len(commits)} commits are signed and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
