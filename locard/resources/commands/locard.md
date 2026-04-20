# locard - Project Management CLI

The `locard` command manages project setup, maintenance, and framework-level operations.

## Synopsis

```
locard <command> [options]
```

## Commands

### locard init

Create a new locard project.

```bash
locard init [project-name] [options]
```

**Arguments:**
- `project-name` - Name of the project (optional, prompts if not provided)

**Options:**
- `-y, --yes` - Use defaults without prompting

**Description:**

Creates a minimal locard project structure:
- `locard/specs/` - Specification files
- `locard/plans/` - Implementation plans
- `locard/reviews/` - Review documents
- `locard/projectlist.md` - Project tracking
- `CLAUDE.md` / `AGENTS.md` - AI agent instructions
- `.gitignore` - Standard ignores

Framework files (protocols, roles) are provided by the embedded skeleton at runtime, not copied to the project.

**Examples:**

```bash
# Interactive creation
locard init

# Create with name
locard init my-app

# Non-interactive with defaults
locard init my-app -y
```

---

### locard adopt

Add locard to an existing project.

```bash
locard adopt [options]
```

**Options:**
- `-y, --yes` - Skip conflict prompts

**Description:**

Adds locard structure to the current directory. Detects existing files (CLAUDE.md, AGENTS.md) and prompts before overwriting.

Use this when you want to add locard to a project that already has code.

**Examples:**

```bash
# Add to current project
cd existing-project
locard adopt

# Skip prompts for conflicts
locard adopt -y
```

---

### locard doctor

Check system dependencies.

```bash
locard doctor
```

**Description:**

Verifies that all required dependencies are installed and properly configured:

**Core Dependencies (required):**
- Node.js (>= 20.0.0)
- tmux (>= 3.0)
- ttyd (>= 1.7.0)
- git (>= 2.5.0)
- gh (GitHub CLI, authenticated)

**AI CLI Dependencies (at least one required):**
- Claude (`@anthropic-ai/claude-code`)
- Gemini (`gemini-cli`)
- Codex (`@openai/codex`)

**Exit Codes:**
- `0` - All OK or warnings only
- `1` - Required dependencies missing

**Example:**

```bash
locard doctor
```

Output:
```
Locard Doctor - Checking your environment
============================================

Core Dependencies (required for Agent Farm)

  ✓ Node.js      20.10.0
  ✓ tmux         3.4
  ✓ ttyd         1.7.4
  ✓ git          2.42.0
  ✓ gh           authenticated
  ✓ @locard/cli  1.0.0

AI CLI Dependencies (at least one required)

  ✓ Claude       working
  ✓ Gemini       working
  ○ Codex        not installed (npm i -g @openai/codex)

============================================
ALL OK - Your environment is ready for Locard!
```

---

### locard update

Update locard templates and protocols.

```bash
locard update [options]
```

**Options:**
- `-n, --dry-run` - Show changes without applying
- `-f, --force` - Force update, overwrite all files
- `-a, --all-projects` - Update all locard projects registered on this host

**Description:**

Updates framework files (protocols, roles, agents) from the installed `@locard/cli` package. User data (specs, plans, reviews) is never modified.

If you've customized a file locally, the update creates a `.locard-new` file with the new version so you can merge changes manually.

#### Single Project (default)

When run without `--all-projects`, updates only the current project:

```bash
# Preview changes
locard update --dry-run

# Apply updates
locard update

# Force overwrite (discard local changes)
locard update --force
```

#### All Projects (`--all-projects`)

Updates every locard project registered on this host. Uses the Agent Farm global registry (`~/.agent-farm/global.db`) to discover projects.

**What it does:**

1. **Finds locard-dev-tools** (the source repo) by tracing back from the CLI install path
2. **Pulls, builds, and reinstalls** locard-dev-tools globally (`git pull` → `npm install` → `npm run build` → `npm install -g`)
3. **Runs `locard update`** in each registered project that has a `locard/` directory

```bash
# Preview what would happen
locard update --all-projects --dry-run

# Update everything on this host
locard update --all-projects

# Force update all projects
locard update --all-projects --force
```

**Example output:**

```
Updating all locard projects on this host

Step 1: Updating locard-dev-tools
  /home/ubuntu/locard-dev-tools
  Pulling latest...
  Installing dependencies...
  Building...
  Installing globally...
  locard-dev-tools updated and reinstalled globally

Step 2: Updating 3 project(s)

  home_ed (/home/ubuntu/elation/home_ed)... up to date
  listing_management (/home/ubuntu/elation/listing_management)... updated
  eventatlas (/home/ubuntu/elation/eventatlas)... up to date

Summary:
  Source: locard-dev-tools (pulled, built, installed)
  1 project(s) updated
  2 project(s) already up to date
```

**Note:** Projects are discovered from the Agent Farm global registry. A project must have been started with `af start` at least once to appear in the registry.

---

### locard tower

Cross-project dashboard showing all agent-farm instances.

```bash
locard tower [options]
```

