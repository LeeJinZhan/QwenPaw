import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate } from "react-router-dom";
import { Button, Drawer, Input, Modal, Space, Spin, Tooltip } from "antd";
import { FixedSizeList, type ListChildComponentProps } from "react-window";
import { IconButton } from "@agentscope-ai/design";
import {
  SparkOperateRightLine,
  SparkLockLine,
  SparkLockFill,
} from "@agentscope-ai/icons";
import {
  useChatAnywhereSessionsState,
  useChatAnywhereSessions,
} from "@agentscope-ai/chat";
import { useTranslation } from "react-i18next";
import sessionApi from "../../sessionApi";
import { buildSessionPath } from "../../../../utils/sessionRoute";
import { useCodingMode } from "../../../../stores/codingModeStore";
import ChatSessionItem from "../ChatSessionItem";
import { getChannelLabel } from "../../../Control/Channels/components";
import { ContextMenu } from "../../../../components/ContextMenu";
import {
  useSessionListData,
  type ExtendedChatSession,
  formatCreatedAt,
} from "./useSessionListData";
import styles from "./index.module.less";
import {
  isRuntimeManagedChat,
  RUNTIME_CONNECTION_CHANGED_EVENT,
  runtimeConsoleApi,
} from "../../../../api/modules/runtimeConsole";

/** Fixed height of each session item in pixels (matches CSS min-height) */
const ITEM_HEIGHT = 77;

/** Data passed to each row via FixedSizeList's itemData prop */
interface SessionRowData {
  sortedSessions: ExtendedChatSession[];
  currentSessionId: string | undefined;
  switchingSessionId: string | null;
  editingSessionId: string | null;
  editValue: string;
  t: ReturnType<typeof useTranslation>["t"];
  handleSessionClick: (sessionId: string) => void;
  handleEditStart: (sessionId: string, currentName: string) => void;
  handleDelete: (sessionId: string) => void;
  handlePinToggle: (sessionId: string) => void;
  handleEditChange: (value: string) => void;
  handleEditSubmit: () => void;
  handleEditCancel: () => void;
  handleItemContextMenu: (sessionId: string, event: React.MouseEvent) => void;
}

/** Memoized row renderer — only re-renders when its specific props change */
const SessionRow = React.memo(function SessionRow({
  index,
  style,
  data,
}: ListChildComponentProps<SessionRowData>) {
  const session = data.sortedSessions[index];
  const channelKey = session.channel?.trim() || "";
  const channelLabel = channelKey
    ? getChannelLabel(channelKey, data.t)
    : undefined;
  const isEditing = data.editingSessionId === session.id;
  const runtimeManaged = isRuntimeManagedChat(session);
  const isDisabled =
    !!data.switchingSessionId && session.id !== data.switchingSessionId;

  return (
    <div style={style}>
      <ChatSessionItem
        sessionId={session.id!}
        name={session.name || "New Chat"}
        time={formatCreatedAt(session.updatedAt ?? null)}
        channelKey={channelKey || undefined}
        channelLabel={channelLabel}
        chatStatus={session.status}
        generating={session.generating}
        pinned={session.pinned}
        active={session.id === data.currentSessionId}
        disabled={isDisabled}
        editing={isEditing}
        editValue={isEditing ? data.editValue : undefined}
        onClick={data.handleSessionClick}
        onEdit={runtimeManaged ? undefined : data.handleEditStart}
        onDelete={runtimeManaged ? undefined : data.handleDelete}
        onPin={runtimeManaged ? undefined : data.handlePinToggle}
        onEditChange={data.handleEditChange}
        onEditSubmit={data.handleEditSubmit}
        onEditCancel={data.handleEditCancel}
        onContextMenu={data.handleItemContextMenu}
      />
    </div>
  );
});

interface ChatSessionDrawerProps {
  /** Whether the drawer is visible */
  open: boolean;
  /** Callback to close the drawer */
  onClose: () => void;
  /** Whether the drawer is pinned (stays open) */
  pinned?: boolean;
  /** Callback to toggle the pinned state */
  onPinChange?: (pinned: boolean) => void;
}

