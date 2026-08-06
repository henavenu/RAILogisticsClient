"""Find exact HTML line numbers for JS brace problems."""
content = open(r'C:\Users\shara\Cursor Projects\rai-trust-platform\pap\ui\dashboard.html', encoding='utf-8').read()

# Find the inline script block
i1 = content.find('<script>')
i2 = content.find('<script>', i1 + 1)
script_html_line = content[:i2].count('\n') + 1
js_end = content.find('</script>', i2)
js_block = content[i2 + 8 : js_end]

print(f'Inline <script> tag at HTML line: {script_html_line}')
print(f'JS block length: {len(js_block):,} chars')
print()

js_lines = js_block.split('\n')
# Problem lines from checker (1-indexed within JS block)
problem_js = [275, 1903, 1957, 3120]
for jl in problem_js:
    html_line = script_html_line + jl
    snippet = js_lines[jl - 1].strip()[:120] if jl <= len(js_lines) else '(out of range)'
    print(f'JS line {jl:4d}  ->  HTML line {html_line:4d}:  {snippet}')
