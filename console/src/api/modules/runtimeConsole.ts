import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import type { ChatHistory, ChatSpec, ChatStatus, Message } from "../types";

export const RUNTIME_MANAGED_CHANNEL = "bank-runtime";
export const RUNTIME_SESSION_ID_PREFIX = "runtime:";

const RUNTIME_EXTERNAL_IDENTITY_KEY =
  "qwenpaw-runtime-external-identity-v1";
const RUNTIME_EXTERNAL_USER_ID_HEADER = "X-Runtime-External-User-Id";
const RUNTIME_EXTERNAL_ORG_ID_HEADER = "X-Runtime-External-Org-Id";
export const RUNTIME_CONNECTION_CHANGED_EVENT =
  "qwenpaw:runtime-connection-changed";

export interface RuntimeConversationSummary {
  conversation_id: string;
  assistant_code?: string;
  scenario_code?: string;
  status?: string;
  last_task_id?: string | null;
  last_task_status?: string | null;
  title?: string | null;
  latest_message?: string | null;
  turn_count?: number;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface RuntimeConversationTurn {
  turn_id: string;
  conversation_id: string;
  task_id?: string | null;
  role: string;
  content: string;
  created_at?: string | null;
}

export interface RuntimeConversationDetail extends RuntimeConversationSummary {
  turns?: RuntimeConversationTurn[];
  uploaded_files?: Array<Record<string, unknown>>;
  generated_files?: Array<Record<string, unknown>>;
}

export interface RuntimeConversationPage {
  items: RuntimeConversationSummary[];
  total: number;
  page?: number;
  page_size?: number;
}

export interface RuntimeExternalIdentity {
  userId: string;
  orgId: string;
}

interface RuntimeConnectResponse {
  connected: boolean;
  identity: {
    user_id: string;
    org_id: string;
  };
}

export class RuntimeConsoleRequestError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "RuntimeConsoleRequestError";
    this.status = status;
  }
}

function getRuntimeIdentity(): RuntimeExternalIdentity | null {
  const serialized = sessionStorage.getItem(RUNTIME_EXTERNAL_IDENTITY_KEY);
  if (!serialized) return null;
  try {
    const parsed = JSON.parse(serialized) as Partial<RuntimeExternalIdentity>;
    const userId = String(parsed.userId || "").trim();
    const orgId = String(parsed.orgId || "").trim();
    return userId && orgId ? { userId, orgId } : null;
  } catch {
    return null;
  }
}

function setRuntimeIdentity(identity: RuntimeExternalIdentity): void {
  sessionStorage.setItem(
    RUNTIME_EXTERNAL_IDENTITY_KEY,
    JSON.stringify(identity),
  );
  window.dispatchEvent(new Event(RUNTIME_CONNECTION_CHANGED_EVENT));
}

function clearRuntimeIdentity(): void {
  sessionStorage.removeItem(RUNTIME_EXTERNAL_IDENTITY_KEY);
  window.dispatchEvent(new Event(RUNTIME_CONNECTION_CHANGED_EVENT));
}

async function runtimeRequest<T>(
  path: string,
  init: RequestInit = {},
  requireRuntimeIdentity = true,
): Promise<T> {
  const runtimeIdentity = getRuntimeIdentity();
  if (requireRuntimeIdentity && !runtimeIdentity) {
    throw new RuntimeConsoleRequestError(401, "Runtime identity required");
  }

  const headers: Record<string, string> = {
    Accept: "application/json",
    ...buildAuthHeaders(),
    ...(init.headers as Record<string, string> | undefined),
  };
  if (runtimeIdentity) {
    headers[RUNTIME_EXTERNAL_USER_ID_HEADER] = runtimeIdentity.userId;
    headers[RUNTIME_EXTERNAL_ORG_ID_HEADER] = runtimeIdentity.orgId;
  }
  if (init.body) headers["Content-Type"] = "application/json";

  const response = await fetch(getApiUrl(path), { ...init, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && requireRuntimeIdentity) {
      clearRuntimeIdentity();
    }
    const detail = payload?.detail;
    const message =
      (typeof detail === "string" && detail.trim()) ||
      (typeof detail?.message === "string" && detail.message.trim()) ||
      "Runtime request failed";
    throw new RuntimeConsoleRequestError(response.status, message);
  }
  // Bank Runtime uses the shared API envelope { data, trace_id } while the
  // proxy's unit tests and compatible deployments may return a bare object.
  return (
    payload && typeof payload === "object" && "data" in payload
      ? payload.data
      : payload
  ) as T;
}

