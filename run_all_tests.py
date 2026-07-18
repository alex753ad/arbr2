#!/usr/bin/env python3
"""
run_all_tests.py — Запуск всех 222 тестов одной командой.

Использование:
  python run_all_tests.py          # из корня проекта
  python run_all_tests.py --quick  # только core (108, < 0.01с)

Код возврата: 0 = все тесты прошли, 1 = есть ошибки.
"""
import subprocess, sys, time, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

suites = [
    ('Core (risk+scoring+types+utils)', 'pairs_scanner/run_tests.py'),
    ('Position Manager (trailing)',      'pairs_scanner/tests/unit/test_position_manager.py'),
    ('Engine (monitor+entry+scanner)',   'pairs_scanner/tests/unit/test_engine.py'),
]

if '--quick' not in sys.argv:
    suites.append(
        ('Pair Analysis + Backtesting',  'pairs_scanner/tests/unit/test_pair_analysis.py'),
    )

t0 = time.time()
total_pass = 0
total_fail = 0
failed_suites = []

for name, script in suites:
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")
    result = subprocess.run([sys.executable, script], capture_output=False)
    if result.returncode != 0:
        failed_suites.append(name)
        total_fail += 1
    else:
        total_pass += 1

elapsed = time.time() - t0
print(f"\n{'='*60}")
print(f"  ИТОГО: {total_pass}/{total_pass + total_fail} суит прошло, {elapsed:.2f}с")
if failed_suites:
    print(f"  ПРОВАЛЕНО: {', '.join(failed_suites)}")
print(f"{'='*60}")

sys.exit(1 if failed_suites else 0)
