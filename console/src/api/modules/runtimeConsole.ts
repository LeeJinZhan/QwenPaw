import { buildAuthHeaders } from "../authHeaders";
import { getApiUrl } from "../config";
import type { ChatHistory, ChatSpec, ChatStatus, Message } from "../types";

export const RUNTIME_MANAGED_CHANNEL = "bank-runtime";
export const RUNTIME_SESSION_ID_PREFIX = "runtime:";

const RUNTIME_USER_TOKEN_KEY = "qwenpaw-runtime-user-token";
const RUNTIME_USER_TOKEN_HEADER = "X-Runtime-User-Token";
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

interface RuntimeLoginResponse {
  access_token: string;
  token_type?: string;
}

export class RuntimeConsoleRequestError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "RuntimeConsoleRequestError";
    this.status = status;
  }
}

function getRuntimeUserToken(): string {
  return sessionStorage.getItem(RUNTIME_USER_TOKEN_KEY)?.trim() || "";
}

function setRuntimeUserToken(token: string): void {
  sessionStorage.setItem(RUNTIME_USER_TOKEN_KEY, token);
  window.dispatchEvent(new Event(RUNTIME_CONNECTION_CHANGED_EVENT));
}

function clearRuntimeUserToken(): void {
  sessionStorage.removeItem(RUNTIME_USER_TOKEN_KEY);
  window.dispatchEvent(new Event(RUNTIME_CONNECTION_CHANGED_EVENT));
}

async function runtimeRequest<T>(
  path: string,
  init: RequestInit = {},
  requireRuntimeLogin = true,
): Promise<T> {
  const runtimeToken = getRuntimeUserToken();
  if (requireRuntimeLogin && !runtimeToken) {
    throw new RuntimeConsoleRequestError(401, "Runtime login required");
  }

  const headers: Record<string, string> = {
    Accept: "application/json",
    ...buildAuthHeaders(),
    ...(init.headers as Record<string, string> | undefined),
  };
  if (runtimeToken) headers[RUNTIME_USER_TOKEN_HEADER] = runtimeToken;
  if (init.body) headers["Content-Type"] = "application/json";

  const response = await fetch(getApiUrl(path), { ...init, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && requireRuntimeLogin) {
      clearRuntimeUserToken();
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
  isConnected: (): boolean => Boolean(getRuntimeUserToken()),

  login: async (username: string, password: string): Promise<void> => {
    const response = await runtimeRequest<RuntimeLoginResponse>(
      "/runtime-console/login",
      {
        method: "POST",
        body: JSON.stringify({ username, password }),
      },
      false,
    );
    const token = response.access_token?.trim();
    if (!token) {
      throw new RuntimeConsoleRequestError(
        502,
        "Runtime returned an invalid login response",
      );
    }
    setRuntimeUserToken(token);
  },

  disconnect: (): void => clearRuntimeUserToken(),

  listConversations: (page = 1, pageSize = 100) =>
    runtimeRequest<RuntimeConversationPage>(
      `/runtime-console/conversations?page=${page}&page_size=${pageSize}`,
    ),

  getConversation: (conversationId: string) =>
    runtimeRequest<RuntimeConversationDetail>(
      `/runtime-console/conversations/${encodeURIComponent(conversationId)}`,
    ),
};
