"""
Check brace/paren balance in the inline JS block of dashboard.html,
excluding characters inside strings and template literals.
"""
content = open(r'C:\Users\shara\Cursor Projects\rai-trust-platform\pap\ui\dashboard.html', encoding='utf-8').read()

# Find the inline script block (skip the Bootstrap CDN src= tag)
parts = content.split('<script>')
js_block = None
for p in parts[1:]:
    s = p.strip()
    if not (s.startswith('src=') or s.startswith('"')):
        js_block = p.split('</script>')[0]
        break

if not js_block:
    print('ERROR: Could not find inline JS block')
    raise SystemExit(1)

print(f'JS block: {len(js_block):,} chars')

braces = 0
parens = 0
in_str = None  # None | "'" | '"' | '`'
i = 0
line = 1
problems = []
brace_stack = []   # track opening brace line numbers
paren_stack = []

while i < len(js_block):
    c = js_block[i]
    if c == '\n':
        line += 1

    if in_str:
        if c == '\\':
            i += 2
            continue
        if c == in_str:
            in_str = None
    else:
        if c in ("'", '"', '`'):
            in_str = c
        elif c == '{':
            braces += 1
            brace_stack.append(line)
        elif c == '}':
            if brace_stack:
                brace_stack.pop()
            braces -= 1
            if braces < 0:
                problems.append(f'Extra closing brace at line {line}')
                braces = 0
        elif c == '(':
            parens += 1
            paren_stack.append(line)
        elif c == ')':
            if paren_stack:
                paren_stack.pop()
            parens -= 1
            if parens < 0:
                problems.append(f'Extra closing paren at line {line}')
                parens = 0
    i += 1

print(f'Brace balance : {braces}  (0 = OK)')
print(f'Paren balance : {parens}  (0 = OK)')

if problems:
    print('PROBLEMS FOUND:')
    for p in problems:
        print(' ', p)
else:
    print('No extra closes detected')

if braces != 0:
    print(f'UNCLOSED BRACES: {braces} open brace(s) never closed')
    print(f'  Last open brace(s) at lines: {brace_stack[-5:]}')
if parens != 0:
    print(f'UNCLOSED PARENS: {parens} open paren(s) never closed')
    print(f'  Last open paren(s) at lines: {paren_stack[-5:]}')

if braces == 0 and parens == 0 and not problems:
    print('ALL BALANCED — no brace/paren syntax errors')
