export type AppPageKind = "dashboard" | "session" | "operator" | "replay";
export type SessionMode = "open" | "hijack";
export type SessionSurface = "user" | "operator";
export type AsyncState<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: T };

export interface ViewModelStatus {
  tone: "info" | "ok" | "error";
  text: string;
}

export interface CommandState {
  pending: boolean;
  lastError: string | null;
}

export interface WidgetMountState {
  mounted: boolean;
  error: string | null;
}

export interface SessionSummary {
  sessionId: string;
  displayName: string;
  connectorType: string;
  lifecycleState: string;
  inputMode: SessionMode;
  connected: boolean;
  autoStart: boolean;
  tags: string[];
  recordingEnabled: boolean;
  recordingPath: string | null;
  lastError: string | null;
}

export interface SessionDetails {
  summary: SessionSummary;
  snapshotPromptId: string | null;
}

export interface SessionRuntimeState {
  summary: SessionSummary;
  snapshotPromptId: string | null;
  analysis: string | null;
}

export interface OperatorWorkspaceState {
  session: SessionRuntimeState;
  status: ViewModelStatus;
  modeCommand: CommandState;
  utilityCommand: CommandState;
  widget: WidgetMountState;
}

export interface UserWorkspaceState {
  session: SessionRuntimeState;
  status: ViewModelStatus;
  widget: WidgetMountState;
}

export interface RecordingEntryView {
  ts: number | null;
  event: string;
  payload: Record<string, unknown>;
  screen: string;
}

export interface ReplayIndexState {
  entries: RecordingEntryView[];
  index: number;
  filter: string;
  limit: number;
  total: number;
  status: ViewModelStatus;
}

export interface AppBootstrap {
  page_kind: AppPageKind;
  title: string;
  app_path: string;
  assets_path: string;
  session_id?: string;
  surface?: SessionSurface;
}
