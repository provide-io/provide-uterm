//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//
import type { ComponentType } from "react";
import { BUILTIN_APP_PAGE_KINDS, type AppBootstrap } from "../api/types";

export interface UtermAuthContext {
  subject: string;
  roles: readonly string[];
  attributes?: Readonly<Record<string, string>>;
}

export interface UtermAuthAdapter {
  resolve(): Promise<UtermAuthContext | null>;
  authorize(context: UtermAuthContext, capability: string, resource?: string): boolean;
}

export interface UtermNavigationItem {
  id: string;
  label: string;
  href: string;
  pageKind?: string;
  requiredCapability?: string;
}

export interface UtermConsumerPageProps {
  bootstrap: AppBootstrap;
}

export interface UtermPageRegistration {
  kind: string;
  component: ComponentType<UtermConsumerPageProps>;
}

export interface UtermConsumerExtension {
  id: string;
  themeTokens?: Readonly<Record<string, string>>;
  navigation?: readonly UtermNavigationItem[];
  pages?: readonly UtermPageRegistration[];
  auth?: UtermAuthAdapter;
}

export interface UtermExtensionSnapshot {
  themeTokens: Readonly<Record<string, string>>;
  navigation: readonly UtermNavigationItem[];
  auth: UtermAuthAdapter | null;
}

const IDENTIFIER = /^[a-z][a-z0-9-]{0,63}$/;
const THEME_TOKEN = /^--[a-z][a-z0-9-]{1,63}$/;
const BUILTIN_PAGE_KINDS = new Set<string>(BUILTIN_APP_PAGE_KINDS);

function requireIdentifier(value: string, label: string): void {
  if (!IDENTIFIER.test(value)) throw new Error(`${label} must be a lowercase identifier`);
}

export class UtermExtensionRegistry {
  readonly #extensionIds = new Set<string>();
  readonly #navigation = new Map<string, UtermNavigationItem>();
  readonly #pages = new Map<string, UtermPageRegistration>();
  readonly #themeTokens = new Map<string, string>();
  #auth: UtermAuthAdapter | null = null;

  register(extension: UtermConsumerExtension): void {
    requireIdentifier(extension.id, "extension id");
    if (this.#extensionIds.has(extension.id)) {
      throw new Error(`extension id is already registered: ${extension.id}`);
    }
    const navigationIds = new Set<string>();
    for (const item of extension.navigation ?? []) {
      requireIdentifier(item.id, "navigation id");
      if (this.#navigation.has(item.id) || navigationIds.has(item.id)) {
        throw new Error(`navigation id is already registered: ${item.id}`);
      }
      navigationIds.add(item.id);
    }
    const pageKinds = new Set<string>();
    for (const page of extension.pages ?? []) {
      requireIdentifier(page.kind, "page kind");
      if (BUILTIN_PAGE_KINDS.has(page.kind)) {
        throw new Error(`reserved page kind cannot be registered: ${page.kind}`);
      }
      if (this.#pages.has(page.kind) || pageKinds.has(page.kind)) {
        throw new Error(`page kind is already registered: ${page.kind}`);
      }
      pageKinds.add(page.kind);
    }
    for (const [token, value] of Object.entries(extension.themeTokens ?? {})) {
      if (!THEME_TOKEN.test(token) || value.trim().length === 0) {
        throw new Error(`invalid theme token: ${token}`);
      }
    }
    if (extension.auth && this.#auth) throw new Error("an authentication adapter is already registered");

    this.#extensionIds.add(extension.id);
    for (const item of extension.navigation ?? []) this.#navigation.set(item.id, { ...item });
    for (const page of extension.pages ?? []) this.#pages.set(page.kind, page);
    for (const [token, value] of Object.entries(extension.themeTokens ?? {})) {
      this.#themeTokens.set(token, value);
    }
    if (extension.auth) this.#auth = extension.auth;
  }

  resolvePage(kind: string): UtermPageRegistration | null {
    return this.#pages.get(kind) ?? null;
  }

  snapshot(): UtermExtensionSnapshot {
    return {
      themeTokens: Object.freeze(Object.fromEntries(this.#themeTokens)),
      navigation: Object.freeze([...this.#navigation.values()].map((item) => Object.freeze({ ...item }))),
      auth: this.#auth,
    };
  }
}

export function createUtermExtensionRegistry(): UtermExtensionRegistry {
  return new UtermExtensionRegistry();
}

export function applyThemeTokens(
  target: HTMLElement,
  tokens: Readonly<Record<string, string>>,
): void {
  for (const [token, value] of Object.entries(tokens)) {
    if (!THEME_TOKEN.test(token) || value.trim().length === 0) {
      throw new Error(`invalid theme token: ${token}`);
    }
  }
  for (const [token, value] of Object.entries(tokens)) {
    target.style.setProperty(token, value);
  }
}
