# Branch Protection Proposal

Issue #2 requires `main` to reject direct pushes, require pull requests, require at least one review, require CI, and disallow force pushes.

Live evidence collected on 2026-09-08 shows:

- Repository: `SYandong/LLM-service-manager`
- Default branch: `main`
- Current credential permission: `WRITE`, not admin
- `main` head: `1838d79a506fae1fc5d3c13b8f1f4b6571bf6f4c`
- Current `main` protection flag: `false`
- Branch-protection API read: `404 Not Found`
- Active workflow: `ci` at `.github/workflows/ci.yml`
- Current successful check run on `main`: `test`
- Issue #2: open, no comments

An administrator should apply the exact request body in `deploy/branch-protection.json` to:

```bash
gh api --method PUT repos/SYandong/LLM-service-manager/branches/main/protection --input deploy/branch-protection.json
```

The request body targets `SYandong/LLM-service-manager` branch `main` through the GitHub REST endpoint `PUT /repos/SYandong/LLM-service-manager/branches/main/protection`.

The proposed settings intentionally use `dismiss_stale_reviews: true` and `require_last_push_approval: true` so a review approval applies only to the current PR head. A Fable COMMENTED verdict under the shared account does not satisfy GitHub's required independent approval. Both the human/member approval required by branch protection and the separate current-SHA Fable verdict plus successful CI must be present before the coordinator merges.

After the administrator applies the setting, verify with:

```bash
gh api repos/SYandong/LLM-service-manager/branches/main --jq '.protected'
gh api repos/SYandong/LLM-service-manager/branches/main/protection
```

Issue #2 should remain open until the administrator has applied the protection and the acceptance checks are observed:

- Direct push to `main` is rejected.
- PRs cannot merge without at least one approval.
- PRs cannot merge with stale approval after a new commit.
- PRs cannot merge unless the `test` check succeeds.

Locally observable reviewer-loop status:

- No running host process with `fable` or `reviewer` in its command was observed.
- No user crontab exists for this account.
- System timers and services did not show a Fable or review loop.
- User-systemd could not be queried from this non-session environment because the user bus variables were unavailable.
- Historical `.claude` review artifacts and a fresh structured review state file exist. The state file records the approval marker format as `FABLE-APPROVED <full sha> + Generated-By line` and has no reviewed PR entries yet.

<!-- Generated-By: Codex / gpt-6-astra -->
