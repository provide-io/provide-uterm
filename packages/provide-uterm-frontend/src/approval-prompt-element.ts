import { LitElement, html, css, type PropertyValues } from "lit";
import { property, state } from "lit/decorators.js";
import { computeRemainingSeconds } from "./hijack-approval.js";

export interface PendingApproval {
  id: string;
  command: string;
  expiresAt: number;
}

export class ApprovalPromptElement extends LitElement {
  @property({ type: Object }) pendingApproval: PendingApproval | null = null;
  @property({ type: String }) mode: "modal" | "statusbar" = "statusbar";
  @property({ type: Boolean }) isAdmin = false;
  @property({ type: Number }) uid = 0;

  @state() private _remainingSeconds = 0;
  private _timer: ReturnType<typeof setInterval> | null = null;

  static override styles = css`
    :host {
      display: block;
    }
    :host([hidden]) {
      display: none;
    }
    
    .hijack-approval-modal {
      position: absolute;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(2px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      padding: 20px;
    }

    .hijack-approval-card {
      background: #1a1f26;
      border: 1px solid #ffab40;
      border-radius: 8px;
      padding: 24px;
      max-width: 450px;
      width: 100%;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6), 0 0 15px rgba(255, 171, 64, 0.15);
      animation: hijack-fade-in 0.2s ease-out;
    }

    @keyframes hijack-fade-in {
      from { opacity: 0; transform: translateY(10px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .hijack-approval-title {
      color: #ffab40;
      font-weight: bold;
      font-size: 15px;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .hijack-approval-body {
      font-size: 13px;
      color: #ccd6e8;
      line-height: 1.5;
    }

    .hijack-approval-command {
      background: #0d1117;
      border: 1px solid #2d333b;
      border-radius: 4px;
      padding: 10px;
      margin: 15px 0;
      font-family: "Fira Code", monospace;
      font-size: 12px;
      color: #fff;
      word-break: break-all;
      white-space: pre-wrap;
    }

    .hijack-approval-timer {
      font-size: 12px;
      color: #8b949e;
      margin-top: 10px;
    }

    .hijack-approval-timer span {
      color: #ffab40;
      font-weight: bold;
    }

    .hijack-approval-actions {
      display: flex;
      gap: 12px;
      margin-top: 20px;
    }

    .hijack-btn {
      flex: 1;
      padding: 8px;
      border-radius: 4px;
      font-weight: bold;
      cursor: pointer;
      border: 1px solid transparent;
      transition: all 0.1s;
    }

    .hijack-btn-approve {
      background: #238636;
      color: #fff;
    }
    .hijack-btn-approve:hover:not(:disabled) { background: #2ea043; }
    .hijack-btn-reject {
      background: #da3633;
      color: #fff;
    }
    .hijack-btn-reject:hover:not(:disabled) { background: #f85149; }
    .hijack-btn:disabled { opacity: 0.5; cursor: not-allowed; }

    .hijack-approval-statusbar {
      position: absolute;
      bottom: 0; left: 0; right: 0;
      background: #ffab40;
      color: #000;
      padding: 6px 15px;
      font-size: 12px;
      font-weight: bold;
      z-index: 900;
      display: flex;
      align-items: center;
      box-shadow: 0 -2px 10px rgba(0,0,0,0.3);
      animation: hijack-slide-up 0.2s ease-out;
    }

    @keyframes hijack-slide-up {
      from { transform: translateY(100%); }
      to { transform: translateY(0); }
    }

    .hijack-approval-status {
      display: flex;
      align-items: center;
      gap: 10px;
      width: 100%;
    }

    .hijack-approval-spinner {
      animation: hijack-spin 2s linear infinite;
      display: inline-block;
    }

    @keyframes hijack-spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
  `;

  override connectedCallback() {
    super.connectedCallback();
    this._startApprovalTimer();
  }

  override disconnectedCallback() {
    super.disconnectedCallback();
    this._stopApprovalTimer();
  }

  protected override updated(changedProperties: PropertyValues): void {
    if (changedProperties.has("pendingApproval")) {
      if (this.pendingApproval) {
        this._startApprovalTimer();
      } else {
        this._stopApprovalTimer();
      }
    }
  }

  private _startApprovalTimer() {
    this._stopApprovalTimer();
    if (!this.pendingApproval) return;

    const update = () => {
      if (!this.pendingApproval) return;
      const remaining = computeRemainingSeconds(this.pendingApproval.expiresAt);
      this._remainingSeconds = remaining;
      if (remaining <= 0) {
        this._stopApprovalTimer();
        this.dispatchEvent(new CustomEvent("approval-expired", { bubbles: true, composed: true }));
      }
    };
    update();
    this._timer = setInterval(update, 1000);
  }

  private _stopApprovalTimer() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  private _handleAction(action: "approve" | "reject") {
    this.dispatchEvent(
      new CustomEvent("approval-action", {
        detail: action,
        bubbles: true,
        composed: true,
      })
    );
  }

  override render() {
    if (!this.pendingApproval) return html``;

    if (this.mode === "modal") {
      return html`
        <div class="hijack-approval-modal">
          <div class="hijack-approval-card">
            <div class="hijack-approval-title">⚠️ APPROVAL REQUIRED</div>
            <div class="hijack-approval-body">
              Your command is being held for administrative review.
              <div class="hijack-approval-command">${this.pendingApproval.command}</div>
              <div class="hijack-approval-timer">
                Expires in <span id="h-${this.uid}-approval-timer">${this._remainingSeconds}</span>s...
              </div>
            </div>
            ${this.isAdmin
              ? html`
                  <div class="hijack-approval-actions">
                    <button
                      class="hijack-btn hijack-btn-approve"
                      id="h-${this.uid}-approve"
                      @click=${() => this._handleAction("approve")}
                    >
                      Approve
                    </button>
                    <button
                      class="hijack-btn hijack-btn-reject"
                      id="h-${this.uid}-reject"
                      @click=${() => this._handleAction("reject")}
                    >
                      Reject
                    </button>
                  </div>
                `
              : ""}
          </div>
        </div>
      `;
    }

    return html`
      <div class="hijack-approval-statusbar">
        <div class="hijack-approval-status">
          <span class="hijack-approval-spinner">⏳</span>
          PAUSED: Command pending approval (<span id="h-${this.uid}-approval-timer">${this._remainingSeconds}</span>s)
        </div>
      </div>
    `;
  }
}

export function registerApprovalPromptElement(registry: CustomElementRegistry = customElements): void {
  if (!registry.get("uterm-approval-prompt")) {
    registry.define("uterm-approval-prompt", ApprovalPromptElement);
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "uterm-approval-prompt": ApprovalPromptElement;
  }
}
