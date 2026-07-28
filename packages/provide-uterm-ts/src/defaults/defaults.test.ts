//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { homedir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import * as defaults from "./index.ts";

describe("defaults", () => {
  it("matches the TerminalDefaults constants byte-for-byte", () => {
    expect({
      TELNET_HOST: defaults.TELNET_HOST,
      TELNET_PORT: defaults.TELNET_PORT,
      SSH_PORT: defaults.SSH_PORT,
      GATEWAY_TELNET_PORT: defaults.GATEWAY_TELNET_PORT,
      GATEWAY_SSH_PORT: defaults.GATEWAY_SSH_PORT,
      BIND_ALL: defaults.BIND_ALL,
      PROXY_PORT: defaults.PROXY_PORT,
      PROXY_WS_PATH: defaults.PROXY_WS_PATH,
      PROXY_POLL_MS: defaults.PROXY_POLL_MS,
      SERVER_HOST: defaults.SERVER_HOST,
      SERVER_PORT: defaults.SERVER_PORT,
      TELNET_REMOTE_PORT: defaults.TELNET_REMOTE_PORT,
      SSH_REMOTE_PORT: defaults.SSH_REMOTE_PORT,
      WS_PING_INTERVAL: defaults.WS_PING_INTERVAL,
      WS_PING_TIMEOUT: defaults.WS_PING_TIMEOUT,
      WS_CLOSE_TIMEOUT: defaults.WS_CLOSE_TIMEOUT,
      RECONNECT_MAX_RETRIES: defaults.RECONNECT_MAX_RETRIES,
      RECONNECT_BASE_BACKOFF_S: defaults.RECONNECT_BASE_BACKOFF_S,
      RECONNECT_MAX_BACKOFF_S: defaults.RECONNECT_MAX_BACKOFF_S,
    }).toStrictEqual({
      TELNET_HOST: "127.0.0.1",
      TELNET_PORT: 2102,
      SSH_PORT: 2222,
      GATEWAY_TELNET_PORT: 2112,
      GATEWAY_SSH_PORT: 2222,
      BIND_ALL: "0.0.0.0",
      PROXY_PORT: 8765,
      PROXY_WS_PATH: "/ws/terminal",
      PROXY_POLL_MS: 50,
      SERVER_HOST: "127.0.0.1",
      SERVER_PORT: 8780,
      TELNET_REMOTE_PORT: 23,
      SSH_REMOTE_PORT: 22,
      WS_PING_INTERVAL: 20,
      WS_PING_TIMEOUT: 20,
      WS_CLOSE_TIMEOUT: 10,
      RECONNECT_MAX_RETRIES: 5,
      RECONNECT_BASE_BACKOFF_S: 0.5,
      RECONNECT_MAX_BACKOFF_S: 30.0,
    });
  });

  it("resolves the resume-token file under the user home directory", () => {
    expect(defaults.tokenFile()).toBe(join(homedir(), ".uterm", "session_token"));
  });
});
