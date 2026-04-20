# Code Audit Prompt

## Purpose

Independent verification that tests actually test what the spec requires, not just that they pass.

## The Problem This Solves

Tests can pass while completely missing the point:
- Tests verify implementation details, not behavior
- Tests are too weak (always pass regardless of bugs)
- Tests verify the wrong thing (test what was built, not what was specified)
- Coverage is high but meaningful coverage is low
- Mocks hide real integration failures

This audit catches the gap between "tests pass" and "tests verify the spec."

## Your Task

You are a fresh set of eyes. You have NOT seen this code before. Your job:

1. **Re-run all tests** in a clean environment
2. **Verify tests match spec** - Do tests actually verify what the spec requires?
3. **Assess test quality** - Are tests meaningful or just green?
4. **Find test smells** - Flaky, over-mocked, wrong assertions

## Audit Checklist

### 1. Test Execution (Fresh Environment)

```bash
# Run full test suite
# Note any failures, flaky tests, or environment issues
```

Report:
- [ ] All tests pass in fresh environment
- [ ] No flaky tests detected (run 3x if suspicious)
- [ ] No environment-specific failures
- [ ] Test runtime reasonable (no timeouts)

### 2. Spec-to-Test Mapping

For EACH acceptance criterion in the spec:

```
AC: [Quote the exact AC text]

Test(s) that verify this AC:
→ [test file:line] - [test name]
→ [test file:line] - [test name]

VERDICT: [COVERED | PARTIAL | NOT COVERED | WRONG TEST]

If PARTIAL or WRONG:
→ Gap: [What's missing or incorrect]
→ Risk: [What bug could slip through]
```

### 3. Test Quality Assessment

For each test file, assess:

| Smell | Check |
|-------|-------|
| **Over-mocking** | Are internal modules mocked? (Should use real implementations) |
| **Wrong layer** | Does test verify behavior or implementation details? |
| **Weak assertions** | Would test catch actual bugs or just verify code runs? |
| **Missing edge cases** | Are boundary conditions tested? |
| **Happy path only** | Are error paths tested? |
| **Test pollution** | Do tests depend on order or shared state? |
| **False coverage** | Is code executed but not actually asserted? |

### 4. Implementation-Test Alignment

Check that the implementation actually does what tests expect:

```
Test expects: [What the test asserts]
Implementation does: [What the code actually does]
Spec requires: [What the AC says]

ALIGNED: [YES | NO | PARTIAL]

If NO or PARTIAL:
→ Mismatch: [Description of the gap]
```

## Red Flags

### Tests That Always Pass
```javascript
// BAD: This test passes even if login is completely broken
test('login works', async () => {
  const result = await login(mockUser);
  expect(result).toBeDefined(); // Too weak!
});
```

### Testing Mocks Instead of Code
```javascript
// BAD: Testing that the mock returns what we told it to
jest.mock('./database');
test('gets user', async () => {
  database.getUser.mockResolvedValue({ id: 1 });
  const user = await getUser(1);
  expect(user.id).toBe(1); // Just testing the mock!
});
```

### Testing Implementation, Not Behavior
```javascript
// BAD: Breaks if implementation changes, even if behavior is same
test('uses bcrypt', () => {
  expect(hashPassword).toHaveBeenCalledWith(password, 10);
});

// GOOD: Tests behavior
test('password is hashed', async () => {
  const hash = await hashPassword('secret');
  expect(hash).not.toBe('secret');
  expect(await verifyPassword('secret', hash)).toBe(true);
});
```

### Missing Integration Tests
```
Unit tests: 50 ✓
Integration tests: 0 ✗

Risk: All units work in isolation, system fails when connected
```

## Output Format

```
## Code Audit Report

### Test Execution
- Tests run: [N]
- Passed: [N]
- Failed: [N]
- Flaky: [N]
- Runtime: [Xs]

### Spec Coverage

| AC | Tests | Verdict | Gap |
|----|-------|---------|-----|
| AC1: [short description] | test_foo.py:42 | COVERED | - |
| AC2: [short description] | - | NOT COVERED | No test exists |
| AC3: [short description] | test_bar.py:15 | WRONG TEST | Tests mock, not real behavior |

### Test Quality Issues

**CRITICAL** (tests give false confidence):
- [Issue]: [File:line] - [Description]

**HIGH** (significant coverage gap):
- [Issue]: [File:line] - [Description]

**MEDIUM** (test smell, should fix):
- [Issue]: [File:line] - [Description]

### Implementation Alignment

| Component | Test Expects | Code Does | Spec Requires | Aligned? |
|-----------|--------------|-----------|---------------|----------|
| [name] | [X] | [Y] | [Z] | YES/NO |

### Summary

**Test Suite Health**: [HEALTHY | WEAK | BROKEN]

**Spec Coverage**: [X/Y ACs verified] ([N]%)

**Critical Gaps**:
1. [Most important gap]
2. [Second most important]

**Recommendation**: [APPROVE | FIX TESTS | REWRITE TESTS]
```

## Verdict Meanings

| Verdict | Meaning |
|---------|---------|
| **APPROVE** | Tests meaningfully verify spec, safe to proceed |
| **FIX TESTS** | Tests exist but have issues, fix before proceeding |
| **REWRITE TESTS** | Tests fundamentally flawed, need redesign |

## When to Use This Review

- **MANDATORY** after AC Verify passes (in Evaluate phase)
- After any significant test changes
- When tests pass but behavior seems wrong
- Before marking implementation complete

## Model Instructions

Be adversarial about test quality:
- Assume tests are wrong until proven right
- Look for ways bugs could slip through
- Check if tests would catch real-world failures
- Verify tests match spec intent, not just implementation

**Your job is to find the gap between "tests pass" and "tests verify the spec."**
