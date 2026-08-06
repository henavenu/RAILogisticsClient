"""
Proper JS template-literal / string balance checker.
Tracks a state stack so we handle nested template literals correctly.
States: 'code', 'single', 'double', 'template', 'expr' (inside ${...} of a template)
"""

content = open(r'C:\Users\shara\Cursor Projects\rai-trust-platform\pap\ui\dashboard.html', encoding='utf-8').read()
# Try new marker first (after PAP/Dashboard split refactor), fall back to old
start = content.find("const _PAP_PORT = 3002")
if start < 0:
    start = content.find("const API = '';")
end   = content.rfind('</script>')
js    = content[start:end]

# State machine
# stack entries: 'code' | 'single' | 'double' | 'template' | 'expr'
stack = ['code']
i = 0
line = 1
errors = []

def state():
    return stack[-1]

while i < len(js):
    c = js[i]
    if c == '\n':
        line += 1

    # Handle escape sequences in strings / templates
    if c == '\\' and state() in ('single', 'double', 'template'):
        i += 2  # skip escaped char
        continue

    st = state()

    if st == 'single':
        if c == "'":
            stack.pop()
    elif st == 'double':
        if c == '"':
            stack.pop()
    elif st == 'template':
        if c == '`':
            stack.pop()
        elif c == '$' and i + 1 < len(js) and js[i+1] == '{':
            stack.append('expr')
            i += 1  # skip the '{'
    elif st == 'expr':
        # Inside ${...} — track nested braces
        if c == '{':
            stack.append('expr_brace')
        elif c == '}':
            stack.pop()   # close the expr
        elif c == "'":
            stack.append('single')
        elif c == '"':
            stack.append('double')
        elif c == '`':
            stack.append('template')
    elif st == 'expr_brace':
        if c == '{':
            stack.append('expr_brace')
        elif c == '}':
            stack.pop()
        elif c == "'":
            stack.append('single')
        elif c == '"':
            stack.append('double')
        elif c == '`':
            stack.append('template')
    else:  # 'code'
        if c == "'":
            stack.append('single')
        elif c == '"':
            stack.append('double')
        elif c == '`':
            stack.append('template')

    i += 1

print(f"Script length: {len(js):,} chars, {line} lines")
print(f"Stack at end: {stack}")
if stack == ['code']:
    print("SYNTAX CHECK: OK — all strings/templates balanced")
else:
    print(f"SYNTAX CHECK: POSSIBLE ISSUE — unclosed state(s): {stack[1:]}")
