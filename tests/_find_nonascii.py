import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

with open(r'C:\Users\shara\Cursor Projects\rai-trust-platform\tests\run_sentinel_scenarios.py', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines, 1):
    bad = [(j+1, ch) for j, ch in enumerate(line) if ord(ch) > 127]
    if bad:
        print(f"Line {i}: {repr(line.rstrip())}")
        for col, ch in bad:
            print(f"  col {col}: U+{ord(ch):04X} {repr(ch)}")
