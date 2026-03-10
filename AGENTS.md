# Agent Manifest for this Django Project

## High-Level Goals
This is a Django web app. Code should:
- follow Django conventions
- include migrations when models change - these should not be made manually, but should use django utilities
- include tests for all new features
- be explicitly testable via `make test`

## Operational Rules
- Always run: `make test` before any commit or PR
- Use feature branches for all changes
- Commit messages should be concise and descriptive
- Each PR should not touch more than one feature area

## Commands Agent Can Use

### Setup
make bootstrap

### Run
make run

### Database
make migrate
make makemigrations

### Testing
make test

## Testing and Quality
- PR is only complete when all tests pass
- If new functionality is added, tests must accompany it
- Avoid editing devcontainer unless necessary for environment

## CI/Automation
- GitHub Actions will run tests on PR
- Follow CI failures and fix before merge

## Review and Merge
- PRs should be created against `main`
- Title and description should explain intent and steps

