//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package frames

// Frame is implemented by every frame struct. FrameType returns the wire
// "type" discriminator literal the struct corresponds to (the Literal[...]
// value of the matching Pydantic model) — not the value currently stored in
// the struct's Type field.
type Frame interface {
	FrameType() string
}

// FrameType returns TypeTerm.
func (TermFrame) FrameType() string { return TypeTerm }

// FrameType returns TypeInput.
func (InputFrame) FrameType() string { return TypeInput }

// FrameType returns TypeSnapshotReq.
func (SnapshotReqFrame) FrameType() string { return TypeSnapshotReq }

// FrameType returns TypeSnapshot.
func (SnapshotFrame) FrameType() string { return TypeSnapshot }

// FrameType returns TypeControl.
func (ControlFrame) FrameType() string { return TypeControl }

// FrameType returns TypeHijackState.
func (HijackStateFrame) FrameType() string { return TypeHijackState }

// FrameType returns TypeHijackRequest.
func (HijackRequestFrame) FrameType() string { return TypeHijackRequest }

// FrameType returns TypeHijackRelease.
func (HijackReleaseFrame) FrameType() string { return TypeHijackRelease }

// FrameType returns TypeHijackStep.
func (HijackStepFrame) FrameType() string { return TypeHijackStep }

// FrameType returns TypeWorkerConnected.
func (WorkerConnectedFrame) FrameType() string { return TypeWorkerConnected }

// FrameType returns TypeWorkerDisconnected.
func (WorkerDisconnectedFrame) FrameType() string { return TypeWorkerDisconnected }

// FrameType returns TypeWorkerHello.
func (WorkerHelloFrame) FrameType() string { return TypeWorkerHello }

// FrameType returns TypeHeartbeat.
func (HeartbeatFrame) FrameType() string { return TypeHeartbeat }

// FrameType returns TypeHeartbeatAck.
func (HeartbeatAckFrame) FrameType() string { return TypeHeartbeatAck }

// FrameType returns TypePing.
func (PingFrame) FrameType() string { return TypePing }

// FrameType returns TypePong.
func (PongFrame) FrameType() string { return TypePong }

// FrameType returns TypeHello.
func (HelloFrame) FrameType() string { return TypeHello }

// FrameType returns TypeResume.
func (ResumeFrame) FrameType() string { return TypeResume }

// FrameType returns TypeIdentity.
func (IdentityFrame) FrameType() string { return TypeIdentity }

// FrameType returns TypeSessionToken.
func (SessionTokenFrame) FrameType() string { return TypeSessionToken }

// FrameType returns TypeResumeOk.
func (ResumeOkFrame) FrameType() string { return TypeResumeOk }

// FrameType returns TypeResumeFailed.
func (ResumeFailedFrame) FrameType() string { return TypeResumeFailed }

// FrameType returns TypeLinkPatterns.
func (LinkPatternsFrame) FrameType() string { return TypeLinkPatterns }

// FrameType returns TypeAnalysis.
func (AnalysisFrame) FrameType() string { return TypeAnalysis }

// FrameType returns TypeError.
func (ErrorFrame) FrameType() string { return TypeError }

// FrameType returns TypeStatus.
func (StatusFrame) FrameType() string { return TypeStatus }

// FrameType returns TypeInputModeChanged.
func (InputModeChangedFrame) FrameType() string { return TypeInputModeChanged }

// FrameType returns TypeApprovalPending.
func (ApprovalPendingFrame) FrameType() string { return TypeApprovalPending }

// FrameType returns TypeApprovalResolved.
func (ApprovalResolvedFrame) FrameType() string { return TypeApprovalResolved }

// FrameType returns TypePresenceUpdate.
func (PresenceUpdateFrame) FrameType() string { return TypePresenceUpdate }

// FrameType returns TypePresenceSync.
func (PresenceSyncFrame) FrameType() string { return TypePresenceSync }

// FrameType returns TypePresenceLeave.
func (PresenceLeaveFrame) FrameType() string { return TypePresenceLeave }

// FrameType returns TypeControlTransfer.
func (ControlTransferFrame) FrameType() string { return TypeControlTransfer }
