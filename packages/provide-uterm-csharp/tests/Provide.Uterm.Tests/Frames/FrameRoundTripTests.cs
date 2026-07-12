//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Frames;

namespace Provide.Uterm.Tests.Frames;

public class FrameRoundTripTests
{
    [Fact]
    public void Builders_EncodeDecode_AllBuilderTypes()
    {
        var frames = new IFrame[]
        {
            FrameBuilders.MakeErrorFrame("boom"),
            FrameBuilders.MakePongFrame(1.5),
            FrameBuilders.MakeHeartbeatAckFrame(99.0, 2.5),
            FrameBuilders.MakeWorkerConnectedFrame("w1", 3.5),
            FrameBuilders.MakeWorkerDisconnectedFrame("w2", 4.5),
            FrameBuilders.MakeTermFrame("hi", 5.5),
            FrameBuilders.MakeSnapshotFrame(new FrameBuilders.SnapshotParams
            {
                Screen = "s",
                Cursor = new Dictionary<string, int> { ["x"] = 1, ["y"] = 2 },
                Cols = 80,
                Rows = 25,
                ScreenHash = "h",
                CursorAtEnd = true,
                HasTrailingSpace = false,
                Ts = 6.5,
                RawTail = "tail",
            }),
            FrameBuilders.MakeAnalysisFrame("fmt", new Dictionary<string, object?> { ["a"] = 1L }, 7.5),
            FrameBuilders.MakeHijackStateFrame(true, "op", 10.0, "hijack"),
            FrameBuilders.MakeHelloFrame(),
            FrameBuilders.NewIdentityFrame("alice"),
            FrameBuilders.CoerceWorkerStatusFrame(new Dictionary<string, object?>
            {
                ["cpu"] = 1.5,
                ["ts"] = 8.5,
            }),
        };

        foreach (var f in frames)
        {
            var bytes = FrameCodec.EncodeFrame(f);
            Assert.NotEmpty(bytes);
            var decoded = FrameCodec.DecodeFrame(bytes);
            Assert.Equal(f.FrameType, decoded.FrameType);
            Assert.Equal(f.Type, decoded.Type);

            var json = FrameCodec.EncodeFrameString(f);
            var again = FrameCodec.DecodeFrame(json);
            Assert.Equal(f.FrameType, again.FrameType);
        }
    }

    [Theory]
    [InlineData(FrameTypeNames.Input, """{"type":"input","data":"x","ts":1.0}""")]
    [InlineData(FrameTypeNames.SnapshotReq, """{"type":"snapshot_req","ts":1.0}""")]
    [InlineData(FrameTypeNames.Control, """{"type":"control","action":"pause","owner":"op","lease_s":30,"ts":1.0}""")]
    [InlineData(FrameTypeNames.HijackRequest, """{"type":"hijack_request","token":"t","ts":1.0}""")]
    [InlineData(FrameTypeNames.HijackRelease, """{"type":"hijack_release","ts":1.0}""")]
    [InlineData(FrameTypeNames.HijackStep, """{"type":"hijack_step","ts":1.0}""")]
    [InlineData(FrameTypeNames.WorkerHello, """{"type":"worker_hello","mode":"rest","ts":1.0}""")]
    [InlineData(FrameTypeNames.Heartbeat, """{"type":"heartbeat","ts":1.0}""")]
    [InlineData(FrameTypeNames.Ping, """{"type":"ping","ts":1.0}""")]
    [InlineData(FrameTypeNames.Resume, """{"type":"resume","token":"tok","player_id":3}""")]
    [InlineData(FrameTypeNames.SessionToken, """{"type":"session_token","token":"tok","player_id":2}""")]
    [InlineData(FrameTypeNames.ResumeOk, """{"type":"resume_ok"}""")]
    [InlineData(FrameTypeNames.ResumeFailed, """{"type":"resume_failed","reason":"nope"}""")]
    [InlineData(FrameTypeNames.LinkPatterns, """{"type":"link_patterns","patterns":[]}""")]
    [InlineData(FrameTypeNames.InputModeChanged, """{"type":"input_mode_changed","input_mode":"open","ts":1.0}""")]
    [InlineData(FrameTypeNames.ApprovalPending, """{"type":"approval_pending","command":"ls","request_id":"r1","expires_at":9.0}""")]
    [InlineData(FrameTypeNames.ApprovalResolved, """{"type":"approval_resolved","outcome":"approved","request_id":"r1"}""")]
    [InlineData(FrameTypeNames.PresenceUpdate, """{"type":"presence_update","user_id":"u1"}""")]
    [InlineData(FrameTypeNames.PresenceSync, """{"type":"presence_sync","users":[],"config":{},"owner_id":"o"}""")]
    [InlineData(FrameTypeNames.PresenceLeave, """{"type":"presence_leave","user_id":"u1","ts":1.0}""")]
    [InlineData(FrameTypeNames.ControlTransfer, """{"type":"control_transfer","from_user_id":"a","to_user_id":"b","reason":"x","queued_keys":""}""")]
    public void Decode_KnownWireTypes(string type, string json)
    {
        var frame = FrameCodec.DecodeFrame(json);
        Assert.Equal(type, frame.Type);
        Assert.Equal(type, frame.FrameType);
        var re = FrameCodec.EncodeFrame(frame);
        Assert.Contains(type, Encoding.UTF8.GetString(re), StringComparison.Ordinal);
    }

    [Fact]
    public void Decode_UnknownType_Throws()
    {
        Assert.Throws<ArgumentException>(() => FrameCodec.DecodeFrame("""{"type":"nope"}"""));
    }

    [Fact]
    public void Decode_NotObject_Throws()
    {
        Assert.Throws<ArgumentException>(() => FrameCodec.DecodeFrame("[1]"));
    }

    [Fact]
    public void Decode_ForbidType_UnknownField_Throws()
    {
        Assert.Throws<ArgumentException>(() =>
            FrameCodec.DecodeFrame("""{"type":"term","data":"x","extra":1}"""));
    }

    [Fact]
    public void Encode_TypeMismatch_Throws()
    {
        var f = new TermFrame { Type = "wrong", Data = "x" };
        Assert.Throws<ArgumentException>(() => FrameCodec.EncodeFrame(f));
    }

    [Fact]
    public void CoerceWorkerStatusFrame_CollectsExtrasAndFillsTs()
    {
        var f = FrameBuilders.CoerceWorkerStatusFrame(new Dictionary<string, object?>
        {
            ["type"] = "status",
            ["load"] = 3,
            ["ok"] = true,
        });
        Assert.Equal(FrameTypeNames.Status, f.Type);
        Assert.NotNull(f.Ts);
        Assert.NotNull(f.Extra);
        Assert.Equal(3, Convert.ToInt32(f.Extra!["load"]));
    }
}
