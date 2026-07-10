//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/lipgloss"
)

// humanSize formats a byte count like the Textual app's human_size.
func humanSize(n int) string {
	switch {
	case n < 1024:
		return fmt.Sprintf("%dB", n)
	case n < 1024*1024:
		return fmt.Sprintf("%.1fKB", float64(n)/1024)
	default:
		return fmt.Sprintf("%.1fMB", float64(n)/(1024*1024))
	}
}

// statusLabel returns the short status column text for an exchange.
func statusLabel(ex *exchange) string {
	if ex.status == nil {
		return "..."
	}
	return fmt.Sprintf("%d", *ex.status)
}

func durationLabel(ex *exchange) string {
	if ex.durationMs == nil {
		return "-"
	}
	return fmt.Sprintf("%.0fms", *ex.durationMs)
}

func sizeLabel(ex *exchange) string {
	if ex.status == nil {
		return "-"
	}
	return humanSize(ex.resBodySize)
}

// View implements tea.Model.
func (m watchModel) View() string {
	if m.quitting {
		return ""
	}
	if m.layoutMode == "modal" && m.showDetail {
		if ex := m.selected(); ex != nil {
			return strings.Join(detailLines(ex), "\n") + "\n"
		}
	}
	var b strings.Builder
	b.WriteString(titleStyle.Render("uterm watch — "+m.tunnelID) + "\n")
	b.WriteString(m.renderTable())
	if m.layoutMode != "modal" {
		b.WriteString("\n" + m.renderDetailPane())
	}
	b.WriteString("\n" + m.renderStatusBar())
	return b.String()
}

var (
	titleStyle  = lipgloss.NewStyle().Bold(true)
	headerStyle = lipgloss.NewStyle().Bold(true)
	statusStyle = lipgloss.NewStyle()
)

// renderTable renders the request table with the cursor marker.
func (m watchModel) renderTable() string {
	rows := m.filtered()
	var b strings.Builder
	b.WriteString(headerStyle.Render(fmt.Sprintf("  %-7s %-40s %-6s %-8s %-8s", "Method", "URL", "Status", "Dur", "Size")) + "\n")
	if len(rows) == 0 {
		b.WriteString("  (no requests)\n")
		return b.String()
	}
	for i, ex := range rows {
		marker := "  "
		if i == m.cursor {
			marker = "> "
		}
		fmt.Fprintf(&b, "%s%-7s %-40s %-6s %-8s %-8s\n",
			marker, ex.method, truncate(ex.url, 40), statusLabel(ex), durationLabel(ex), sizeLabel(ex))
	}
	return b.String()
}

// renderDetailPane renders the detail for the selected row (horizontal/vertical).
func (m watchModel) renderDetailPane() string {
	ex := m.selected()
	if ex == nil {
		return "(select a request)"
	}
	return strings.Join(detailLines(ex), "\n")
}

func (m watchModel) renderStatusBar() string {
	conn := "Disconnected"
	if m.connected {
		conn = "Connected"
	}
	filter := m.methodFilter
	if filter == "" {
		filter = "ALL"
	}
	return statusStyle.Render(fmt.Sprintf(" %s  %s  %d requests  layout=%s  filter=%s  [l]ayout [f]ilter [q]uit",
		m.tunnelID, conn, len(m.exchanges), m.layoutMode, filter))
}

// selected returns the exchange under the cursor within the filtered view.
func (m watchModel) selected() *exchange {
	rows := m.filtered()
	if m.cursor < 0 || m.cursor >= len(rows) {
		return nil
	}
	return rows[m.cursor]
}

// detailLines builds the request/response detail text. Port of _detail_lines
// (Textual rich markup stripped — this renders to a plain terminal).
func detailLines(ex *exchange) []string {
	lines := []string{fmt.Sprintf("%s %s", ex.method, ex.url)}
	if ex.status != nil {
		dur := 0.0
		if ex.durationMs != nil {
			dur = *ex.durationMs
		}
		lines = append(lines, fmt.Sprintf("%d %s — %.0fms", *ex.status, ex.statusText, dur))
	}
	lines = append(lines, "", "Request Headers")
	for _, k := range sortedKeys(ex.reqHeaders) {
		lines = append(lines, fmt.Sprintf("  %s: %s", k, ex.reqHeaders[k]))
	}
	if body := decodeBody(ex.reqBodyB64, ex.reqBodyTruncated, ex.reqBodyBinary, ex.reqBodySize); body != "" {
		lines = append(lines, "", "Request Body", body)
	}
	if ex.status != nil {
		lines = append(lines, "", "Response Headers")
		for _, k := range sortedKeys(ex.resHeaders) {
			lines = append(lines, fmt.Sprintf("  %s: %s", k, ex.resHeaders[k]))
		}
		if body := decodeBody(ex.resBodyB64, ex.resBodyTruncated, ex.resBodyBinary, ex.resBodySize); body != "" {
			lines = append(lines, "", "Response Body", body)
		}
	}
	return lines
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	if n <= 1 {
		return s[:n]
	}
	return s[:n-1] + "…"
}
