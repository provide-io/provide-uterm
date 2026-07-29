//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The checks that run before a PTY session touches the operating system.
 *
 * Port of `provide.uterm.pty._validate` and `provide.uterm.pty.socket_utils`.
 * Every one of these runs *before* a fork, an execve, a PAM call or a bind,
 * which is the whole reason they exist: once any of those has happened the
 * value is in the kernel's hands and nothing here can take it back.
 *
 * What they are actually stopping:
 *
 * - **A null byte.** C stops reading at the first one, so `/bin/sh\0; rm -rf /`
 *   is one string to this process and a different one to the kernel. Every
 *   value that crosses the boundary is checked for one.
 * - **A path that is not absolute.** A bare name or a relative path would be
 *   resolved against whatever `PATH` happened to be — the caller's
 *   environment, not the operator's intent.
 * - **An `=` in an environment key.** `execve` joins a key and a value with
 *   one, so a key carrying its own would define a second variable nobody
 *   wrote.
 */

import { pyRepr } from "../pycompat/index.ts";

/** How long each of these may be, in characters. */
export const MAX_PATH_LEN = 4096;
export const MAX_USERNAME_LEN = 255;
export const MAX_SERVICE_LEN = 255;
export const MAX_ENV_KEYS = 1000;
export const MAX_ENV_VALUE_LEN = 65536;

/** Written by name: a literal one in source is invisible and easy to lose. */
const NULL_BYTE = "\u0000";

/** POSIX portable filename characters, which is what a username may hold. */
const USERNAME_PATTERN = /^[A-Za-z0-9._-]+$/;

/** What a PAM service name may hold. */
const SERVICE_PATTERN = /^[A-Za-z0-9_-]+$/;

/**
 * Check a command path for use with `execve`.
 *
 * @throws {Error} On an empty path, a null byte, an over-long path, or one
 *   that is not absolute.
 */
export function validateCommand(command: string): void {
  if (command === "") {
    throw new Error("command must not be empty");
  }
  if (command.includes(NULL_BYTE)) {
    throw new Error("command contains null byte");
  }
  if (command.length > MAX_PATH_LEN) {
    throw new Error(`command path too long (max ${MAX_PATH_LEN} chars)`);
  }
  if (!command.startsWith("/")) {
    throw new Error(
      `command must be an absolute path (got ${pyRepr(command)}); relative paths and shell lookups are not allowed`,
    );
  }
}

/**
 * Check an operating-system username.
 *
 * @throws {Error} On an empty name, a null byte, an over-long name, or a
 *   character outside the portable set.
 */
export function validateUsername(username: string): void {
  if (username === "") {
    throw new Error("username must not be empty");
  }
  if (username.includes(NULL_BYTE)) {
    throw new Error("username contains null byte");
  }
  if (username.length > MAX_USERNAME_LEN) {
    throw new Error(`username too long (max ${MAX_USERNAME_LEN} chars)`);
  }
  if (!USERNAME_PATTERN.test(username)) {
    throw new Error(
      `username ${pyRepr(username)} contains invalid character; only A-Z, a-z, 0-9, '.', '_', '-' are allowed`,
    );
  }
}

/**
 * Check a PAM service name.
 *
 * Tighter than a username: no dot, because a service name selects a file in
 * the PAM configuration directory.
 *
 * @throws {Error} On an empty name, a null byte, an over-long name, or a
 *   character outside the permitted set.
 */
export function validateServiceName(service: string): void {
  if (service === "") {
    throw new Error("PAM service name must not be empty");
  }
  if (service.includes(NULL_BYTE)) {
    throw new Error("PAM service name contains null byte");
  }
  if (service.length > MAX_SERVICE_LEN) {
    throw new Error(`PAM service name too long (max ${MAX_SERVICE_LEN} chars)`);
  }
  if (!SERVICE_PATTERN.test(service)) {
    throw new Error(
      `PAM service name ${pyRepr(service)} contains invalid character; only A-Z, a-z, 0-9, '_', '-' are allowed`,
    );
  }
}

/**
 * Check an environment for use with `execve`.
 *
 * @throws {Error} On too many keys, an `=` in a key, a null byte in either
 *   half, or an over-long value.
 */
export function validateEnv(env: Readonly<Record<string, string>>): void {
  const entries = Object.entries(env);
  if (entries.length > MAX_ENV_KEYS) {
    throw new Error(`env dict has too many keys (max ${MAX_ENV_KEYS})`);
  }
  for (const [key, value] of entries) {
    if (key.includes("=")) {
      throw new Error(`invalid key ${pyRepr(key)}: env keys must not contain '='`);
    }
    if (key.includes(NULL_BYTE)) {
      throw new Error(`env key ${pyRepr(key)} contains null byte`);
    }
    if (value.includes(NULL_BYTE)) {
      throw new Error(`env value for ${pyRepr(key)} contains null byte`);
    }
    if (value.length > MAX_ENV_VALUE_LEN) {
      throw new Error(`env value for ${pyRepr(key)} too long (max ${MAX_ENV_VALUE_LEN} chars)`);
    }
  }
}

/**
 * Check a Unix socket path.
 *
 * The null-byte rule has a second consequence here: Linux's abstract socket
 * namespace is addressed with a leading null, so this API cannot reach it at
 * all. That is the reference's behaviour and it is the safer default — an
 * abstract socket has no filesystem permissions on it.
 *
 * @throws {Error} On a null byte or a path that is not absolute.
 */
export function validateSocketPath(path: string): void {
  if (path.includes(NULL_BYTE)) {
    throw new Error("socket path contains null byte");
  }
  if (!path.startsWith("/")) {
    throw new Error("socket path must be an absolute path");
  }
}
