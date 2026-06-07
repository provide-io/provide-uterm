import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { ToastStack } from './toast-stack.js';

describe('uterm-toast-stack', () => {
  let el: ToastStack;

  beforeEach(async () => {
    el = document.createElement('uterm-toast-stack') as ToastStack;
    document.body.appendChild(el);
    await el.updateComplete;
  });

  afterEach(() => {
    el.remove();
  });

  it('renders a toast container', () => {
    expect(el.shadowRoot).toBeTruthy();
    expect(el.shadowRoot!.querySelector('.toast-container')).toBeTruthy();
  });
});