**Options:**

| Option | Description |
|--------|-------------|
| `-p, --port <port>` | Port to run on (default: 4100) |
| `--stop` | Stop the tower dashboard |
| `--allowed-hosts <hosts>` | Comma-separated list of allowed external hostnames |
| `--no-auth-localhost` | Disable authentication for localhost connections |
| `--tls-cert <path>` | Path to TLS certificate file |
| `--tls-key <path>` | Path to TLS private key file |
| `--tls-auto` | Generate self-signed certificate automatically |
| `--trust-proxy` | Trust reverse proxy for TLS termination |
| `--dashboard-path-pattern <pattern>` | URL pattern for dashboard links in remote mode (default: `/p/{port}/`) |

**Description:**

Starts a web-based dashboard that shows all running agent-farm instances across different projects. Tower provides:

- **Multi-project view**: See all running AF instances at a glance
- **Remote access**: Access Tower securely over the internet via reverse proxy
- **Project management**: Start, stop, and restart AF instances from the UI
- **Token authentication**: Secure access with bearer tokens

#### Local Usage

For local development, Tower runs on `localhost:4100` with no authentication required:

```bash
# Start tower dashboard
locard tower

# Start on custom port
locard tower -p 4200

# Stop the dashboard
locard tower --stop
```

#### Remote Access via Reverse Proxy

To access Tower remotely through a reverse proxy (e.g., nginx), use the remote access options:

```bash
# Start with remote access enabled
locard tower \
  --allowed-hosts cloud.example.com \
  --trust-proxy \
  --dashboard-path-pattern '/p/{port}/' \
  --no-auth-localhost
```

**Required nginx configuration:**

```nginx
# Tower dashboard at /tower/
location /tower/ {
    rewrite ^/tower/(.*)$ /$1 break;
    proxy_pass http://127.0.0.1:4100;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}

# Individual project dashboards at /p/{port}/
location ~ ^/p/(\d+)/ {
    set $target_port $1;
    rewrite ^/p/\d+/(.*)$ /$1 break;
    proxy_pass http://127.0.0.1:$target_port;
    proxy_http_version 1.1;
    proxy_set_header Host localhost:$target_port;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

**How remote mode works:**

When `--trust-proxy` is set and the `X-Forwarded-Host` header is present, Tower automatically transforms dashboard URLs:
- Local: `http://localhost:4200` → Remote: `/p/4200/`
- Local: `http://localhost:4300` → Remote: `/p/4300/`

This allows clicking dashboard links in Tower to work correctly through the reverse proxy.

#### TLS Configuration

For direct HTTPS access (without a reverse proxy):

```bash
# With existing certificates
locard tower --tls-cert /path/to/cert.pem --tls-key /path/to/key.pem

# With auto-generated self-signed certificate
locard tower --tls-auto --allowed-hosts myhost.local
```

#### Authentication

Tower uses bearer token authentication for remote access. Tokens are created and managed via subcommands:

```bash
# Create a new token
locard tower token create --name my-laptop

# List all tokens
locard tower token list

# Revoke a token
locard tower token revoke my-laptop
```

**Using tokens:**

```bash
# Via URL parameter (sets cookie)
https://cloud.example.com/tower/?token=tower_xxx...

# Via Authorization header
curl -H "Authorization: Bearer tower_xxx..." https://cloud.example.com/tower/api/status
```

---

### locard tower token create

Create a new authentication token.

```bash
locard tower token create --name <name> [--expires <days>]
```

**Options:**
- `--name <name>` - Human-readable name for the token (required)
- `--expires <days>` - Token expiration in days (optional, default: never)

**Example:**

```bash
locard tower token create --name "my-laptop" --expires 30
```

Output:
```
Tower token created successfully!

Token:    tower_YgHKigK7aPyCZt1reVTwJ6oYRYvCEKx9WorE1B8_cFI
ID:       a1b2c3d4
Name:     my-laptop
Expires:  2024-03-15 12:00:00

⚠️  Save this token now - it will not be shown again!
```

---

### locard tower token list

List all tower tokens.

```bash
locard tower token list
```

Shows all tokens with their ID, name, creation date, last used date, and expiration status.

---

### locard tower token revoke

Revoke a tower token.

```bash
locard tower token revoke <name-or-id> [--by-id]
```

**Options:**
- `--by-id` - Treat the argument as a token ID instead of name

**Examples:**

```bash
# Revoke by name
locard tower token revoke my-laptop

# Revoke by ID
locard tower token revoke a1b2c3d4 --by-id
```

---

### locard tower secret rotate

Rotate the tower secret (invalidates all sessions).

```bash
locard tower secret rotate
```

**Description:**

Rotates the cryptographic secret used for session management. All existing sessions will be invalidated after a 5-minute grace period.

---

## Workflow Commands

Commands for managing specs, plans, and development workflow.

### locard spec

Display spec requirements and feature checklist.

