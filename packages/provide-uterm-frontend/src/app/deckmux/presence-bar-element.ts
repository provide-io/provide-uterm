import { LitElement, html, css } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import { classMap } from "lit/directives/class-map.js";
import { styleMap } from "lit/directives/style-map.js";
import type { DeckMuxUser } from "./types.js";

export type PresenceUser = DeckMuxUser & { idle?: boolean; requesting?: boolean };

const ROLE_COLORS: Record<string, string> = {
  admin: "#f97316",
  operator: "#3b82f6",
  viewer: "#6b7280",
};

@customElement("uterm-presence-bar")
export class PresenceBar extends LitElement {
  @property({ type: Array }) users: PresenceUser[] = [];
  @property({ type: String }) ownerId: string | null = null;

  @state() private _namesVisible = false;
  @state() private _cursorsVisible = true;
  @state() private _ghostBoxVisible = true;

  static styles = css`
    :host {
      display: block;
      width: 100%;
      flex-shrink: 0;
    }
    .dm-presence-bar {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 6px 12px;
      background: #0d1117;
      border-bottom: 1px solid #30363d;
      min-height: 44px;
      box-sizing: border-box;
      user-select: none;
      position: relative;
      z-index: 10;
    }
    .dm-avatar-row {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: nowrap;
      overflow: hidden;
    }
    .dm-avatar-wrap {
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 3px;
      cursor: pointer;
      position: relative;
      transition: opacity 0.2s ease;
    }
    .dm-avatar-wrap--owner .dm-avatar {
      animation: dm-owner-pulse 2s ease-in-out infinite;
    }
    .dm-avatar-wrap--requesting .dm-avatar {
      box-shadow: 0 0 0 2px #eab308, 0 0 6px 2px rgba(234, 179, 8, 0.4);
      animation: none;
    }
    .dm-avatar--idle {
      opacity: 0.35;
    }
    .dm-avatar {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      background: color-mix(in srgb, var(--dm-user-color, #6b7280) 80%, #161b22);
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
      flex-shrink: 0;
      transition: box-shadow 0.2s ease, opacity 0.2s ease;
    }
    .dm-avatar-wrap:hover .dm-avatar {
      filter: brightness(1.15);
    }
    .dm-avatar-initials {
      font-size: 11px;
      font-weight: 600;
      color: #fff;
      line-height: 1;
      letter-spacing: 0.02em;
      pointer-events: none;
    }
    .dm-role-dot {
      position: absolute;
      bottom: 1px;
      right: 1px;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      border: 1.5px solid #0d1117;
      pointer-events: none;
    }
    .dm-typing-dot {
      position: absolute;
      top: 1px;
      right: 1px;
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: #22c55e;
      border: 1.5px solid #0d1117;
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.2s ease;
    }
    .dm-avatar-wrap--typing .dm-typing-dot {
      opacity: 1;
      animation: dm-typing-pulse 1s ease-in-out infinite;
    }
    .dm-avatar-name {
      font-size: 10px;
      color: #8b949e;
      white-space: nowrap;
      max-width: 56px;
      overflow: hidden;
      text-overflow: ellipsis;
      line-height: 1;
    }
    .dm-avatar-dims {
      font-size: 9px;
      color: #8b949e;
      white-space: nowrap;
      line-height: 1;
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    }
    .dm-count-badge {
      font-size: 11px;
      color: #8b949e;
      white-space: nowrap;
      margin-left: 4px;
    }
    .dm-toggles {
      display: flex;
      gap: 4px;
      margin-left: auto;
      flex-shrink: 0;
    }
    .dm-toggle-btn {
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 4px;
      border: 1px solid #30363d;
      background: transparent;
      color: #8b949e;
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
      line-height: 1.4;
    }
    .dm-toggle-btn:hover {
      background: #161b22;
      color: #e6edf3;
      border-color: #484f58;
    }
    .dm-toggle-btn--active {
      background: #161b22;
      color: #e6edf3;
      border-color: #58a6ff;
    }

    @keyframes dm-owner-pulse {
      0%, 100% {
        box-shadow: 0 0 0 2px var(--dm-user-color, #f97316), 0 0 0 4px transparent;
      }
      50% {
        box-shadow: 0 0 0 2px var(--dm-user-color, #f97316), 0 0 6px 4px color-mix(in srgb, var(--dm-user-color, #f97316) 40%, transparent);
      }
    }
    @keyframes dm-typing-pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.7); }
    }
  `;

