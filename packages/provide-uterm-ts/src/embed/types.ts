//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

/**
 * The embed layer's types: which clients a broadcast reaches, and what an
 * embedded session answers a telnet server with.
 *
 * Port of `provide.uterm.embed.types`.
 */

/** Where a session is in its life. */
export const SESSION_LIFECYCLE = [
  "created",
  "connecting",
  "negotiated",
  "connected",
  "upstream_lost",
  "reconnecting",
  "client_attached",
  "shutdown",
] as const;

/** One of {@link SESSION_LIFECYCLE}. */
export type SessionLifecycle = (typeof SESSION_LIFECYCLE)[number];

/** What an interceptor decided to do with some bytes. */
export const INTERCEPT_ACTIONS = ["pass", "replace", "consume", "defer", "inject"] as const;

/** One of {@link INTERCEPT_ACTIONS}. */
export type InterceptAction = (typeof INTERCEPT_ACTIONS)[number];

/** What to drop when a client cannot keep up. */
export const BACKPRESSURE_POLICIES = ["drop_oldest", "drop_newest", "disconnect"] as const;

/** One of {@link BACKPRESSURE_POLICIES}. */
export type BackpressurePolicy = (typeof BACKPRESSURE_POLICIES)[number];

/** Which way bytes are travelling. */
export const BYTE_DIRECTIONS = ["upstream_to_app", "client_to_upstream"] as const;

/** One of {@link BYTE_DIRECTIONS}. */
export type ByteDirection = (typeof BYTE_DIRECTIONS)[number];

/** What a wire event is about. */
export const WIRE_EVENT_KINDS = ["iac", "negotiation", "diagnostic"] as const;

/** One of {@link WIRE_EVENT_KINDS}. */
export type WireEventKind = (typeof WIRE_EVENT_KINDS)[number];

/** An interceptor's verdict, and the bytes it goes with. */
export interface InterceptResult {
  action: InterceptAction;
  payload: Uint8Array | undefined;
}

/** Let the bytes through unchanged. */
export function interceptPass(): InterceptResult {
  return { action: "pass", payload: undefined };
}

/** Send these bytes instead. */
export function interceptReplace(payload: Uint8Array): InterceptResult {
  return { action: "replace", payload };
}

/** Send nothing; the bytes are handled. */
export function interceptConsume(): InterceptResult {
  return { action: "consume", payload: undefined };
}

/** Hold the bytes for now. */
export function interceptDefer(): InterceptResult {
  return { action: "defer", payload: undefined };
}

/** Send these bytes as well. */
export function interceptInject(payload: Uint8Array): InterceptResult {
  return { action: "inject", payload };
}

/** How many frames a client may fall behind before its policy applies. */
export const DEFAULT_QUEUE_CAPACITY = 64;

/** What is known about an attached client. */
export interface ClientMetadata {
  clientId: string;
  tags: ReadonlySet<string>;
  attributes: Readonly<Record<string, string>>;
  backpressure: BackpressurePolicy;
  queueCapacity: number;
}

/** A client with the defaults filled in. */
export function clientMetadata(clientId: string, overrides: Partial<ClientMetadata> = {}): ClientMetadata {
  return {
    clientId,
    tags: new Set(),
    attributes: {},
    backpressure: "drop_oldest",
    queueCapacity: DEFAULT_QUEUE_CAPACITY,
    ...overrides,
  };
}

/** Which clients a broadcast is for. */
export interface ClientFilter {
  requireAnyTag?: readonly string[] | undefined;
  excludeTags?: readonly string[] | undefined;
  predicate?: ((meta: ClientMetadata) => boolean) | undefined;
}

/**
 * Whether a client is one this filter is for.
 *
 * An exclusion beats a requirement: a client carrying both is excluded,
 * because the exclusion is the narrower statement — an operator adding one
 * means "not these", and a requirement that overrode it would make the
 * exclusion silently useless.
 *
 * An absent or empty list is no constraint rather than an impossible one, so
 * a filter naming no required tags matches every client. The other reading
 * would make attaching a filter to say one thing stop every broadcast.
 */