```bash
locard spec [options]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `-s, --spec <id>` | Spec ID (auto-detected if not provided) | auto |
| `-f, --full` | Show full output without truncation | - |

**Description:**

Displays the current spec's requirements and tracks feature completion status. When run in a builder worktree, auto-detects the spec from the branch name.

**Examples:**

```bash
# Show current spec (auto-detected)
locard spec

# Show specific spec
locard spec -s 0042

# Show full output
locard spec -f
```

---

### locard plan

Display architecture and implementation approach.

```bash
locard plan [options]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `-s, --spec <id>` | Spec ID (auto-detected if not provided) | auto |
| `-f, --full` | Show full output without truncation | - |
| `-u, --update` | Open plan in editor for updating | - |

**Description:**

Displays the implementation plan for the current or specified spec. Includes architecture details, phase breakdown, and implementation guidance.

**Examples:**

```bash
# Show current plan
locard plan

# Edit the plan
locard plan -u

# Show specific plan in full
locard plan -s 0042 -f
```

---

### locard pr

Create a PR and notify the Architect.

```bash
locard pr [options]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `-t, --title <title>` | Custom PR title | auto-generated |
| `-b, --body <body>` | PR description | auto-generated |
| `-d, --draft` | Create as draft PR | - |
| `--base <branch>` | Base branch to merge into | `main` |
| `--skip-tests` | Skip running tests before PR | - |
| `--skip-notify` | Skip architect notification | - |

**Description:**

Creates a GitHub pull request for the current builder branch and sends a notification to the Architect. Automatically generates a PR title and body based on the spec.

**Examples:**

```bash
# Create PR with auto-generated title/body
locard pr

# Create draft PR
locard pr -d

# Create with custom title
locard pr -t "Add user authentication"

# Skip pre-PR tests
locard pr --skip-tests
```

---

### locard start

Start working on a feature (creates .agent-scope).

```bash
locard start [spec-id] [options]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `-b, --builder <id>` | Builder ID (auto-generated if not provided) | auto |
| `--allowed-paths <paths...>` | Glob patterns for allowed paths | - |
| `--allowed-shared <paths...>` | Glob patterns for shared utility paths | - |
| `--blocked-paths <paths...>` | Glob patterns for blocked paths | - |
| `--branch` | Create feature branch | - |
| `--force` | Force start even if another feature is active | - |
| `--json` | Output as JSON | - |

**Description:**

Initializes a working session for a spec by creating `.agent-scope` file that defines path restrictions for the builder. Helps enforce scope during implementation.

**Examples:**

```bash
# Start working on spec 0042
locard start 0042

# Start with custom path restrictions
locard start 0042 --allowed-paths "src/auth/**" --blocked-paths "src/admin/**"

# Force start (override active session)
locard start 0042 --force
```

---

### locard complete

Mark a feature as complete (runs required tests first).

```bash
locard complete <feature-id> [options]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--skip-test-integrity` | Skip test integrity check | - |
| `--skip-plan-rot` | Skip plan rot warning | - |

**Description:**

Marks a feature as complete after validating tests pass. Performs integrity checks to ensure the implementation matches the plan.

**Example:**

```bash
# Mark feature as complete
locard complete 0042

# Skip test integrity (not recommended)
locard complete 0042 --skip-test-integrity
```

---

### locard verify

Verify all passing features still pass (regression detection).

```bash
locard verify [options]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `-s, --spec <id>` | Verify specific spec only | all |

**Description:**

Runs tests for all features marked as complete to detect regressions. Useful before releases or after major changes.

**Examples:**

```bash
# Verify all features
locard verify

# Verify specific spec
locard verify -s 0042
```

---

### locard features

View and manage spec feature lists.

```bash
locard features [options]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `-s, --spec <id>` | Filter by spec ID | all |
| `-i, --init` | Generate features from spec requirements | - |
| `-e, --export` | Export to FEATURE_STATUS.md | - |
| `-o, --output <path>` | Output path for export | - |
| `-f, --force` | Force overwrite existing features | - |

**Description:**

Manages feature tracking for specs. Can generate initial feature lists from spec requirements and export status for documentation.

**Examples:**

```bash
# List all features
locard features

# Initialize features from spec
locard features -s 0042 -i

# Export feature status
locard features -e -o docs/FEATURE_STATUS.md
```

---

### locard import

AI-assisted protocol import from other locard projects.

```bash
locard import <source> [options]
```

**Arguments:**
- `source` - Local path or GitHub URL (github:owner/repo or https://...)

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `-n, --dry-run` | Show what would be imported without running Claude | - |

**Description:**

Imports protocol improvements from other locard projects with AI assistance. Claude analyzes differences and recommends imports, which you can approve or reject interactively.

**Examples:**

```bash
# Import from GitHub
locard import github:example/project

# Import from local directory
locard import /path/to/other-project

# Dry run to preview
locard import github:example/project --dry-run
```

---

## See Also

- [af](agent-farm.md) - Agent Farm commands
- [consult](consult.md) - AI consultation
- [overview](overview.md) - CLI overview
