# consult - AI Consultation CLI

The `consult` command provides a unified interface for AI consultation with external models (Gemini, Codex, Claude). Use it for code reviews, spec reviews, and general questions.

## Synopsis

```
consult -m <model> <subcommand> [args] [options]
```

## Required Option

```
-m, --model <model>    Model to use (required)
```

## Models

| Model | Alias | CLI Used | Notes |
|-------|-------|----------|-------|
| `gemini` | `pro` | gemini-cli | Pure text analysis, fast |
| `codex` | `gpt` | @openai/codex | Shell command exploration, thorough |
| `claude` | `opus` | @anthropic-ai/claude-code | Balanced analysis |

## Options

```
-n, --dry-run           Show what would execute without running
-t, --type <type>       Review type (see Review Types below)
-r, --role <role>       Custom role from locard/roles/ (see Custom Roles below)
```

## Subcommands

### consult pr

Review a pull request.

```bash
consult -m <model> pr <number>
```

**Arguments:**
- `number` - PR number to review

**Description:**

Reviews a GitHub pull request. The consultant reads:
- PR info and description
- Comments and discussions
- Diff of all changes
- File metadata

Outputs a structured review with verdict: APPROVE, REQUEST_CHANGES, or COMMENT.

**Examples:**

```bash
# Review PR #42 with Gemini
consult -m gemini pr 42

# Review with Codex (more thorough, slower)
consult -m codex pr 42

# Dry run to see command
consult -m gemini pr 42 --dry-run
```

---

### consult spec

Review a specification.

```bash
consult -m <model> spec <number>
```

**Arguments:**
- `number` - Spec number to review (e.g., `42` for `locard/specs/0042-*.md`)

**Description:**

Reviews a specification file for:
- Clarity and completeness
- Technical feasibility
- Edge cases and error scenarios
- Security considerations
- Testing strategy

If a matching plan exists, it's included for context.

**Examples:**

```bash
# Review spec 42
consult -m gemini spec 42

# With specific review type
consult -m gemini spec 42 --type spec-review
```

---

### consult plan

Review an implementation plan.

```bash
consult -m <model> plan <number>
```

**Arguments:**
- `number` - Plan number to review (e.g., `42` for `locard/plans/0042-*.md`)

**Description:**

Reviews an implementation plan for:
- Alignment with specification
- Implementation approach
- Task breakdown and ordering
- Risk identification
- Testing strategy

If a matching spec exists, it's included for context.

**Example:**

```bash
consult -m gemini plan 42
```

---

### consult general

General AI consultation.

```bash
consult -m <model> general "<query>"
```

**Arguments:**
- `query` - Question or request (quoted string)

**Description:**

Sends a free-form query to the consultant. The consultant role is still loaded, so responses follow the consultant guidelines.

**Examples:**

```bash
# Ask about code design
consult -m gemini general "What's the best way to structure auth middleware?"

# Get architecture advice
consult -m codex general "Review src/lib/database.ts for potential issues"
```

---

## Review Types

Use `--type` to load stage-specific review prompts:

| Type | Stage | Use Case |
|------|-------|----------|
| `spec-review` | conceived | Review specification completeness |
| `plan-review` | specified | Review implementation plan |
| `impl-review` | implementing | Review code implementation |
| `pr-ready` | implemented | Final check before PR |
| `integration-review` | committed | Architect's integration review |

**Location:** Review type prompts are stored in `locard/consult-types/`. You can customize existing prompts or add your own by creating new `.md` files in this directory.

> **Migration Note (v1.4.0+)**: Review types moved from `locard/roles/review-types/` to `locard/consult-types/`. The old location still works with a deprecation warning. To migrate:
> ```bash
> mkdir -p locard/consult-types
> mv locard/roles/review-types/* locard/consult-types/
> rm -r locard/roles/review-types
> ```

**Example:**

```bash
consult -m gemini spec 42 --type spec-review
consult -m codex pr 68 --type integration-review
```

---

## Custom Roles

Use `--role` to load a custom role instead of the default consultant:

```bash
consult -m gemini --role security-reviewer general "Audit this API endpoint"
consult -m codex --role gtm-specialist general "Review our landing page copy"
```

**Arguments:**
- `role` - Name of role file in `locard/roles/` (without `.md` extension)

**Available roles** depend on your project. Common ones include:
- `architect` - System design perspective
- `builder` - Implementation-focused review
- `consultant` - Default balanced review (used when no `--role` specified)

**Creating custom roles:**

1. Create a markdown file in `locard/roles/`:
   ```bash
   # locard/roles/security-reviewer.md
   # Role: Security Reviewer

   You are a security-focused code reviewer...
   ```

2. Use it with `--role`:
   ```bash
   consult -m gemini --role security-reviewer pr 42
   ```

**Role name restrictions:**
- Only letters, numbers, hyphens, and underscores
- No path separators (security: prevents directory traversal)
- Falls back to embedded skeleton if not found locally

**Example:**

```bash
# Use the architect role for high-level review
consult -m gemini --role architect general "Review this system design"

# Use a custom GTM specialist role
consult -m codex --role gtm-specialist general "Analyze our pricing page"
```

---

## Parallel Consultation (3-Way Reviews)

For thorough reviews, run multiple models in parallel:

