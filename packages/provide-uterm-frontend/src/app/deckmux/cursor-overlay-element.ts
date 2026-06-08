import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";
import { classMap } from "lit/directives/class-map.js";
import { styleMap } from "lit/directives/style-map.js";

export interface OverlayUser {
  userId: string;
  name: string;
  color: string;
  pin?: { line: number };
  selection?: { startLine: number; endLine: number };
}

@customElement("uterm-cursor-overlay")
export class CursorOverlayElement extends LitElement {
  @property({ type: Array }) users: OverlayUser[] = [];
  @property({ type: String }) ownerId: string | null = null;
  @property({ type: Boolean }) visible: boolean = true;

  static override styles = css`
    :host {
      display: block;
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
      z-index: 15;
    }
    .dm-cursor-overlay {
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
      overflow: hidden;
    }
    .dm-pin {
      position: absolute;
      left: 0;
      display: flex;
      align-items: center;
      gap: 4px;
      height: 1lh;
    }
    .dm-pin-bar {
      width: 2px;
      height: 100%;
      background: var(--dm-user-color, #6b7280);
      border-radius: 1px;
      flex-shrink: 0;
    }
    .dm-pin-label {
      font-size: 11px;
      color: #e6edf3;
      background: color-mix(in srgb, var(--dm-user-color, #6b7280) 90%, #161b22);
      padding: 1px 5px 1px 4px;
      border-radius: 0 3px 3px 0;
      white-space: nowrap;
      line-height: 1lh;
    }
    .dm-pin--owner .dm-pin-label::before {
      content: "";
    }
    .dm-selection {
      position: absolute;
      left: 0;
      width: 100%;
      background: color-mix(in srgb, var(--dm-user-color, #6b7280) 20%, transparent);
      border-left: 2px solid color-mix(in srgb, var(--dm-user-color, #6b7280) 60%, transparent);
      pointer-events: none;
    }
  `;

  override render() {
    return html`
      <div class="dm-cursor-overlay" style=${styleMap({ display: this.visible ? "" : "none" })}>
        ${this.users.map((u) => this._renderSelection(u))}
        ${this.users.map((u) => this._renderPin(u))}
      </div>
    `;
  }

  private _renderSelection(u: OverlayUser) {
    if (!u.selection) return null;
    const { startLine, endLine } = u.selection;
    const lineCount = Math.max(1, endLine - startLine + 1);
    
    return html`
      <div 
        class="dm-selection" 
        data-user-id=${u.userId}
        style=${styleMap({
          "--dm-user-color": u.color,
          top: startLine + "lh",
          height: lineCount + "lh",
        })}
      ></div>
    `;
  }

  private _renderPin(u: OverlayUser) {
    if (!u.pin) return null;
    const isOwner = this.ownerId === u.userId;
    const icon = isOwner ? "⌨️" : "📌";
    
    return html`
      <div 
        class=${classMap({ "dm-pin": true, "dm-pin--owner": isOwner })} 
        data-user-id=${u.userId}
        style=${styleMap({
          "--dm-user-color": u.color,
          top: u.pin.line + "lh",
        })}
      >
        <div class="dm-pin-bar"></div>
        <span class="dm-pin-label">${icon} ${u.name}</span>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "uterm-cursor-overlay": CursorOverlayElement;
  }
}
