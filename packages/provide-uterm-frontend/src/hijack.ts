// Split shim to keep file size below 500 LOC.
export * from './hijack_impl.ts';
import { startReconnectAnim, stopReconnectAnim } from './hijack-websocket.js';
if (typeof window !== "undefined") {
  (window as any).__testHooks_startReconnectAnim = startReconnectAnim;
  (window as any).__testHooks_stopReconnectAnim = stopReconnectAnim;
}