const ChatSessionDrawer: React.FC<ChatSessionDrawerProps> = (props) => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { sessions, currentSessionId, setCurrentSessionId, setSessions } =
    useChatAnywhereSessionsState();
  const { codingMode } = useCodingMode();
  const { createSession } = useChatAnywhereSessions();
  const [runtimeConnected, setRuntimeConnected] = useState(() =>
    runtimeConsoleApi.isConnected(),
  );
  const [runtimeIdentity, setRuntimeIdentity] = useState(() =>
    runtimeConsoleApi.currentIdentity(),
  );
  const [runtimeConnectOpen, setRuntimeConnectOpen] = useState(false);
  const [runtimeUserId, setRuntimeUserId] = useState("");
  const [runtimeOrgId, setRuntimeOrgId] = useState("");
  const [runtimeConnectLoading, setRuntimeConnectLoading] = useState(false);
  const [runtimeConnectError, setRuntimeConnectError] = useState("");

  useEffect(() => {
    const syncConnection = () => {
      setRuntimeConnected(runtimeConsoleApi.isConnected());
      setRuntimeIdentity(runtimeConsoleApi.currentIdentity());
    };
    window.addEventListener(RUNTIME_CONNECTION_CHANGED_EVENT, syncConnection);
    return () =>
      window.removeEventListener(
        RUNTIME_CONNECTION_CHANGED_EVENT,
        syncConnection,
      );
  }, []);

  /** Create a new session; close the drawer only when not pinned */
  const handleCreateSession = useCallback(async () => {
    await createSession();
    if (!props.pinned) {
      props.onClose();
    }
  }, [createSession, props.onClose, props.pinned]);

  /** Height of the virtual list container, measured via ResizeObserver */
  const [listHeight, setListHeight] = useState(0);
  const observerRef = useRef<ResizeObserver | null>(null);

  /** Callback ref: attach a ResizeObserver whenever the wrapper DOM node appears */
  const listWrapperRef = useCallback((node: HTMLDivElement | null) => {
    if (observerRef.current) {
      observerRef.current.disconnect();
      observerRef.current = null;
    }
    if (!node) return;
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const height = entry.contentRect.height;
        if (height > 0) setListHeight(height);
      }
    });
    observer.observe(node);
    observerRef.current = observer;
    const initialHeight = node.clientHeight;
    if (initialHeight > 0) setListHeight(initialHeight);
  }, []);

  /**
   * onSessionClick: inside the chat context tree, so we can call
   * setCurrentSessionId / navigate directly.
   */
  const onSessionClick = useCallback(
    (sessionId: string) => {
      sessionApi.isSessionSwitching = true;
      sessionApi
        .preloadSession(sessionId)
        .then(({ realId }) => {
          const effectiveId = realId || sessionId;
          const targetUrl = buildSessionPath(
            codingMode ? "coding" : "chat",
            effectiveId,
          );
          sessionApi.lastNavigatedChatId = effectiveId;
          navigate(targetUrl, { replace: true });
          setCurrentSessionId(sessionId);
        })
        .catch(() => {
          setCurrentSessionId(sessionId);
        })
        .finally(() => {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              sessionApi.finishSessionSwitch();
            });
          });
        });
    },
    [codingMode, navigate, setCurrentSessionId],
  );

  const extSessions = sessions as ExtendedChatSession[];
  const setExtSessions = setSessions as (s: ExtendedChatSession[]) => void;

  const {
    sortedSessions,
    loading: listLoading,
    switchingSessionId,
    editingSessionId,
    editValue,
    handleSessionClick,
    handleEditStart,
    handleDelete,
    handlePinToggle,
    handleEditChange,
    handleEditSubmit,
    handleEditCancel,
    handleItemContextMenu,
    contextMenu: sharedContextMenu,
    contextMenuItems,
    refreshSessions,
  } = useSessionListData(extSessions, setExtSessions, {
    active: props.open,
    currentSessionId,
    onSessionClick,
  });

  const handleRuntimeConnect = useCallback(async () => {
    if (!runtimeUserId.trim() || !runtimeOrgId.trim()) return;
    setRuntimeConnectLoading(true);
    setRuntimeConnectError("");
    try {
      await runtimeConsoleApi.connect(
        runtimeUserId.trim(),
        runtimeOrgId.trim(),
      );
      setRuntimeConnectOpen(false);
      await refreshSessions();
    } catch {
      setRuntimeConnectError(
        t(
          "chat.runtime.loginFailed",
          "Connection failed. Check the user ID and organization ID, then try again.",
        ),
      );
    } finally {
      setRuntimeConnectLoading(false);
    }
  }, [refreshSessions, runtimeOrgId, runtimeUserId, t]);

  const handleRuntimeDisconnect = useCallback(async () => {
    const currentWasRuntimeManaged =
      (window as Window & { currentChannel?: string }).currentChannel ===
      "bank-runtime";
    runtimeConsoleApi.disconnect();
    await refreshSessions();
    if (currentWasRuntimeManaged) await createSession();
  }, [createSession, refreshSessions]);

  /** Stable data object for FixedSizeList */
  const itemData = useMemo<SessionRowData>(
    () => ({
      sortedSessions,
      currentSessionId,
      switchingSessionId,
      editingSessionId,
      editValue,
      t,
      handleSessionClick,
      handleEditStart,
      handleDelete,
      handlePinToggle,
      handleEditChange,
      handleEditSubmit,
      handleEditCancel,
      handleItemContextMenu,
    }),
    [
      sortedSessions,
      currentSessionId,
      switchingSessionId,
      editingSessionId,
      editValue,
      t,
      handleSessionClick,
      handleEditStart,
      handleDelete,
      handlePinToggle,
      handleEditChange,
      handleEditSubmit,
      handleEditCancel,
      handleItemContextMenu,
    ],
  );

  return (
    <Drawer
      open={props.open}
      onClose={props.pinned ? undefined : props.onClose}
      destroyOnHidden={!props.pinned}
      placement="right"
      width={360}
      closable={false}
      title={null}
      mask={!props.pinned}
      styles={{
        header: { display: "none" },
        body: {
          padding: 0,
          display: "flex",
          flexDirection: "column",
          height: "100%",
          overflow: "hidden",
        },
        mask: { background: "transparent" },
      }}
      className={styles.drawer}
    >
      {/* Header bar */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.headerTitle}>{t("chat.allChats")}</span>
        </div>
        <div className={styles.headerRight}>
          {runtimeConnected ? (
            <Tooltip
              title={t("chat.runtime.disconnect", "Disconnect Runtime")}
            >
              <Button
                type="text"
                size="small"
                onClick={handleRuntimeDisconnect}
              >
                {runtimeIdentity
                  ? `${runtimeIdentity.orgId}/${runtimeIdentity.userId}`
                  : t("chat.runtime.disconnect", "Disconnect Runtime")}
              </Button>
            </Tooltip>
          ) : (
            <Button
              type="text"
              size="small"
              onClick={() => {
                setRuntimeConnectError("");
                setRuntimeConnectOpen(true);
              }}
            >
              {t("chat.runtime.connect", "Connect Runtime")}
            </Button>
          )}
          <Tooltip
            title={
              props.pinned
                ? t("chat.unpinDrawer", "Unpin")
                : t("chat.pinDrawer", "Pin")
            }
            mouseEnterDelay={0.5}
          >
            <IconButton
              bordered={false}
              icon={props.pinned ? <SparkLockFill /> : <SparkLockLine />}
              className={props.pinned ? styles.pinActive : undefined}
              onClick={() => props.onPinChange?.(!props.pinned)}
            />
          </Tooltip>
          {!props.pinned && (
            <IconButton
              bordered={false}
              icon={<SparkOperateRightLine />}
              onClick={props.onClose}
            />
          )}
        </div>
      </div>

      {/* Create new chat button */}
      <div className={styles.createSection}>
        <div className={styles.createButton} onClick={handleCreateSession}>
          {t("chat.createNewChat")}
        </div>
      </div>

      {/* Session list */}
      <div
        className={styles.listWrapper}
        ref={listWrapperRef}
        style={switchingSessionId ? { pointerEvents: "none" } : undefined}
      >
        <div className={styles.topGradient} />
        {listLoading ? (
          <div
            style={{
              display: "flex",
              justifyContent: "center",
              padding: 40,
            }}
          >
            <Spin />
          </div>
        ) : (
          <FixedSizeList
            height={listHeight}
            width="100%"
            itemCount={sortedSessions.length}
            itemSize={ITEM_HEIGHT}
            overscanCount={20}
            itemData={itemData}
            className={styles.list}
          >
            {SessionRow}
          </FixedSizeList>
        )}
        <div className={styles.bottomGradient} />
      </div>

      {/* Shared context menu — single instance for all session items */}
      <ContextMenu
        visible={sharedContextMenu.visible}
        x={sharedContextMenu.x}
        y={sharedContextMenu.y}
        items={contextMenuItems}
        onClose={sharedContextMenu.hide}
      />

      <Modal
        open={runtimeConnectOpen}
        title={t("chat.runtime.connectTitle", "Connect Bank Runtime")}
        okText={t("chat.runtime.connect", "Connect Runtime")}
        cancelText={t("common.cancel", "Cancel")}
        confirmLoading={runtimeConnectLoading}
        okButtonProps={{
          disabled: !runtimeUserId.trim() || !runtimeOrgId.trim(),
        }}
        onOk={() => void handleRuntimeConnect()}
        onCancel={() => {
          if (runtimeConnectLoading) return;
          setRuntimeConnectError("");
          setRuntimeConnectOpen(false);
        }}
      >
        <p>
          {t(
            "chat.runtime.connectDescription",
            "Enter the external user ID and organization ID whose Runtime-managed conversation history you need to inspect.",
          )}
        </p>
        <Space direction="vertical" size={12} style={{ width: "100%" }}>
          <Input
            autoComplete="off"
            value={runtimeUserId}
            placeholder={t("chat.runtime.userId", "User ID")}
            onChange={(event) => setRuntimeUserId(event.target.value)}
          />
          <Input
            autoComplete="off"
            value={runtimeOrgId}
            placeholder={t("chat.runtime.orgId", "Organization ID")}
            onChange={(event) => setRuntimeOrgId(event.target.value)}
            onPressEnter={() => void handleRuntimeConnect()}
          />
          {runtimeConnectError ? (
            <div role="alert" style={{ color: "var(--ant-color-error)" }}>
              {runtimeConnectError}
            </div>
          ) : null}
        </Space>
      </Modal>
    </Drawer>
  );
};

export default ChatSessionDrawer;
