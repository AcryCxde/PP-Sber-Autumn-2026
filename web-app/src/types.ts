export type Run = {
  id: string;
  status: "running" | "clarification" | "completed" | "failed";
  question?: { id: string; text: string };
  result?: { id: string; title: string; content: string };
};
export type SaveAck = {
  saved: true;
  artifactId: string;
  version: string;
  savedAt: string;
};
export type Pending = {
  action: "create" | "start" | "answer" | "save";
  requestId: string;
  body: Record<string, unknown>;
};
export type Project = {
  id: string;
  title: string;
  context: string;
  serverId?: string;
  run?: Run;
  saved?: SaveAck;
  pending?: Pending;
  updatedAt: string;
};
export type MetricName =
  | "project_started"
  | "request_sent"
  | "response_received"
  | "artifact_saved"
  | "artifact_downloaded"
  | "run_failed"
  | "run_created"
  | "workspace_left";
export type Metric = {
  name: MetricName;
  at: string;
  projectId?: string;
  requestId?: string;
  runId?: string;
  artifactId?: string;
  participantId?: string;
  buildVersion?: string;
  code?: string;
  durationMs?: number;
};
