//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerAuth;

namespace Provide.Uterm.Tests.ServerAuth;

/// <summary>
/// One share subject of a session can be revoked on its own: its tokens issued at
/// or before the cutoff are refused, the session's other share viewers are not,
/// and a token minted for it after the cutoff is admitted. Port of
/// tests/server/test_viewer_subject_revocation.py's table half.
/// </summary>
public sealed class ShareViewerSubjectRevocationTests
{
    private const int Cap = AuthorizationService.ShareViewerSubjectRevocationsPerSessionMax;

    private static Principal Share(string subject, object? iat, params string[] roles)
    {
        var p = new Principal
        {
            SubjectId = subject,
            Roles = StringSet.Of(roles.Length == 0 ? new[] { "viewer" } : roles),
        };
        if (iat is not null) p.Claims["iat"] = iat;
        return p;
    }

    [Theory]
    [InlineData(499.0, true)]
    [InlineData(500.0, true)]
    [InlineData(500.5, true)]
    [InlineData(501.0, false)]
    public void A_subject_cutoff_refuses_that_subjects_tokens_up_to_it(double iat, bool revoked)
    {
        var authz = new AuthorizationService();
        Assert.Equal(500.5, authz.RevokeShareViewerSubject("s1", "share:s1:a", 500.5));
        Assert.Equal(500.5, authz.ShareViewerSubjectRevocation("s1", "share:s1:a"));
        Assert.Equal(revoked, authz.ShareViewerRevoked(Share("share:s1:a", iat), "s1"));
    }

    [Fact]
    public void A_subject_cutoff_leaves_the_sessions_other_viewers_and_other_sessions_alone()
    {
        var authz = new AuthorizationService();
        authz.RevokeShareViewerSubject("s1", "share:s1:a", 500);
        Assert.False(authz.ShareViewerRevoked(Share("share:s1:b", 1.0), "s1"));
        Assert.Null(authz.ShareViewerRevocation("s1"));
        Assert.Equal(0, authz.ShareViewerRevocationCount);
        Assert.Null(authz.ShareViewerSubjectRevocation("s2", "share:s1:a"));
        Assert.False(authz.ShareViewerRevoked(Share("share:s1:a", 1.0), "s2"));
    }

    [Fact]
    public void A_subject_cutoff_refuses_a_token_without_iat_and_never_anyone_but_a_share_viewer()
    {
        var authz = new AuthorizationService();
        authz.RevokeShareViewerSubject("s1", "share:s1:a", 500);
        Assert.True(authz.ShareViewerRevoked(Share("share:s1:a", null), "s1"));
        Assert.False(authz.ShareViewerRevoked(Share("share:s1:a", 1.0, "viewer", "operator"), "s1"));
    }

    [Fact]
    public void The_later_of_the_session_and_subject_cutoffs_decides()
    {
        var authz = new AuthorizationService();
        authz.RevokeShareViewers("s1", 300);
        authz.RevokeShareViewerSubject("s1", "share:s1:a", 500);
        Assert.True(authz.ShareViewerRevoked(Share("share:s1:a", 400.0), "s1"));
        Assert.False(authz.ShareViewerRevoked(Share("share:s1:b", 400.0), "s1"));
        authz.RevokeShareViewers("s1", 600);
        Assert.True(authz.ShareViewerRevoked(Share("share:s1:a", 550.0), "s1"));
    }

    [Fact]
    public void A_full_session_refuses_a_new_subject_but_moves_a_held_one_forward()
    {
        var authz = new AuthorizationService();
        for (var n = 0; n < Cap; n++) authz.RevokeShareViewerSubject("s1", $"share:s1:{n}", 100 + n);
        var full = Assert.Throws<ShareViewerSubjectRevocationsFullException>(
            () => authz.RevokeShareViewerSubject("s1", "share:s1:new", 500));
        Assert.Equal("share-viewer subject revocation table is full for this session", full.Message);
        Assert.Null(authz.ShareViewerSubjectRevocation("s1", "share:s1:new"));
        Assert.Equal(100, authz.ShareViewerSubjectRevocation("s1", "share:s1:0"));
        Assert.Equal(600, authz.RevokeShareViewerSubject("s1", "share:s1:0", 600));
        Assert.Equal(600, authz.RevokeShareViewerSubject("s2", "share:s2:a", 600));
    }

