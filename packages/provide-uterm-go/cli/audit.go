//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"fmt"
	"io"

	"github.com/spf13/cobra"
)

// newAuditCmd mirrors the Python `audit` subcommand and its nested `verify`
// action — a byte-exact port of the tamper-evident WORM hash-chain verifier.
func newAuditCmd() *cobra.Command {
	audit := &cobra.Command{
		Use:   "audit",
		Short: "verify a tamper-evident WORM audit log",
		Long:  "Verify the integrity of a hash-chained append-only audit log.",
	}
	verify := &cobra.Command{
		Use:          "verify PATH",
		Short:        "verify the hash chain of an audit log file",
		Long:         "Walk the audit log and confirm no record was inserted, deleted, reordered, or altered.",
		Args:         cobra.ExactArgs(1),
		SilenceUsage: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			f := cmd.Flags()
			var head *expectedHead
			seqSet := f.Changed("expected-seq")
			hashSet := f.Changed("expected-hash")
			// A head is a (seq, hash) pair; one without the other is a usage error.
			if seqSet != hashSet {
				return fmt.Errorf("--expected-seq and --expected-hash must be given together")
			}
			if seqSet {
				seq, _ := f.GetInt("expected-seq")
				hash, _ := f.GetString("expected-hash")
				head = &expectedHead{seq: int64(seq), hash: hash}
			}
			return runAuditVerify(args[0], head, cmd.OutOrStdout())
		},
	}
	vf := verify.Flags()
	vf.Int("expected-seq", 0, "expected head sequence number (requires --expected-hash)")
	vf.String("expected-hash", "", "expected head record hash (requires --expected-seq)")
	audit.AddCommand(verify)
	return audit
}

// runAuditVerify verifies the chain and reports OK / TAMPERED. It returns
// errTampered (a non-nil error) when the chain is broken so Execute maps it to a
// non-zero exit code, matching the Python `sys.exit(1)` contract.
func runAuditVerify(path string, head *expectedHead, out io.Writer) error {
	result := verifyAuditLog(path, head)
	if result.OK {
		_, _ = fmt.Fprintf(out, "OK: %d records, head seq=%s hash=%s\n",
			result.Count, fmtSeq(result.HeadSeq), fmtHash(result.HeadHash))
		return nil
	}
	_, _ = fmt.Fprintf(out, "TAMPERED: %s at seq=%s\n", result.Reason, fmtSeq(result.FirstBadSeq))
	return errTampered
}

// errTampered signals a verification failure without printing a second "error:"
// line — the TAMPERED report is already written to stdout.
var errTampered = &silentError{}

type silentError struct{}

func (*silentError) Error() string { return "" }

func fmtSeq(v *int64) string {
	if v == nil {
		return "None"
	}
	return fmt.Sprintf("%d", *v)
}

func fmtHash(v *string) string {
	if v == nil {
		return "None"
	}
	return *v
}
