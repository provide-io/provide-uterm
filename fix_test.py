import re

with open('packages/provide-uterm-frontend/src/hijack-extra.test_part1.ts', 'r') as f:
    content = f.read()

# Fix q()
content = content.replace('''function q(container: HTMLElement, name: string): HTMLElement | null {
  return container.querySelector(`[id$="-${name}"]`);
}''', '''function q(container: HTMLElement, name: string): HTMLElement | null {
  let el = container.querySelector<HTMLElement>(`[id$="-${name}"]`);
  if (el) return el;
  const prompt = container.querySelector("uterm-approval-prompt");
  if (prompt?.shadowRoot) {
    el = prompt.shadowRoot.querySelector<HTMLElement>(`[id$="-${name}"]`);
    if (el) return el;
  }
  return null;
}''')

# Make tests async and add await
content = re.sub(
    r'it\("renders admin approval modal with approve/reject buttons after a hello frame upgrades a viewer to admin", \(\) => \{',
    r'it("renders admin approval modal with approve/reject buttons after a hello frame upgrades a viewer to admin", async () => {',
    content
)
content = re.sub(
    r'it\("renders the statusbar \(non-admin\) UX when neither config nor server role is admin", \(\) => \{',
    r'it("renders the statusbar (non-admin) UX when neither config nor server role is admin", async () => {',
    content
)
content = re.sub(
    r'it\("falls back to constructor role when hello carries no role field", \(\) => \{',
    r'it("falls back to constructor role when hello carries no role field", async () => {',
    content
)

# Add await before expect(q(...))
content = re.sub(
    r'(\s+)expect\(q\(container, "approve"\)\)',
    r'\1await new Promise((r) => setTimeout(r, 0));\1expect(q(container, "approve"))',
    content
)

content = content.replace(
    'const modal = container.querySelector(".hijack-approval-modal, .hijack-approval-statusbar");',
    'const modal = container.querySelector("uterm-approval-prompt")?.shadowRoot?.querySelector(".hijack-approval-modal, .hijack-approval-statusbar");'
)

with open('packages/provide-uterm-frontend/src/hijack-extra.test_part1.ts', 'w') as f:
    f.write(content)

