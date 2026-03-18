//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

import type { AppBootstrap } from "../../api/types";
import styles from "./AppHeader.module.css";

interface Crumb {
  label: string;
  href?: string;
}

interface AppHeaderProps {
  bootstrap: AppBootstrap;
  crumbs?: Crumb[];
  right?: React.ReactNode;
}

export function AppHeader({ bootstrap, crumbs, right }: AppHeaderProps) {
  const allCrumbs: Crumb[] = [
    { label: "Dashboard", href: `${bootstrap.app_path}/` },
    ...(crumbs ?? []),
  ];

  return (
    <header className={styles.header}>
      <nav className={styles.nav}>
        {allCrumbs.map((crumb, i) => (
          <span key={crumb.label} className={styles.crumbWrap}>
            {i > 0 && <span className={styles.separator}>/</span>}
            {crumb.href && i < allCrumbs.length - 1 ? (
              <a className={styles.link} href={crumb.href}>{crumb.label}</a>
            ) : (
              <span className={i === allCrumbs.length - 1 ? styles.active : styles.link}>{crumb.label}</span>
            )}
          </span>
        ))}
      </nav>
      {right && <div className={styles.right}>{right}</div>}
    </header>
  );
}
