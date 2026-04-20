# Plan: [Title]

## Metadata
- **ID**: plan-[YYYY-MM-DD]-[short-name]
- **Status**: draft
- **Specification**: [Link to locard/specs/spec-file.md]
- **Created**: [YYYY-MM-DD]

## Executive Summary
[Brief overview of the implementation approach chosen and why. Reference the specification's selected approach.]

## Success Metrics
[Copy from specification and add implementation-specific metrics]
- [ ] All specification criteria met
- [ ] Test coverage >90%
- [ ] Performance benchmarks achieved
- [ ] Zero critical security issues
- [ ] Documentation complete

## Acceptance Test Matrix (MANDATORY)

This matrix maps EVERY acceptance criterion from the spec to specific tests. TDD will generate these tests BEFORE implementation begins.

| AC ID | Acceptance Criterion | Phase | Test Type | Test Location |
|-------|---------------------|-------|-----------|---------------|
| AC1 | [Copy exact text from spec] | Phase 1 | Integration | `tests/integration/test_*.py` |
| AC2 | [Copy exact text from spec] | Phase 1 | Unit | `tests/unit/test_*.py` |
| AC3 | [Copy exact text from spec] | Phase 2 | E2E | `tests/e2e/test_*.py` |

**Coverage Requirements**:
- [ ] Every AC has at least one test
- [ ] User-facing ACs have integration or E2E tests (not just unit)
- [ ] Error handling ACs have explicit error case tests
- [ ] Security constraints have adversarial tests

---

## Phase Breakdown

### Phase 1: [Descriptive Name]
**Dependencies**: None

#### Objectives
- [Clear, single objective for this phase]
- [What value does this phase deliver?]

#### Deliverables
- [ ] [Specific deliverable 1]
- [ ] [Specific deliverable 2]
- [ ] [Tests for this phase]
- [ ] [Documentation updates]

#### Implementation Details
[Specific technical approach for this phase. Include:
- Key files/modules to create or modify
- Architectural decisions
- API contracts
- Data models]

#### Acceptance Criteria
- [ ] [Testable criterion 1]
- [ ] [Testable criterion 2]
- [ ] All tests pass
- [ ] Code review completed

#### Acceptance Test Design (MANDATORY)
Map each AC this phase addresses to specific test cases:

| AC | Test Type | Test Description | Input | Expected Output |
|----|-----------|------------------|-------|-----------------|
| AC1.1 | Integration | [What behavior is verified] | [Test input] | [Expected result] |
| AC1.2 | Unit | [What behavior is verified] | [Test input] | [Expected result] |

**Edge Cases**:
- [Edge case 1]: [How it will be tested]
- [Edge case 2]: [How it will be tested]

**Error Cases**:
- [Error condition]: [Expected behavior] → [Test assertion]

#### Additional Test Plan
- **Defensive Tests**: [Additional coverage beyond AC verification]
- **Performance Tests**: [If applicable per spec]
- **Manual Testing**: [Scenarios requiring human verification]

#### Rollback Strategy
[How to revert this phase if issues arise]

#### Risks
- **Risk**: [Specific risk for this phase]
  - **Mitigation**: [How to address]

---

### Phase 2: [Descriptive Name]
**Dependencies**: Phase 1

[Repeat structure for each phase]

---

### Phase 3: [Descriptive Name]
**Dependencies**: Phase 2

[Continue for all phases]

## Dependency Map
```
Phase 1 ──→ Phase 2 ──→ Phase 3
             ↓
         Phase 4 (optional)
```

## Resource Requirements
### Development Resources
- **Engineers**: [Expertise needed]
- **Environment**: [Dev/staging requirements]

### Infrastructure
- [Database changes]
- [New services]
- [Configuration updates]
- [Monitoring additions]

## Integration Points
### External Systems
- **System**: [Name]
  - **Integration Type**: [API/Database/Message Queue]
  - **Phase**: [Which phase needs this]
  - **Fallback**: [What if unavailable]

### Internal Systems
[Repeat structure]

## Risk Analysis
### Technical Risks
| Risk | Probability | Impact | Mitigation | Owner |
|------|------------|--------|------------|-------|
| [Risk 1] | L/M/H | L/M/H | [Strategy] | [Name] |

### Schedule Risks
| Risk | Probability | Impact | Mitigation | Owner |
|------|------------|--------|------------|-------|
| [Risk 1] | L/M/H | L/M/H | [Strategy] | [Name] |

## Validation Checkpoints
1. **After Phase 1**: [What to validate]
2. **After Phase 2**: [What to validate]
3. **Before Production**: [Final checks]

## Monitoring and Observability
### Metrics to Track
- [Metric 1: Description and threshold]
- [Metric 2: Description and threshold]

### Logging Requirements
- [What to log and at what level]
- [Retention requirements]

### Alerting
- [Alert condition and severity]
- [Who to notify]

## Documentation Updates Required
- [ ] API documentation
- [ ] Architecture diagrams
- [ ] Runbooks
- [ ] User guides
- [ ] Configuration guides

## Post-Implementation Tasks
- [ ] Performance validation
- [ ] Security audit
- [ ] Load testing
- [ ] User acceptance testing
- [ ] Monitoring validation

## Consultation Log
<!-- MANDATORY: Consultation is ENABLED BY DEFAULT. Skip ONLY if user explicitly said "without consultation" -->

### First Consultation (After Initial Plan)
**Date**: [YYYY-MM-DD]
**Models Consulted**: GPT-5 Codex, Gemini Pro
**Key Feedback**:
- [Model]: [Feasibility assessment]
- [Model]: [Missing considerations]
- [Model]: [Risk identification]
**Plan Adjustments**:
- [Phase X]: [How modified based on feedback]

### Second Consultation (After Human Review)
**Date**: [YYYY-MM-DD]
**Models Consulted**: GPT-5 Codex, Gemini Pro
**Key Feedback**:
- [Model]: [Validation of changes]
- [Model]: [Final suggestions]
**Plan Adjustments**:
- [Phase X]: [How modified based on feedback]

### Red Team Security Review (MANDATORY)
**Date**: [YYYY-MM-DD]
**Command**: `consult --model gemini --type red-team-plan plan XXXX`
**Findings**:

| Severity | Issue | Phase | Secure Alternative |
|----------|-------|-------|-------------------|
| CRITICAL | [None or describe] | [Which phase] | [Implementation fix] |
| HIGH | [None or describe] | [Which phase] | [Implementation fix] |
| MEDIUM | [None or describe] | [Which phase] | [Recommendation] |

**Dependency Vulnerabilities**:
| Dependency | Version | CVE | Risk | Mitigation |
|------------|---------|-----|------|------------|
| [name] | [ver] | [CVE-XXXX or None] | HIGH/MED/LOW | [Action taken] |

**Plan Adjustments**:
- [Phase X]: [How modified to address security finding]

**Verdict**: [APPROVE / REQUEST_CHANGES - ALL findings resolved]

Note: All consultation and security review feedback has been incorporated directly into the relevant phases above.

## Approval
- [ ] Technical Lead Review
- [ ] Engineering Manager Approval
- [ ] Resource Allocation Confirmed
- [ ] Expert AI Consultation Complete
- [ ] Red Team Security Review Complete (no unresolved findings)

## Change Log
| Date | Change | Reason | Author |
|------|--------|--------|--------|
| [Date] | [What changed] | [Why] | [Who] |

## Notes
[Additional context, assumptions, or considerations]

---

## Amendment History

This section tracks all TICK amendments to this plan. TICKs modify both the spec and plan together as an atomic unit.

<!-- When adding a TICK amendment, add a new entry below this line in chronological order -->

<!--
### TICK-001: [Amendment Title] (YYYY-MM-DD)

**Changes**:
- [Phase added]: [Description of new phase]
- [Phase modified]: [What was updated]
- [Implementation steps]: [New steps added]

**Review**: See `reviews/####-name-tick-001.md`

---
-->