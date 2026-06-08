import { css, html, LitElement, nothing } from "lit";
import { customElement, property } from "lit/decorators.js";
import type { ContextAction, DeckMuxUser } from "./types.js";

@customElement("uterm-context-menu")
export class ContextMenu extends LitElement {
  @property({ type: Object }) user!: DeckMuxUser;
  @property({ type: Array }) actions: ContextAction[] = [];

  static override styles = css`
    :host {
      display: block;
      position: fixed;
      z-index: 200;
      min-width: 180px;
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 8px;
      box-shadow: 0 8px 24px rgba(1, 4, 9, 0.6);
      overflow: hidden;
      padding: 4px 0;
      box-sizing: border-box;
    }
    .dm-context-menu-header {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-bottom: 1px solid #30363d;
      font-size: 13px;
      font-weight: 600;
      color: #e6edf3;
    }
    .dm-context-menu-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      flex-shrink: 0;
    }
    .dm-context-menu-item {
      display: flex;
      align-items: center;
      gap: 10px;
      width: 100%;
      padding: 8px 12px;
      background: none;
      border: none;
      color: #c9d1d9;
      font-size: 13px;
      cursor: pointer;
      text-align: left;
      transition: background 0.1s ease;
      box-sizing: border-box;
    }
    .dm-context-menu-item:hover {
      background: #21262d;
      color: #e6edf3;
    }
    .dm-context-menu-item--danger {
      color: #f85149;
    }
    .dm-context-menu-item--danger:hover {
      background: rgba(248, 81, 73, 0.1);
      color: #ff7b72;
    }
    .dm-context-menu-icon {
      width: 16px;
      text-align: center;
      flex-shrink: 0;
      font-size: 14px;
    }
    .dm-context-menu-label-wrap {
      display: flex;
      flex-direction: column;
      gap: 1px;
    }
    .dm-context-menu-label {
      line-height: 1.3;
    }
    .dm-context-menu-sublabel {
      font-size: 11px;
      color: #8b949e;
      line-height: 1.2;
    }
  `;

  override render() {
    if (!this.user) return nothing;

    return html`
      <div class="dm-context-menu-header">
        <span class="dm-context-menu-dot" style="background: ${this.user.color}"></span>
        <span>${this.user.name}</span>
      </div>
      ${this.actions.map(
        (action) => html`
          <button
            class="dm-context-menu-item ${action.danger ? "dm-context-menu-item--danger" : ""}"
            @click=${() => action.onClick()}
          >
            <span class="dm-context-menu-icon">${action.icon}</span>
            <span class="dm-context-menu-label-wrap">
              <span class="dm-context-menu-label">${action.label}</span>
              ${action.sublabel ? html`<span class="dm-context-menu-sublabel">${action.sublabel}</span>` : nothing}
            </span>
          </button>
        `,
      )}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "uterm-context-menu": ContextMenu;
  }
}
