//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import { useState } from "react";
import { quickConnect } from "../../api/sessions";
import type { AppBootstrap } from "../../api/types";
import { saveRecent } from "./ConnectPage";
import styles from "./ConnectPage.module.css";

interface ConnectFormProps {
  bootstrap: AppBootstrap;
}

export function ConnectForm({ bootstrap: _bootstrap }: ConnectFormProps) {
  const [connType, setConnType] = useState("telnet");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("23");
  const [portEdited, setPortEdited] = useState(false);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [recording, setRecording] = useState(true);
  const [autoReconnect, setAutoReconnect] = useState(false);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const needsHost = connType === "ssh" || connType === "telnet";

  function handleTypeChange(value: string) {
    setConnType(value);
    if (!portEdited) {
      setPort(value === "telnet" ? "23" : "22");
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    if (needsHost && !host.trim()) {
      setError(`Host is required for ${connType.toUpperCase()} connections.`);
      return;
    }
    setSubmitting(true);
    try {
      const payload: Record<string, unknown> = { connector_type: connType };
      if (needsHost) {
        payload.host = host.trim();
        payload.port = parseInt(port, 10) || (connType === "telnet" ? 23 : 22);
      }
      if (connType === "ssh") {
        if (username.trim()) payload.username = username.trim();
        if (password) payload.password = password;
      }
      if (recording) payload.recording_enabled = true;
      if (autoReconnect) payload.auto_start = true;
      // biome-ignore lint/suspicious/noExplicitAny: quickConnect payload
      const result = await quickConnect(payload as any);
      if (needsHost) {
        saveRecent({
          host: host.trim(), transport: connType,
          port: parseInt(port, 10) || 23, lastUsed: new Date().toISOString(),
        });
      }
      window.location.href = result.url;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Connection failed.");
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={(e) => void handleSubmit(e)}>
      <div className={styles.formGrid}>
        <label>
          <span className="form-label">Host</span>
          <input type="text" value={needsHost ? host : "(local)"}
            onChange={needsHost ? (e) => setHost(e.target.value) : undefined}
            placeholder="bbs.example.com" disabled={!needsHost} readOnly={!needsHost} />
        </label>
        <label>
          <span className="form-label">Transport</span>
          <select value={connType} onChange={(e) => handleTypeChange(e.target.value)}>
            <option value="telnet">telnet</option>
            <option value="ssh">ssh</option>
            <option value="shell">shell</option>
          </select>
        </label>
        <label>
          <span className="form-label">Port</span>
          <input type="text" value={port}
            onChange={(e) => { setPort(e.target.value); setPortEdited(true); }}
            placeholder="23" disabled={!needsHost} />
        </label>
      </div>

      {connType === "ssh" && (
        <div className="ssh-grid">
          <label>
            <span className="form-label">Username</span>
            <input type="text" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="username" />
          </label>
          <label>
            <span className="form-label">Password</span>
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="password" />
          </label>
        </div>
      )}

      <div className={styles.formRow}>
        <label className="checkbox-label">
          <input type="checkbox" checked={recording} onChange={(e) => setRecording(e.target.checked)} className="checkbox-input" />
          Record session
        </label>
        <label className="checkbox-label ml-16">
          <input type="checkbox" checked={autoReconnect} onChange={(e) => setAutoReconnect(e.target.checked)} className="checkbox-input" />
          Auto-reconnect
        </label>
        <div className="flex-spacer" />
        <button type="submit" disabled={submitting} className="btn-primary btn-connect">
          {submitting ? "Connecting…" : "Connect"}
        </button>
      </div>

      {error && <div className="error-text mt-8">{error}</div>}
    </form>
  );
}
