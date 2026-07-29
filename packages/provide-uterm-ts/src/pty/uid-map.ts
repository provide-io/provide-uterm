//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * Which operating-system user a session runs as.
 *
 * Port of `provide.uterm.pty.uid_map`. This decides the identity a shell is
 * started under, so the rule that matters is the one it refuses: nothing
 * resolves to uid 0 or gid 0 unless an operator has explicitly allowed root.
 * That is the difference between a session running as a person and one running
 * as the machine, and it is checked on every path in — an explicit uid, a
 * run-as spec, a table entry, and the user's own passwd record.
 *
 * The passwd database is the caller's: a lookup is injected rather than
 * imported, so this module runs anywhere and a test can describe a machine
 * rather than depend on one.
 */

import { pyInt, pyRepr } from "../pycompat/index.ts";
import { validateUsername } from "./validate.ts";

/** What a session needs to know about the user it runs as. */
export interface ResolvedUser {
  uid: number;
  gid: number;
  home: string;
  shell: string;
  name: string;
}

/** One row of the passwd database. */
export interface PasswdEntry {
  name: string;
  uid: number;
  gid: number;
  home: string;
  shell: string;
}

/** How this module reads the passwd database. */
export interface PasswdLookup {
  /** By name, or nothing when there is no such user. */
  byName(name: string): PasswdEntry | undefined;
  /** By number, or nothing when no user has it. */
  byUid(uid: number): PasswdEntry | undefined;
}

/** A resolution that cannot be allowed, or cannot be made at all. */
export class UidMapError extends Error {}

/** What a caller may say about how a session resolves. */
export interface UidMapOptions {
  /** Application username → run-as spec. `*` matches anybody. */
  table?: Readonly<Record<string, string>>;
  /**
   * Whether root may be resolved to.
   *
   * Off by default, and the default is the point: a deployment that means to
   * run sessions as root has to say so.
   */
  allowRoot?: boolean;
  passwd: PasswdLookup;
}

/** What one resolution may override. */
export interface ResolveOptions {
  /** An OS username, a numeric uid, or `uid:gid`. */
  runAs?: string;
  /** An explicit uid, which beats everything else. */
  runAsUid?: number;
  /** An explicit gid, used where the chosen path does not name one. */
  runAsGid?: number;
}

/** How a uid nobody has is described, having nothing to describe it with. */
const UNKNOWN_UID_HOME = "/";
const UNKNOWN_UID_SHELL = "/bin/sh";

/**
 * Resolve an application username to an operating-system identity.
 *
 * In order: an explicit uid, a run-as spec, a table entry for this user or the
 * wildcard, and finally the user's own passwd record — so a session runs as
 * itself unless something says otherwise.
 */
export class UidMap {
  readonly #table: Readonly<Record<string, string>>;
  readonly #allowRoot: boolean;
  readonly #passwd: PasswdLookup;

  constructor(options: UidMapOptions) {
    this.#table = options.table ?? {};
    this.#allowRoot = options.allowRoot ?? false;
    this.#passwd = options.passwd;
  }

  /**
   * The identity this session runs as.
   *
   * @throws {UidMapError} When there is no such user, or when the answer would
   *   be privileged and root has not been allowed.
   * @throws {Error} When the username is not one an operating system would
   *   accept.
   */
  resolve(username: string, options: ResolveOptions = {}): ResolvedUser {
    // Checked before anything is looked up: a name with a null byte or a colon
    // in it must not reach a passwd lookup at all.
    if (username !== "") {
      validateUsername(username);
    }

    // An explicit uid beats everything else, including a run-as naming a
    // different user. The privilege check lives in `#fromUid`, which every
    // path in goes through.
    if (options.runAsUid !== undefined) {
      return this.#fromUid(options.runAsUid, options.runAsGid);
    }

    if (options.runAs !== undefined) {
      return this.#fromSpec(options.runAs, options.runAsGid);
    }

    const spec = this.#table[username] || this.#table["*"];
    if (spec !== undefined) {
      return this.#fromSpec(spec, options.runAsGid);
    }

    const entry = this.#passwd.byName(username);
    if (entry === undefined) {
      throw new UidMapError(`no such OS user: ${pyRepr(username)}`);
    }
    const gid = options.runAsGid ?? entry.gid;
    this.#checkPrivilege(entry.uid, gid);
    return { uid: entry.uid, gid, home: entry.home, shell: entry.shell, name: entry.name };
  }

  /**
   * Refuse a privileged answer.
   *
   * Either half counts: a session running as an unprivileged user in group
   * zero is still in group zero.
   */
  #checkPrivilege(uid: number, gid: number | undefined): void {
    if (this.#allowRoot) {
      return;
    }
    if (uid === 0 || gid === 0) {
      throw new UidMapError(`resolving to privileged ${uid}:${gid === undefined ? "None" : gid} is not allowed`);
    }
  }

  /**
   * The identity behind a number.
   *
   * A uid nobody has still resolves — to itself, at `/`, with a plain shell.
   * The privilege check has already run, so this cannot be root.
   */
  #fromUid(uid: number, gid: number | undefined): ResolvedUser {
    this.#checkPrivilege(uid, gid);
    const entry = this.#passwd.byUid(uid);
    if (entry === undefined) {
      return {
        uid,
        gid: gid ?? uid,
        home: UNKNOWN_UID_HOME,
        shell: UNKNOWN_UID_SHELL,
        name: String(uid),
      };
    }
    return { uid: entry.uid, gid: gid ?? entry.gid, home: entry.home, shell: entry.shell, name: entry.name };
  }

  /** A run-as spec: an OS username, a `uid`, or a `uid:gid`. */
  #fromSpec(spec: string, runAsGid: number | undefined): ResolvedUser {
    if (spec.includes(":")) {
      const separator = spec.indexOf(":");
      const uid = pyInt(spec.slice(0, separator));
      const gid = pyInt(spec.slice(separator + 1));
      if (uid === undefined || gid === undefined) {
        throw new UidMapError(`invalid literal for int() with base 10: ${pyRepr(spec.slice(separator + 1))}`);
      }
      return this.#fromUid(uid, gid);
    }

    const uid = pyInt(spec);
    if (uid !== undefined) {
      return this.#fromUid(uid, runAsGid);
    }

    const entry = this.#passwd.byName(spec);
    if (entry === undefined) {
      throw new UidMapError(`no such OS user: ${pyRepr(spec)}`);
    }
    const gid = runAsGid ?? entry.gid;
    this.#checkPrivilege(entry.uid, gid);
    return { uid: entry.uid, gid, home: entry.home, shell: entry.shell, name: entry.name };
  }
}