  render() {
    return html`
      <div class="dm-presence-bar">
        <div class="dm-avatar-row">
          ${this.users.map((u) => this._renderAvatar(u))}
        </div>
        ${this.users.length > 0 ? html`<span class="dm-count-badge">${this.users.length} watching</span>` : ""}
        <div class="dm-toggles" role="toolbar" aria-label="Presence display options">
          <button
            type="button"
            class="dm-toggle-btn ${this._namesVisible ? "dm-toggle-btn--active" : ""}"
            aria-pressed="${this._namesVisible}"
            aria-label="Toggle participant names"
            @click="${this._toggleNames}"
          >Names</button>
          <button
            type="button"
            class="dm-toggle-btn ${this._cursorsVisible ? "dm-toggle-btn--active" : ""}"
            aria-pressed="${this._cursorsVisible}"
            aria-label="Toggle participant cursors"
            @click="${this._toggleCursors}"
          >Cursors</button>
          <button
            type="button"
            class="dm-toggle-btn ${this._ghostBoxVisible ? "dm-toggle-btn--active" : ""}"
            aria-pressed="${this._ghostBoxVisible}"
            aria-label="Toggle participant viewport dimensions"
            @click="${this._toggleDims}"
          >Dims</button>
        </div>
      </div>
    `;
  }

  private _renderAvatar(user: PresenceUser) {
    const isOwner = this.ownerId === user.userId;
    const wrapClasses = {
      "dm-avatar-wrap": true,
      "dm-avatar-wrap--owner": isOwner,
      "dm-avatar-wrap--typing": !!user.typing,
      "dm-avatar-wrap--requesting": !!user.requesting,
      "dm-avatar--idle": !!user.idle,
    };
    
    const roleColor = ROLE_COLORS[user.role] ?? ROLE_COLORS.viewer;
    const hasDims = user.cols > 0 && user.rows > 0;

    return html`
      <div
        class=${classMap(wrapClasses)}
        data-user-id=${user.userId}
        @mouseenter=${() => this.dispatchEvent(new CustomEvent("presence:hover-avatar", { detail: user.userId }))}
        @mouseleave=${() => this.dispatchEvent(new CustomEvent("presence:hover-out-avatar", { detail: user.userId }))}
        @click=${() => this.dispatchEvent(new CustomEvent("presence:click-avatar", { detail: user.userId }))}
      >
        <div class="dm-avatar" style=${styleMap({ "--dm-user-color": user.color })}>
          <span class="dm-avatar-initials">${user.initials.slice(0, 2)}</span>
          <span class="dm-role-dot" style=${styleMap({ background: roleColor })}></span>
          <span class="dm-typing-dot"></span>
        </div>
        <span class="dm-avatar-name" style=${styleMap({ display: this._namesVisible ? "" : "none" })}>${user.name}</span>
        <span class="dm-avatar-dims" style=${styleMap({ display: hasDims ? "" : "none" })}>${hasDims ? `${user.rows}×${user.cols}` : ""}</span>
      </div>
    `;
  }

  private _toggleNames() {
    this._namesVisible = !this._namesVisible;
    this.dispatchEvent(new CustomEvent("presence:toggle-names", { detail: this._namesVisible }));
  }

  private _toggleCursors() {
    this._cursorsVisible = !this._cursorsVisible;
    this.dispatchEvent(new CustomEvent("presence:toggle-cursors", { detail: this._cursorsVisible }));
  }

  private _toggleDims() {
    this._ghostBoxVisible = !this._ghostBoxVisible;
    this.dispatchEvent(new CustomEvent("presence:toggle-ghost-box", { detail: this._ghostBoxVisible }));
  }

  getAvatarElement(userId: string): HTMLElement | null {
    if (!this.shadowRoot) return null;
    const wraps = this.shadowRoot.querySelectorAll<HTMLElement>(".dm-avatar-wrap");
    for (const wrap of wraps) {
      if (wrap.dataset.userId === userId) return wrap;
    }
    return null;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "uterm-presence-bar": PresenceBar;
  }
}
