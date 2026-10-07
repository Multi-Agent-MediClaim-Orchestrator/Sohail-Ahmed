#!/usr/bin/env bash
# Fails when a provider key name or a key-shaped literal appears outside infra/llm-gateway/ (04-04 task 12, architecture rule).
# Usage: scripts/ci_no_provider_keys.sh [root]
set -u
ROOT="${1:-.}"
fail=0
names='GEMINI_API_KEY|GOOGLE_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY'
literals='AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9]{32,}'
excl=(--exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=llm-gateway --exclude-dir=__pycache__ --exclude-dir=.pytest_cache \
      --exclude=ci_no_provider_keys.sh --exclude='*.md' --exclude='*.jsonl' --exclude='test_*.py')
if grep -rInE "${excl[@]}" "($names)" "$ROOT" ; then echo "provider key NAME found outside infra/llm-gateway" >&2; fail=1; fi
if grep -rInE "${excl[@]}" "($literals)" "$ROOT" ; then echo "key-shaped LITERAL found outside infra/llm-gateway" >&2; fail=1; fi
exit $fail
