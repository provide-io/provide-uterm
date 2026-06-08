import { LitElement, html, css } from "lit";
import { customElement, property } from "lit/decorators.js";
import { classMap } from "lit/directives/class-map.js";
import { styleMap } from "lit/directives/style-map.js";

export interface EdgeIndicatorUser {
  userId: string;
  slot: number;
  color: string;
  range: { top: number; height: number };
  options: {
    isOwner?: boolean;
    selection?: { top: number; height: number };
    pin?: number;
    name?: string;
    idle?: boolean;
  };
}

@customElement("uterm-edge-indicators")
export class EdgeIndicatorsElement extends LitElement {
  @property({ type: Array }) users: EdgeIndicatorUser[] = [];
  @property({ type: Boolean }) namesVisible = false;

  static override styles = css`
    :host {
      display: contents;
    }
    .dm-edge-track {
      position: absolute;
      top: 0;
      right: 0;
      width: 35px; /* 7 slots × 5px (4px bar + 1px gap) */
      height: 100%;
      background: rgba(13, 17, 23, 0.7);
      border-left: 1px solid #30363d;
      pointer-events: none;
      z-index: 20;
      overflow: hidden;
    }
    .dm-edge-bar {
      position: absolute;
      width: 4px;
      min-height: 2px;
      background: color-mix(in srgb, var(--dm-user-color, #6b7280) 60%, transparent);
      border-radius: 1px;
      transition: opacity 0.2s ease;
    }
    .dm-edge-bar--owner {
      box-shadow: 0 0 3px 1px color-mix(in srgb, var(--dm-user-color, #f97316) 50%, transparent);
      z-index: 1;
    }
    .dm-edge-bar--idle {
      opacity: 0.2;
    }
    .dm-edge-selection {
      position: absolute;
      left: 0;
      width: 100%;
      background: color-mix(in srgb, var(--dm-user-color, #6b7280) 80%, transparent);
      border-radius: 1px;
      min-height: 2px;
    }
    .dm-edge-pin {
      position: absolute;
      left: 50%;
      transform: translateX(-50%);
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #fff;
      box-shadow: 0 0 3px 1px color-mix(in srgb, var(--dm-user-color, #fff) 70%, transparent);
    }
    .dm-edge-name {
      position: absolute;
      right: 10px;
      top: 50%;
      transform: translateY(-50%);
      font-size: 10px;
      color: #e6edf3;
      white-space: nowrap;
      background: rgba(22, 27, 34, 0.9);
      padding: 1px 4px;
      border-radius: 3px;
      pointer-events: none;
    }
  `;

  override render() {
    return html`
      <div class="dm-edge-track">
        ${this.users.map((u) => this._renderUser(u))}
      </div>
    `;
  }

  private _renderUser(user: EdgeIndicatorUser) {
    const { slot, color, range, options } = user;
    const isOwner = options.isOwner ?? false;
    const idle = options.idle ?? false;

    const barClasses = {
      "dm-edge-bar": true,
      "dm-edge-bar--owner": isOwner,
      "dm-edge-bar--idle": idle,
    };

    const SLOT_STEP = 5; // 4 + 1
    const barStyles = {
      left: `${slot * SLOT_STEP}px`,
      top: `${range.top * 100}%`,
      height: `${range.height * 100}%`,
      "--dm-user-color": color,
    };

    return html`
      <div class=${classMap(barClasses)} style=${styleMap(barStyles)} data-user-id=${user.userId}>
        ${options.selection ? this._renderSelection(options.selection, range) : ""}
        ${options.pin !== undefined ? this._renderPin(options.pin, range) : ""}
        ${options.name ? html`<span class="dm-edge-name" style=${styleMap({ display: this.namesVisible ? "" : "none" })}>${options.name}</span>` : ""}
      </div>
    `;
  }

  private _renderSelection(selection: { top: number; height: number }, range: { top: number; height: number }) {
    const selStyles = {
      top: `${((selection.top - range.top) / range.height) * 100}%`,
      height: `${(selection.height / range.height) * 100}%`,
    };
    return html`<div class="dm-edge-selection" style=${styleMap(selStyles)}></div>`;
  }

  private _renderPin(pin: number, range: { top: number; height: number }) {
    const pinOffset = (pin - range.top) / range.height;
    const pinStyles = {
      top: `${pinOffset * 100}%`,
    };
    return html`<div class="dm-edge-pin" style=${styleMap(pinStyles)}></div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "uterm-edge-indicators": EdgeIndicatorsElement;
  }
}
