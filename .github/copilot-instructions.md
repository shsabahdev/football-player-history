# Project documentation rules

## Standalone workspace boundary

This is a standalone workspace. Accessing the original or sibling repositories, initializing Git, or publishing requires explicit authorization.

## PROJECT_BUILD_LOG.md

`PROJECT_BUILD_LOG.md` records changes to the project itself only.

Update it when the user requests a change to:
- Source code
- Configuration
- Database schema
- Checkpoint behavior
- API providers or endpoints
- Tests
- Dependencies
- CLI commands
- Project structure
- Project documentation

For each project change:
1. Update `PROJECT_BUILD_LOG.md` first.
2. Add:
   - Date
   - Requested change
   - Files changed
   - Implementation notes
3. Implement the requested change.
4. Preserve all previous entries.

## Git operations

Do **not** update `PROJECT_BUILD_LOG.md` for Git-related requests, including:

- `git init`
- `git add`
- `git commit`
- `git branch`
- `git checkout`
- `git switch`
- `git merge`
- `git rebase`
- `git pull`
- `git fetch`
- `git push`
- Creating, deleting, renaming, or switching branches
- Creating pull requests
- Reviewing diffs
- Staging files
- Resolving merge conflicts
- Changing Git configuration

Git operations are repository-management actions, not project-change requests.

For Git requests:
- Do not modify `PROJECT_BUILD_LOG.md`.
- Do not add a change-history entry.
- Perform only the requested Git action.
- Keep the response concise.

## Database documentation

`DATABASE.md` is the authoritative documentation for the SQLite schema.

Whenever the database schema changes, Copilot must update `DATABASE.md` in the same change.

A schema change includes:
- Adding, removing, renaming, or modifying a table
- Adding, removing, renaming, or modifying a column
- Changing a data type, default, nullability, constraint, or enum value
- Adding or removing an index
- Adding or removing a foreign key
- Changing table relationships
- Changing status values or football-related terminology

The `DATABASE.md` update must include:
- Updated table purpose and granularity
- Updated column dictionary
- Updated relationships
- Updated entity relationship diagram
- Updated data-integrity rules
- Updated common queries if affected
- Migration notes if existing data is affected

Do not update `DATABASE.md` for Git operations such as commit, branch, push, merge, or pull.