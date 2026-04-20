# Locard CLI Command Reference

Locard provides three CLI tools for AI-assisted software development:

| Tool | Description |
|------|-------------|
| `locard` | Project setup, maintenance, and framework commands |
| `af` | Agent Farm - multi-agent orchestration for development |
| `consult` | AI consultation with external models (Gemini, Codex, Claude) |

## Quick Start

```bash
# Create a new project
locard init my-project

# Or add locard to an existing project
locard adopt

# Check your environment
locard doctor

# Start the architect dashboard
af start

# Consult an AI model about a spec
consult -m gemini spec 42
```

## Installation

```bash
# Install from source - see INSTALL.md
```

This installs all three commands globally: `locard`, `af`, and `consult`.

## Command Summaries

### locard - Project Management

#### Setup & Maintenance

| Command | Description |
|---------|-------------|
| `locard init [name]` | Create a new locard project |
| `locard adopt` | Add locard to an existing project |
| `locard doctor` | Check system dependencies |
| `locard update` | Update locard templates and protocols |
| `locard update --all-projects` | Update all locard projects on this host |
| `locard import <source>` | AI-assisted protocol import from other projects |

#### Tower (Cross-Project Dashboard)

| Command | Description |
|---------|-------------|
| `locard tower` | Start cross-project dashboard |
| `locard tower --stop` | Stop the tower dashboard |
| `locard tower token create` | Create Tower authentication token |
| `locard tower token list` | List all Tower tokens |
| `locard tower token revoke` | Revoke a Tower token |

#### Workflow Commands

| Command | Description |
|---------|-------------|
| `locard spec` | Display spec requirements and feature checklist |
| `locard plan` | Display architecture and implementation approach |
| `locard pr` | Create a PR and notify the Architect |
| `locard start [spec-id]` | Start working on a feature (creates .agent-scope) |
| `locard complete <id>` | Mark a feature as complete |
| `locard verify` | Verify all passing features still pass |
| `locard features` | View and manage spec feature lists |

See [locard.md](locard.md) for full documentation.

### af - Agent Farm

#### Lifecycle

| Command | Description |
|---------|-------------|
| `af start` | Start the architect dashboard |
| `af start --remote <target>` | Start on remote host with SSH tunnel |
| `af stop` | Stop all agent farm processes |
| `af status` | Show status of all agents |

#### Builders

| Command | Description |
|---------|-------------|
| `af spawn` | Spawn a new builder |
| `af cleanup` | Clean up a builder worktree |
| `af send` | Send instructions to a builder |
| `af watch` | Watch builder for completion signals |

#### Terminals

| Command | Description |
|---------|-------------|
| `af util` / `af shell` | Spawn a utility shell |
| `af architect` | Start/attach to architect session |
| `af open <file>` | Open file annotation viewer |

#### Authentication

| Command | Description |
|---------|-------------|
| `af token create` | Create authentication token |
| `af token list` | List all tokens |
| `af token revoke` | Revoke a token |

#### Portal (Remote Access Hub)

| Command | Description |
|---------|-------------|
| `af portal start` | Start the portal server |
| `af portal token` | Generate enrollment token for agents |
| `af portal enroll` | Enroll this machine as a portal agent |
| `af portal status` | Show portal or agent status |

See [agent-farm.md](agent-farm.md) for full documentation.

For remote deployment, see [Remote Access Guide](../remote-access.md).

### consult - AI Consultation

| Subcommand | Description |
|------------|-------------|
| `consult -m <model> pr <num>` | Review a pull request |
| `consult -m <model> spec <num>` | Review a specification |
| `consult -m <model> plan <num>` | Review an implementation plan |
| `consult -m <model> general "<query>"` | General consultation |

**Models:** `gemini` (pro), `codex` (gpt), `claude` (opus)

**Options:** `--dry-run`, `--type <review-type>`, `--role <custom-role>`

See [consult.md](consult.md) for full documentation.

## Global Options

All locard commands support:

```bash
--version    Show version number
--help       Show help for any command
```

## Configuration

Customize agent-farm commands via `locard/config.json`:

```json
{
  "shell": {
    "architect": "claude --model opus",
    "builder": "claude --model sonnet",
    "shell": "bash"
  },
  "aws": {
    "secrets": true,
    "prefix": "myproject/prod",
    "region": "us-east-1"
  }
}
```

The optional `aws` block enables API key fetching from AWS Secrets Manager instead of environment variables. See [consult.md](consult.md#api-key-configuration) for details.

## Related Documentation

- [Remote Access Guide](../remote-access.md) - Deploy Agent Farm remotely with Nginx/SSL
- [SPIDER Protocol](../protocols/spider/protocol.md) - Multi-phase development workflow
- [TICK Protocol](../protocols/tick/protocol.md) - Fast amendment workflow
- [Architect Role](../roles/architect.md) - Architect responsibilities
- [Builder Role](../roles/builder.md) - Builder responsibilities