function runtimeChatStatus(value: string | null | undefined): ChatStatus {
  return value === "running" || value === "queued" ? "running" : "idle";
}

export function isRuntimeManagedChat(
  session:
    | { channel?: string; meta?: Record<string, unknown> }
    | null
    | undefined,
): boolean {
  return Boolean(
    session &&
      (session.channel === RUNTIME_MANAGED_CHANNEL ||
        session.meta?.runtimeManaged === true),
  );
}

export function runtimeConversationIdFromSessionId(
  sessionId: string,
): string | null {
  return sessionId.startsWith(RUNTIME_SESSION_ID_PREFIX)
    ? sessionId.slice(RUNTIME_SESSION_ID_PREFIX.length)
    : null;
}

export function runtimeSummaryToChatSpec(
  summary: RuntimeConversationSummary,
): ChatSpec {
  const remoteId = `${RUNTIME_SESSION_ID_PREFIX}${summary.conversation_id}`;
  return {
    id: remoteId,
    session_id: remoteId,
    user_id: "runtime-current-user",
    channel: RUNTIME_MANAGED_CHANNEL,
    name: summary.title || summary.latest_message || "Runtime conversation",
    created_at: summary.created_at ?? null,
    updated_at: summary.updated_at ?? summary.created_at ?? null,
    status: runtimeChatStatus(summary.last_task_status),
    pinned: false,
    meta: {
      runtimeManaged: true,
      runtimeConversationId: summary.conversation_id,
      readOnly: true,
      assistantCode: summary.assistant_code || summary.scenario_code || "",
      turnCount: summary.turn_count ?? 0,
    },
  };
}

export function runtimeConversationToChatHistory(
  detail: RuntimeConversationDetail,
): ChatHistory {
  const messages: Message[] = (detail.turns || []).map((turn) => ({
    id: turn.turn_id,
    type: "message",
    role: turn.role,
    content: [{ type: "text", text: turn.content || "" }],
    metadata: {
      timestamp: turn.created_at ?? undefined,
      runtime_task_id: turn.task_id ?? undefined,
    },
  }));
  return {
    messages,
    status: runtimeChatStatus(detail.last_task_status),
  };
}

export const runtimeConsoleApi = {
  isConnected: (): boolean => Boolean(getRuntimeIdentity()),

  currentIdentity: (): RuntimeExternalIdentity | null => getRuntimeIdentity(),

  connect: async (userId: string, orgId: string): Promise<void> => {
    const response = await runtimeRequest<RuntimeConnectResponse>(
      "/runtime-console/connect",
      {
        method: "POST",
        body: JSON.stringify({ user_id: userId, org_id: orgId }),
      },
      false,
    );
    const connectedUserId = response.identity?.user_id?.trim();
    const connectedOrgId = response.identity?.org_id?.trim();
    if (!response.connected || !connectedUserId || !connectedOrgId) {
      throw new RuntimeConsoleRequestError(
        502,
        "Runtime returned an invalid connection response",
      );
    }
    setRuntimeIdentity({ userId: connectedUserId, orgId: connectedOrgId });
  },

  disconnect: (): void => clearRuntimeIdentity(),

  listConversations: (page = 1, pageSize = 100) =>
    runtimeRequest<RuntimeConversationPage>(
      `/runtime-console/conversations?page=${page}&page_size=${pageSize}`,
    ),

  getConversation: (conversationId: string) =>
    runtimeRequest<RuntimeConversationDetail>(
      `/runtime-console/conversations/${encodeURIComponent(conversationId)}`,
    ),
};
