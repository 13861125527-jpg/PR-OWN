# GitHub Action Demo

RepoSage can review a pull request from a trusted `pull_request_target` workflow. The workflow
runs the reviewer from the default branch, fetches the PR head only as a Git object, calls the
configured OpenAI-compatible model, and publishes a summary plus eligible inline comments.

## One-time repository setup

1. Push this project to the GitHub repository's default branch.
2. Open **Settings → Secrets and variables → Actions**.
3. Add the repository secret `MODEL_API_KEY`.
4. Optionally add the repository variable `MODEL_BASE_URL`; it defaults to
   `https://api.deepseek.com`.
5. Ensure repository Actions are enabled and workflows may read repository contents and write
   pull-request and issue comments.

The workflow uses GitHub's temporary `GITHUB_TOKEN`; no personal GitHub token is required for the
same repository.

## Demo

Create a branch, change a Python file, and open a non-draft pull request. The
**RepoSage PR Review** workflow starts on open, synchronize, reopen, or ready-for-review events.
Its JSON evidence is retained as a workflow artifact for 14 days.

Fork pull requests are reviewed without exposing the model secret to PR code: the workflow never
checks out or executes the untrusted head. RepoSage reads it through locked Git object snapshots.

## Local dry run

Set `MODEL_API_KEY` and `MODEL_BASE_URL`, then run:

```powershell
reposage review --repo . --base <base-sha> --head <head-sha> --strategy agentic
```

The default is dry-run. `--publish` is meaningful only with `--pr` inside a GitHub Actions context.
