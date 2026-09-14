#!/bin/bash
export NO_PROXY=127.0.0.1,localhost
PY=".venv/Scripts/python.exe"
: > _regress.log
for f in *_test.py; do
  case "$f" in
    llm_cache_test.py|prefix_cache_test.py|zhipu_cache_test.py) echo "SKIP(slow-live) $f" >> _regress.log; continue;;
  esac
  out=$($PY "$f" 2>&1); rc=$?
  echo "=== $f rc=$rc" >> _regress.log
  echo "$out" | grep -E "通过 / [0-9]+ 失败|通过 / [0-9]+ 失败 /|结果：|测试：" | tail -2 >> _regress.log
  if [ $rc -ne 0 ]; then echo "$out" | tail -15 >> _regress.log; fi
done
echo "ALLDONE" >> _regress.log