    [Fact]
    public void Subject_cutoffs_older_than_the_ttl_age_out_and_free_their_slots()
    {
        var authz = new AuthorizationService();
        authz.RevokeShareViewerSubject("s1", "share:s1:a", 10);
        authz.RevokeShareViewerSubject("s1", "share:s1:b", 20);
        // Exactly the TTL old is kept; older goes.
        authz.RevokeShareViewerSubject("s2", "share:s2:a", 20 + AuthorizationService.ShareViewerRevocationTtlSeconds);
        Assert.Null(authz.ShareViewerSubjectRevocation("s1", "share:s1:a"));
        Assert.Equal(20, authz.ShareViewerSubjectRevocation("s1", "share:s1:b"));

        var full = new AuthorizationService();
        for (var n = 0; n < Cap; n++) full.RevokeShareViewerSubject("s1", $"share:s1:{n}", n);
        var later = 1 + AuthorizationService.ShareViewerRevocationTtlSeconds;
        Assert.Equal(later, full.RevokeShareViewerSubject("s1", "share:s1:new", later));
        Assert.Null(full.ShareViewerSubjectRevocation("s1", "share:s1:0"));
    }

    [Fact]
    public void A_re_revoked_subject_ages_out_from_its_newest_cutoff()
    {
        var authz = new AuthorizationService();
        authz.RevokeShareViewerSubject("s1", "share:s1:a", 10);
        authz.RevokeShareViewerSubject("s1", "share:s1:b", 20);
        authz.RevokeShareViewerSubject("s1", "share:s1:a", 30);
        authz.RevokeShareViewerSubject("s2", "share:s2:a", 25 + AuthorizationService.ShareViewerRevocationTtlSeconds);
        Assert.Null(authz.ShareViewerSubjectRevocation("s1", "share:s1:b"));
        Assert.Equal(30, authz.ShareViewerSubjectRevocation("s1", "share:s1:a"));
    }

    [Fact]
    public void Forgetting_a_session_drops_its_subject_cutoffs_only()
    {
        var authz = new AuthorizationService();
        authz.RevokeShareViewerSubject("s1", "share:s1:a", 10);
        authz.RevokeShareViewerSubject("s1", "share:s1:b", 11);
        authz.RevokeShareViewerSubject("s2", "share:s2:a", 12);
        authz.ForgetShareViewers("s1");
        authz.ForgetShareViewers("never-revoked");
        Assert.Null(authz.ShareViewerSubjectRevocation("s1", "share:s1:a"));
        Assert.Null(authz.ShareViewerSubjectRevocation("s1", "share:s1:b"));
        Assert.Equal(12, authz.ShareViewerSubjectRevocation("s2", "share:s2:a"));
        for (var n = 0; n < Cap; n++) authz.RevokeShareViewerSubject("s1", $"share:s1:{n}", 20);
    }

    [Fact]
    public void A_share_subject_of_the_session_is_told_from_anything_else()
    {
        const string prefix = "share:s1:";
        var longest = prefix + new string('x', AuthorizationService.ShareViewerSubjectMaxBytes - prefix.Length);
        Assert.True(AuthorizationService.IsShareSubjectOf("share:s1:blackbetty:7", "s1"));
        Assert.True(AuthorizationService.IsShareSubjectOf(longest, "s1"));
        Assert.False(AuthorizationService.IsShareSubjectOf(longest + "x", "s1"));
        Assert.False(AuthorizationService.IsShareSubjectOf(
            prefix + new string('é', (AuthorizationService.ShareViewerSubjectMaxBytes - prefix.Length) / 2 + 1), "s1"));
        foreach (var subject in new[] { "share:s1:", "share:s1", "share:s1x:7", "share:s2:7", "alice" })
        {
            Assert.False(AuthorizationService.IsShareSubjectOf(subject, "s1"));
        }
        Assert.Equal(512, AuthorizationService.ShareViewerSubjectMaxBytes);
        Assert.Equal(100, Cap);
    }