```bash
# Using background processes
consult -m gemini spec 42 &
consult -m codex spec 42 &
consult -m claude spec 42 &
wait
```

Or with separate terminal sessions for better output separation.

---

## Performance

| Model | Typical Time | Approach |
|-------|--------------|----------|
| Gemini | ~120-150s | Pure text analysis |
| Codex | ~200-250s | Shell command exploration |
| Claude | ~60-120s | Balanced tool use |

Codex is slower because it executes shell commands (git show, rg, etc.) sequentially. It's more thorough but takes ~2x longer than Gemini.

---

## Prerequisites

Install the model CLIs you plan to use:

```bash
# Claude
npm install -g @anthropic-ai/claude-code

# Codex
npm install -g @openai/codex

# Gemini
# See: https://github.com/google-gemini/gemini-cli
```

### API Key Configuration

API keys can be provided in two ways:

**Option 1: Environment Variables (Legacy)**

Set environment variables in your shell profile:
- Claude: `ANTHROPIC_API_KEY`
- Codex: `OPENAI_API_KEY`
- Gemini: `GOOGLE_API_KEY` or `GEMINI_API_KEY`
- GitHub CLI: `GITHUB_TOKEN`

**Option 2: AWS Secrets Manager (Recommended for remote hosts)**

Fetch API keys from AWS Secrets Manager at runtime instead of environment variables. This prevents key leakage into child processes (e.g., PM2 snapshots capturing keys from the shell environment).

Add an `aws` block to `locard/config.json`:

```json
{
  "aws": {
    "secrets": true,
    "prefix": "myproject/prod",
    "region": "us-east-1",
    "cacheTtlSeconds": 300
  }
}
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `secrets` | Yes | — | Must be `true` to enable AWS mode |
| `prefix` | Yes | — | Secret name prefix (e.g., `myproject/prod`) |
| `region` | No | AWS default | AWS region for Secrets Manager |
| `cacheTtlSeconds` | No | `300` | In-memory cache TTL (seconds) |

**Secrets naming convention:**

The consult tool constructs secret IDs by appending a suffix to the prefix:

| Secret Suffix | Environment Variable | Used By |
|---------------|---------------------|---------|
| `gemini-key` | `GEMINI_API_KEY` | Gemini CLI |
| `openai-key` | `OPENAI_API_KEY` | Codex CLI |
| `anthropic-key` | `ANTHROPIC_API_KEY` | Claude CLI |
| `github-token` | `GITHUB_TOKEN` | gh CLI (PR reviews) |

For a prefix of `myproject/prod`, the full secret IDs would be:
- `myproject/prod/gemini-key`
- `myproject/prod/openai-key`
- `myproject/prod/anthropic-key`
- `myproject/prod/github-token`

**How it works:**

When AWS secrets are enabled, the consult tool:
1. **Sanitizes the environment** — strips all sensitive keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GITHUB_TOKEN`, etc.) from the child process environment
2. **Fetches only the needed key** — retrieves the model-specific API key from AWS and injects it into the child environment
3. **Caches secrets in memory** — avoids repeated AWS API calls (default 5-minute TTL)
4. **Falls back gracefully** — if AWS fetch fails, falls back to any existing environment variable

**Behavior when disabled or absent:**

| Config State | Behavior |
|---|---|
| No `aws` block in config.json | Legacy mode — full environment passthrough |
| `aws.secrets: false` | Legacy mode — full environment passthrough |
| `aws.secrets: true` but no `prefix` | Legacy mode — full environment passthrough |
| `aws.secrets: true` with valid `prefix` | AWS mode — sanitized env + fetched secrets |

Setting `secrets: false` is a clean kill switch — you can disable AWS without removing the entire config block.

**Secret value formats:**

Both formats are supported — the tool auto-detects:
- **Plaintext**: `sk-abc123` — used as-is
- **JSON**: `{"api_key": "sk-abc123"}` — extracts the first string value

The AWS console defaults to JSON format when creating secrets. Both work.

**Requirements:**
- AWS CLI installed and configured (`aws secretsmanager get-secret-value`)
- IAM permissions for `secretsmanager:GetSecretValue` on the relevant secrets

---

## The Consultant Role

The consultant role (`locard/roles/consultant.md`) defines behavior:
- Provides second perspectives on decisions
- Offers alternatives and considerations
- Works constructively (not adversarial, not a rubber stamp)
- Uses `git show <branch>:<file>` for PR reviews

Customize by copying to your local locard/ directory:

```bash
mkdir -p locard/roles
cp $(npm root -g)/@locard/cli/skeleton/roles/consultant.md locard/roles/
```

---

## Query Logging

All consultations are logged to `.consult/history.log`:

```
2024-01-15T10:30:00.000Z model=gemini duration=142.3s query=Review spec 0042...
```

---

## Examples

```bash
# Quick spec review
consult -m gemini spec 42

# Thorough PR review
consult -m codex pr 68

# Architecture question
consult -m claude general "How should I structure the caching layer?"

# Dry run to see command
consult -m gemini pr 42 --dry-run

# 3-way parallel review
consult -m gemini spec 42 &
consult -m codex spec 42 &
consult -m claude spec 42 &
wait
```

---

## See Also

- [locard](locard.md) - Project management commands
- [af](agent-farm.md) - Agent Farm commands
- [overview](overview.md) - CLI overview
