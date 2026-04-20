# {{PROJECT_NAME}} - Claude Code Instructions

## Project Overview

This project uses **Locard** for AI-assisted development.

## Available Protocols

- **SPIDER**: Multi-phase development with consultation (`locard/protocols/spider/protocol.md`)
- **TICK**: Fast autonomous implementation (`locard/protocols/tick/protocol.md`)
- **EXPERIMENT**: Disciplined experimentation (`locard/protocols/experiment/protocol.md`)
- **MAINTAIN**: Codebase maintenance (`locard/protocols/maintain/protocol.md`)

## Key Locations

- **Specs**: `locard/specs/` - Feature specifications (WHAT to build)
- **Plans**: `locard/plans/` - Implementation plans (HOW to build)
- **Tests**: `locard/tests/` - TDD acceptance tests (generated before implementation)
- **Reviews**: `locard/reviews/` - Reviews and lessons learned
- **Protocols**: `locard/protocols/` - Development protocols

## Quick Start

1. For new features, start with the Specification phase
2. Create exactly THREE documents per feature: spec, plan, and review
3. Follow the MANDATORY workflow steps below exactly

## MANDATORY Specification Workflow (Follow Exactly)

When writing a spec, your todo list MUST include these steps in order:
1. Reserve project number in projectlist.md
2. Write initial specification draft
3. Commit: "Initial specification draft"
4. Run expert consultation (`consult --model gemini --type spec-review spec XXXX`)
5. Update spec with consultation feedback
6. Commit: "Specification with multi-agent review"
7. **Run red team security review** (`consult --model gemini --type red-team-spec spec XXXX`)
8. **Address ALL security findings (CRITICAL, HIGH, MEDIUM, LOW)**
9. Commit: "Specification with security review"
10. Present to human for approval

## MANDATORY Planning Workflow (Follow Exactly)

When writing a plan, your todo list MUST include these steps in order:
1. Write initial plan draft
2. Commit: "Initial plan draft"
3. Run expert consultation (`consult --model gemini --type plan-review plan XXXX`)
4. Update plan with consultation feedback
5. Commit: "Plan with multi-agent review"
6. **Run red team security review** (`consult --model gemini --type red-team-plan plan XXXX`)
7. **Address ALL security findings (CRITICAL, HIGH, MEDIUM, LOW)**
8. Commit: "Plan with security review"
9. Present to human for approval

## Multi-Agent Consultation (ENABLED BY DEFAULT)

**DEFAULT BEHAVIOR**: Consultation is **ENABLED BY DEFAULT** when using SPIDER protocol.

**DEFAULT AGENTS**:
- **GPT-5 Codex** (codex): Primary reviewer for architecture, feasibility, and code quality
- **Gemini Pro** (gemini): Secondary reviewer for completeness, edge cases, and alternative approaches

**DISABLING CONSULTATION**: To run without consultation, user must explicitly say "without consultation"

### CRITICAL CONSULTATION CHECKPOINTS (DO NOT SKIP)

| Phase | When to Consult | Command |
|-------|-----------------|---------|
| Specification | After initial draft | `consult --model gemini --type spec-review spec XXXX` |
| Specification | After human comments | (same command, second round) |
| **Specification** | **After expert review** | `consult --model gemini --type red-team-spec spec XXXX` |
| Planning | After initial plan | `consult --model gemini --type plan-review plan XXXX` |
| Planning | After human review | (same command, second round) |
| **Planning** | **After expert review** | `consult --model gemini --type red-team-plan plan XXXX` |
| Implementation | After code complete | `consult --model gemini --type impl-review spec XXXX` |
| **Implementation** | **After expert review** | `consult --model gemini --type red-team-impl pr XX` |
| Defend | After tests written | (consult for test coverage review) |
| Evaluate | Before marking complete | (final expert approval required) |

**⚠️ BLOCKING**: The protocol is BLOCKED until required consultations AND red team reviews are complete. You cannot proceed to the next phase without completing all reviews. CRITICAL findings from red team reviews must be resolved before proceeding.

Run consultations in parallel for speed:
```bash
consult --model gemini --type spec-review spec 0042 &
consult --model codex --type spec-review spec 0042 &
wait
```

## File Naming Convention

Use sequential numbering with descriptive names:
- Specification: `locard/specs/0001-feature-name.md`
- Plan: `locard/plans/0001-feature-name.md`
- Review: `locard/reviews/0001-feature-name.md`

## Git Workflow

**NEVER use `git add -A` or `git add .`** - Always add files explicitly.

Commit messages format:
```
[Spec 0001] Description of change
[Spec 0001][Phase: implement] feat: Add feature
```

## TDD (Test-First AI)

Locard supports Test-Driven Development where tests are generated BEFORE implementation.

### For Architects

Generate acceptance tests from a spec:
```bash
consult --model gemini --type test-design spec 0042 > locard/tests/0042-feature.ts
git add locard/tests/0042-feature.ts
git commit -m "[Spec 0042] TDD test scaffolding"
```

### For Builders

When spawned with TDD tests:
1. Read the tests first - they encode requirements
2. Run tests early - all should fail initially
3. Implement to pass tests - each passing test = progress
4. DO NOT modify acceptance tests - they are the source of truth
5. Add additional unit tests in the Defend phase

### When to Use TDD

**Always use** for: new features, API changes, security-sensitive code, complex business logic

**Skip** for: bug fixes, TICK amendments, documentation, config changes

## CLI Commands

Locard provides three CLI tools:

- **locard**: Project management (init, adopt, update, doctor)
- **af**: Agent Farm orchestration (start, spawn, status, cleanup)
- **consult**: AI consultation for reviews (pr, spec, plan)

For complete reference, see `locard/resources/commands/`:
- `locard/resources/commands/overview.md` - Quick start
- `locard/resources/commands/locard.md` - Project commands
- `locard/resources/commands/agent-farm.md` - Agent Farm commands
- `locard/resources/commands/consult.md` - Consultation commands

## Configuration

Customize Agent Farm behavior in `locard/config.json`:

```json
{
  "shell": {
    "architect": "claude",
    "builder": "claude",
    "shell": "bash"
  }
}
```

## For More Info

Read the full protocol documentation in `locard/protocols/`.
