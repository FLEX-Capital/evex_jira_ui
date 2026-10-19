# /linear-session

Start a coding session from a Linear issue.

## Steps

1. Ask the user for the Linear issue identifier (e.g. `ENG-123`) if not provided as an argument.

2. Fetch the issue via the Linear MCP:
   - Title, description, and current state
   - Any sub-tasks (child issues)
   - Blocking relations

3. Present a one-paragraph summary of what needs to be done. Ask the user to confirm or clarify before proceeding.

4. Move the issue to **In Progress** in Linear.

5. Create a branch from the issue slug:

   ```bash
   git checkout -b feature/<linear-slug>
   ```

   Use `fix/` prefix instead of `feature/` if the issue is a bug.

6. State the first behaviour to implement and wait for the user to say go.
