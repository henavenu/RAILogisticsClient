import re

content = open(r'C:\Users\shara\Cursor Projects\rai-trust-platform\pap\ui\dashboard.html', encoding='utf-8').read()
start = content.find("const API = '';")
end   = content.rfind('</script>')
js    = content[start:end]

# Walk the JS tracking string/template depth to find unbalanced backticks
# This is intentionally simplified (ignores escaped chars, comments)
depth = 0
in_single = False
in_double = False
in_template = 0   # nesting level of template literals
prev = ''
errors = []
line = 1

for i, c in enumerate(js):
    if c == '\n':
        line += 1
    if prev == '\\':
        prev = ''
        continue
    if c == "'" and not in_double and in_template == 0:
        in_single = not in_single
    elif c == '"' and not in_single and in_template == 0:
        in_double = not in_double
    elif c == '`' and not in_single and not in_double:
        in_template += 1 if in_template % 2 == 0 else -1
    prev = c

print(f"Script block: {len(js):,} chars, up to line {line}")
print(f"Template literal depth at end (0=balanced): {in_template}")
print(f"Single-quote string open at end: {in_single}")
print(f"Double-quote string open at end: {in_double}")
