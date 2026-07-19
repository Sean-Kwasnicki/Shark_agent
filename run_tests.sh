#!/usr/bin/env bash
# Full offline test suite — run before every deploy.
set -e
for t in test_core test_learning test_economics test_collector test_intelligence test_hardening test_feed test_opportunity; do
  echo "== $t =="
  python3 $t.py > /tmp/$t.log 2>&1 && tail -1 /tmp/$t.log || (cat /tmp/$t.log; exit 1)
done
for t in test_learning2 test_learning3; do
  echo "== $t (benchmark, slow) =="
  python3 $t.py > /tmp/$t.log 2>&1 && tail -1 /tmp/$t.log || (cat /tmp/$t.log; exit 1)
done
echo "ALL 10 SUITES PASSED"
