"""Resolver evaluation harness for nonprofit website identity.

This package is intentionally small and file-based:
- input: labeled CSV rows
- output: JSONL per-row decisions + compact summary

The goal is to compare resolver strategies on the same gold set
before changing the production website-resolution path.
"""

