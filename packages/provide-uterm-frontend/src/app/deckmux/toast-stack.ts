import { css, html, LitElement } from "lit";
import { customElement } from "lit/decorators.js";

@customElement("uterm-toast-stack")
export class ToastStack extends LitElement {
  static override styles = css`
    :host {
      display: block;
      position: fixed;
      bottom: 20px;
      right: 20px;
    }
    ::slotted(.toast) {
      background: var(--dm-bg, #333);
      color: white;
      padding: 12px;
      border-radius: 4px;
      margin-top: 8px;
    }
  `;

  override render() {
    return html`
      <div class="toast-container">
        <slot></slot>
      </div>
    `;
  }
}