    [Fact]
    public void A_whole_session_revoke_drops_the_subject_cutoffs_it_subsumes()
    {
        var authz = new AuthorizationService();
        authz.RevokeShareViewerSubject("s1", "share:s1:a", 100);
        authz.RevokeShareViewerSubject("s1", "share:s1:b", 200);
        authz.RevokeShareViewerSubject("s1", "share:s1:c", 300);
        authz.RevokeShareViewerSubject("s2", "share:s2:a", 100);
        authz.RevokeShareViewers("s1", 200);
        Assert.Null(authz.ShareViewerSubjectRevocation("s1", "share:s1:a"));
        Assert.Null(authz.ShareViewerSubjectRevocation("s1", "share:s1:b"));
        Assert.Equal(300, authz.ShareViewerSubjectRevocation("s1", "share:s1:c"));
        Assert.Equal(100, authz.ShareViewerSubjectRevocation("s2", "share:s2:a"));
        Assert.True(authz.ShareViewerRevoked(Share("share:s1:a", 150.0), "s1"));
        Assert.True(authz.ShareViewerRevoked(Share("share:s1:c", 250.0), "s1"));
        // A session that holds no subject cutoff is revoked as before.
        authz.RevokeShareViewers("s3", 200);
        Assert.Equal(200, authz.ShareViewerRevocation("s3"));
    }

    [Fact]
    public void A_refused_whole_session_revoke_drops_no_subject_cutoff()
    {
        var authz = new AuthorizationService();
        for (var n = 0; n < AuthorizationService.ShareViewerRevocationsMax; n++) authz.RevokeShareViewers($"other-{n}", 100);
        authz.RevokeShareViewerSubject("s1", "share:s1:a", 100);
        Assert.Throws<ShareViewerRevocationsFullException>(() => authz.RevokeShareViewers("s1", 200));
        Assert.Equal(100, authz.ShareViewerSubjectRevocation("s1", "share:s1:a"));
    }

    [Fact]
    public void A_full_subject_table_refuses_a_new_subject_but_moves_a_held_one_forward()
    {
        var authz = new AuthorizationService();
        for (var n = 0; n < AuthorizationService.ShareViewerSubjectRevocationsMax; n++)
        {
            authz.RevokeShareViewerSubject($"s{n / Cap}", $"share:s{n / Cap}:{n % Cap}", 100);
        }
        Assert.Throws<ShareViewerRevocationsFullException>(() => authz.RevokeShareViewerSubject("fresh", "share:fresh:a", 200));
        Assert.Null(authz.ShareViewerSubjectRevocation("fresh", "share:fresh:a"));
        Assert.Equal(200, authz.RevokeShareViewerSubject("s0", "share:s0:0", 200));
        var later = 100.5 + AuthorizationService.ShareViewerRevocationTtlSeconds;
        Assert.Equal(later, authz.RevokeShareViewerSubject("fresh", "share:fresh:a", later));
        Assert.Equal(10000, AuthorizationService.ShareViewerSubjectRevocationsMax);
    }

    /// <summary>
    /// Races checks against the revoke that records a session cutoff and drops the
    /// subject cutoff it subsumes. Every state the table passes through refuses the
    /// token, so no check may admit it. Reading the two cutoffs under separate
    /// locks could fall between them and see neither; with the checkers spinning
    /// on their own threads that interleaving shows up within a few thousand
    /// revokes.
    /// </summary>
    [Fact]
    public void A_whole_session_revoke_never_lets_a_check_see_neither_cutoff()
    {
        var authz = new AuthorizationService();
        const int checkers = 4;
        for (var i = 0; i < 5000; i++)
        {
            var sessionId = $"s{i}";
            var subject = $"share:{sessionId}:a";
            authz.RevokeShareViewerSubject(sessionId, subject, 100);
            var token = Share(subject, 50.0);
            var stop = 0;
            var admitted = 0;
            using var started = new CountdownEvent(checkers);
            var threads = Enumerable.Range(0, checkers).Select(_ => new Thread(() =>
            {
                started.Signal();
                while (Volatile.Read(ref stop) == 0)
                {
                    if (!authz.ShareViewerRevoked(token, sessionId)) Volatile.Write(ref admitted, 1);
                }
            })).ToList();
            threads.ForEach(thread => thread.Start());
            started.Wait();
            authz.RevokeShareViewers(sessionId, 200);
            Volatile.Write(ref stop, 1);
            threads.ForEach(thread => thread.Join());
            Assert.True(admitted == 0, $"iteration {i}: a token refused before and after the revoke was admitted during it");
        }
    }
}