export function filterMatches(filter: ClientFilter, meta: ClientMetadata): boolean {
  if (filter.excludeTags !== undefined && filter.excludeTags.some((tag) => meta.tags.has(tag))) {
    return false;
  }
  if (
    filter.requireAnyTag !== undefined &&
    filter.requireAnyTag.length > 0 &&
    !filter.requireAnyTag.some((tag) => meta.tags.has(tag))
  ) {
    return false;
  }
  return filter.predicate === undefined || filter.predicate(meta);
}

/** The telnet bytes this policy speaks. */
const IAC = 255;
const SB = 250;
const SE = 240;
const WILL = 251;
const WONT = 252;
const DO = 253;
const DONT = 254;

/** The two options answered: terminal type and window size. */
const OPTION_TERMINAL_TYPE = 24;
const OPTION_WINDOW_SIZE = 31;

/** The subnegotiation byte that asks for a terminal type. */
const TERMINAL_TYPE_SEND = 1;

/** What the reference substitutes for a character ASCII cannot carry. */
const ASCII_REPLACEMENT = 0x3f;

/** How a session presents itself to a telnet server. */
export interface TelnetPolicy {
  terminalType: string;
  /** Columns and rows, in that order. */
  windowSize: readonly [number, number];
}

/** The policy a session uses unless told otherwise. */
export const DEFAULT_TELNET_POLICY: TelnetPolicy = { terminalType: "ANSI", windowSize: [80, 25] };

/** Encode as ASCII, substituting for anything it cannot carry. */
function asciiBytes(text: string): number[] {
  // A question mark, which is what `errors="replace"` writes on the way *out*
  // — not the replacement character, which is what decoding substitutes.
  return [...text].map((character) => {
    const code = character.codePointAt(0) as number;
    return code < 0x80 ? code : ASCII_REPLACEMENT;
  });
}

/**
 * Answer a telnet option negotiation.
 *
 * Symmetrically: a DO is met with a WILL and a WILL with a DO, because the
 * policy accepts whatever is offered; a WONT or DONT is mirrored back so
 * neither end is left waiting on the other. A command that is none of those
 * gets nothing.
 */
export function onTelnetOption(command: number, option: number): Uint8Array {
  const reply =
    command === DO ? WILL : command === WILL ? DO : command === WONT ? DONT : command === DONT ? WONT : undefined;
  return reply === undefined ? new Uint8Array(0) : Uint8Array.from([IAC, reply, option]);
}

/**
 * Answer a telnet subnegotiation.
 *
 * Only two are answered — a terminal-type request and a window size — and
 * anything else gets nothing rather than a guess, because a wrong answer to an
 * option nobody implemented is worse than silence.
 */
export function onTelnetSubnegotiation(policy: TelnetPolicy, option: number, body: Uint8Array): Uint8Array {
  if (option === OPTION_TERMINAL_TYPE) {
    // Only a *request* is answered; a server sending its own terminal type is
    // telling, not asking.
    if (body.length === 0 || body[0] !== TERMINAL_TYPE_SEND) {
      return new Uint8Array(0);
    }
    return Uint8Array.from([IAC, SB, OPTION_TERMINAL_TYPE, 0, ...asciiBytes(policy.terminalType), IAC, SE]);
  }
  if (option === OPTION_WINDOW_SIZE) {
    const [cols, rows] = policy.windowSize;
    // Two bytes each, most significant first: a terminal wider than 255
    // columns is ordinary, and sending one byte would wrap it.
    return Uint8Array.from([
      IAC,
      SB,
      OPTION_WINDOW_SIZE,
      (cols >> 8) & 0xff,
      cols & 0xff,
      (rows >> 8) & 0xff,
      rows & 0xff,
      IAC,
      SE,
    ]);
  }
  return new Uint8Array(0);
}
