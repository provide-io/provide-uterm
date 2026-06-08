import re

with open('packages/provide-uterm-frontend/src/hijack-extra.test_part1.ts', 'r') as f:
    content = f.read()

content = content.replace(
    'await new Promise((r) => setTimeout(r, 0));',
    'const __p = container.querySelector("uterm-approval-prompt"); if (__p) await (__p as any).updateComplete;'
)

with open('packages/provide-uterm-frontend/src/hijack-extra.test_part1.ts', 'w') as f:
    f.write(content)

