#!/usr/bin/env bash
# Full offline test suite — run before every deploy.
set -e
for t in test_core test_learning test_economics test_collector test_intelligence test_hardening; do
  echo "== $t =="
  python3 $t.py > /tmp/$t.log 2>&1 && tail -1 /tmp/$t.log || (cat /tmp/$t.log; exit 1)
done
echo "== test_learning2 (benchmark, ~60s) =="
python3 test_learning2.py > /tmp/l2.log 2>&1 && tail -1 /tmp/l2.log || (cat /tmp/l2.log; exit 1)
echo "ALL 7 SUITES PASSED"
