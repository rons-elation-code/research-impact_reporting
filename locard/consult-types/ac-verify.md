# Acceptance Criteria Verification Prompt

## Purpose

Verify that acceptance criteria are ACTUALLY met - not just that code exists.

## The Problem This Solves

Builders often mark ACs as "done" when:
- Code exists that could theoretically satisfy the AC
- The happy path works in isolation
- They haven't actually tested the full flow

This review catches the gap between "code exists" and "feature works".

## Your Task

For EACH acceptance criterion in the spec:

1. **Read the AC literally** - What does it actually say?
2. **Trace the implementation** - Does code exist that fulfills this?
3. **Verify end-to-end** - Would a real user experience what the AC describes?
4. **Check edge cases** - What happens when things go wrong?

## Verification Checklist Per AC

For each AC, answer:

```
AC: [Quote the exact AC text]

□ Code exists that addresses this AC
  → Where? [file:line or "MISSING"]

□ Happy path works
  → Evidence: [How do you know?]

□ Error handling exists
  → What happens if it fails?

□ User can actually do this
  → Is there UI? Is it reachable? Is it obvious?

□ Integration complete
  → Does it connect to other components correctly?

VERDICT: [MET | PARTIAL | NOT MET | CANNOT VERIFY]
EVIDENCE: [Specific proof or gap identified]
```

## Red Flags to Watch For

### "Invisible Features"
- AC says "User can X" but there's no UI path to X
- Feature exists in code but isn't wired to routes/navigation
- API endpoint exists but nothing calls it

### "Partial Implementation"
- Happy path works, error path crashes
- Create works, but update/delete missing
- UI exists but doesn't submit to backend

### "Wrong Layer"
- AC is about user experience, implementation is about data model
- "User sees confirmation" but there's no UI feedback
- "Admin can manage X" but no admin role exists

### "Assumed Infrastructure"
- AC requires auth but auth isn't wired up
- AC requires permissions but authorization is missing
- AC requires navigation but links don't exist

## Output Format

```
## Acceptance Criteria Verification Report

### Summary
- Total ACs: [N]
- MET: [N]
- PARTIAL: [N]
- NOT MET: [N]
- CANNOT VERIFY: [N]

### Detailed Findings

#### AC1: [Quote AC]
VERDICT: [MET | PARTIAL | NOT MET]
EVIDENCE: [Specific details]
GAP: [What's missing, if any]
FIX: [What needs to be done]

[Repeat for each AC]

### Critical Gaps
[List ACs marked NOT MET that block the feature from working]

### Partial Implementations
[List ACs marked PARTIAL with what's missing]

### Verification Blockers
[List anything that prevented full verification]
```

## Verdict Meanings

| Verdict | Meaning |
|---------|---------|
| **MET** | AC is fully satisfied, verified working |
| **PARTIAL** | Some aspects work, others missing or broken |
| **NOT MET** | AC is not satisfied, feature doesn't work as described |
| **CANNOT VERIFY** | Unable to determine (need access, unclear AC, etc.) |

## When to Use This Review

- **After Implement phase** - Before moving to Defend
- **After Defend phase** - Before Evaluate
- **Before PR merge** - Final check that spec is satisfied
- **On suspicion** - When something feels incomplete

## Model Instructions

Be adversarial about verification:
- Don't assume code that looks right works right
- Don't trust comments or function names
- Trace actual execution paths
- Look for missing connections between components
- Check that UI actually renders and is reachable

**Your job is to find the gap between "code exists" and "feature works".**
