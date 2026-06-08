import { LitElement, html, css, PropertyValues } from "lit";
import { customElement, property } from "lit/decorators.js";
import { styleMap } from "lit/directives/style-map.js";

export interface GhostEntryState {
  userId: string;
  color: string;
  cols: number;
  rows: number;
  hidden: boolean;
  flash: boolean;
}

@customElement("uterm-ghost-overlay")
export class UtermGhostOverlayElement extends LitElement {
  @property({ type: Boolean }) visible = true;
  @property({ type: Number }) ownCols = 0;
  @property({ type: Number }) ownRows = 0;
  @property({ type: Array }) entries: GhostEntryState[] = [];

  static styles = css`
    :host {
      display: block;
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      pointer-events: none;
      z-index: 14;
      overflow: visible;
    }

    .dm-ghost-box {
      position: absolute;
      top: 0;
      left: 0;
      border: 1.5px solid color-mix(in srgb, var(--dm-user-color, #6b7280) 60%, transparent);
      border-radius: 2px;
      background: color-mix(in srgb, var(--dm-user-color, #6b7280) 4%, transparent);
      pointer-events: none;
      transition: opacity 0.15s ease;
      box-sizing: border-box;
    }

    .dm-ghost-box--hidden {
      opacity: 0;
      pointer-events: none;
    }

    .dm-ghost-box--flash {
      animation: dm-ghost-flash 1.8s ease forwards;
    }

    @keyframes dm-ghost-flash {
      0% {
        opacity: 1;
      }
      60% {
        opacity: 0.7;
      }
      100% {
        opacity: 0;
      }
    }
  `;

  render() {
    if (!this.visible || this.ownCols === 0 || this.ownRows === 0) {
      return html``;
    }

    return html`
      ${this.entries.map((entry) => {
        if (entry.cols === 0 || entry.rows === 0) return html``;

        const pctW = (entry.cols / this.ownCols) * 100;
        const pctH = (entry.rows / this.ownRows) * 100;

        const styles = {
          "--dm-user-color": entry.color,
          width: `${Math.min(pctW, 200)}%`,
          height: `${Math.min(pctH, 200)}%`,
        };

        const classes = [
          "dm-ghost-box",
          entry.hidden ? "dm-ghost-box--hidden" : "",
          entry.flash ? "dm-ghost-box--flash" : "",
        ]
          .filter(Boolean)
          .join(" ");

        return html`<div
          class=${classes}
          data-user-id=${entry.userId}
          data-cols=${entry.cols}
          data-rows=${entry.rows}
          style=${styleMap(styles)}
        ></div>`;
      })}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "uterm-ghost-overlay": UtermGhostOverlayElement;
  }
}
