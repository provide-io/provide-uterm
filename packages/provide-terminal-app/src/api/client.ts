//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

export type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";

export async function apiJson<T>(path: string, method: HttpMethod = "GET", body: unknown = null): Promise<T> {
  const init: RequestInit = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (body !== null) {
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  if (!response.ok) {
    throw new Error(String(response.status));
  }
  return (await response.json()) as T;
}
