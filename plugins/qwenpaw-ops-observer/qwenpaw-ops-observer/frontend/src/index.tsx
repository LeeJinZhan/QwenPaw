/**
 * Ops Observer frontend: sidebar menu + dashboard route.
 *
 * Registers a "Ops Observer" menu item and a /ops-observer page that
 * visualizes the content-free metrics collected by the backend plugin
 * via the /api/ops-observer/stats/* endpoints.
 */

const { React, antd } = window.QwenPaw.host;
const { useState, useEffect, useRef } = React;
const {
  Card,
  Col,
  Empty,
  Progress,
  Row,
  Segmented,
  Spin,
  Statistic,
  Table,
  Tag,
  Typography,
} = antd;

const pluginId = "qwenpaw-ops-observer";

const TEXTS = {
  zh: {
    title: "运维观测",
    range24h: "24 小时",
    range7d: "7 天",
    range30d: "30 天",
    totalRuns: "运行次数",
    successRate: "成功率",
    avgDuration: "平均耗时",
    toolCalls: "工具调用",
    toolErrorRate: "工具错误率",
    llmCalls: "LLM 调用",
    activeAgents: "活跃 Agent",
    userEvents: "用户事件",
    trend: "运行趋势",
    runs: "运行",
    errors: "错误",
    topTools: "工具排行",
    toolName: "工具",
    calls: "调用次数",
    errorCount: "错误次数",
    avgMs: "平均耗时 (ms)",
    agentStats: "Agent 统计",
    agentId: "Agent",
    llmStats: "LLM 调用指标",
    avgTtft: "平均首 Token (ms)",
    eventStats: "用户事件分布",
    eventType: "事件类型",
    count: "次数",
    recentRuns: "最近运行",
    status: "状态",
    channel: "渠道",
    startedAt: "开始时间",
    duration: "耗时 (ms)",
    noData: "暂无数据",
    loadFailed: "数据加载失败，请检查插件是否正常启动",
    noRuns: "暂无运行数据，请先与 Agent 对话生成数据",
  },
  en: {
    title: "Ops Observer",
    range24h: "24h",
    range7d: "7d",
    range30d: "30d",
    totalRuns: "Total Runs",
    successRate: "Success Rate",
    avgDuration: "Avg Duration",
    toolCalls: "Tool Calls",
    toolErrorRate: "Tool Error Rate",
    llmCalls: "LLM Calls",
    activeAgents: "Active Agents",
    userEvents: "User Events",
    trend: "Run Trend",
    runs: "Runs",
    errors: "Errors",
    topTools: "Top Tools",
    toolName: "Tool",
    calls: "Calls",
    errorCount: "Errors",
    avgMs: "Avg (ms)",
    agentStats: "Agent Stats",
    agentId: "Agent",
    llmStats: "LLM Metrics",
    avgTtft: "Avg TTFT (ms)",
    eventStats: "User Events",
    eventType: "Event Type",
    count: "Count",
    recentRuns: "Recent Runs",
    status: "Status",
    channel: "Channel",
    startedAt: "Started At",
    duration: "Duration (ms)",
    noData: "No data yet",
    loadFailed: "Failed to load data. Please check if the plugin started correctly.",
    noRuns: "No run data yet. Chat with an agent to generate metrics.",
  },
};

function fmtMs(value: number | null): string {
  if (value === null || value === undefined) return "-";
  if (value >= 1000) return `${(value / 1000).toFixed(2)}s`;
  return `${Math.round(value)}ms`;
}

function fmtPct(value: number | null): string {
  if (value === null || value === undefined) return "-";
  return `${(value * 100).toFixed(1)}%`;
}

function fmtBucket(bucket: string): string {
  // "2026-07-17T10" -> "07-17 10:00"
  const [date, hour] = bucket.split("T");
  const [, month, day] = date.split("-");
  return `${month}-${day} ${hour}:00`;
}

/** Lightweight SVG grouped bar chart (runs vs errors per hour bucket). */
function TrendChart({ buckets, isDark, t }: { buckets: any[]; isDark: boolean; t: any }) {
  if (!buckets || buckets.length === 0) {
    return <Empty description={t.noData} style={{ padding: 24 }} />;
  }
  const width = 720;
  const height = 220;
  const padLeft = 40;
  const padBottom = 28;
  const padTop = 12;
  const chartW = width - padLeft - 8;
  const chartH = height - padTop - padBottom;
  const maxVal = Math.max(1, ...buckets.map((b) => b.runs));
  const barGroupW = chartW / buckets.length;
  const barW = Math.max(4, Math.min(20, (barGroupW - 6) / 2));
  const axisColor = isDark ? "#555" : "#d9d9d9";
  const textColor = isDark ? "#999" : "#666";
  const labelEvery = Math.ceil(buckets.length / 12);

  return (
    <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", display: "block" }}>
      {[0, 0.5, 1].map((ratio) => {
        const y = padTop + chartH * (1 - ratio);
        return (
          <g key={ratio}>
            <line x1={padLeft} y1={y} x2={width - 8} y2={y} stroke={axisColor} strokeWidth={0.5} />
            <text x={padLeft - 6} y={y + 4} fontSize={10} fill={textColor} textAnchor="end">
              {Math.round(maxVal * ratio)}
            </text>
          </g>
        );
      })}
      {buckets.map((b, i) => {
        const x = padLeft + i * barGroupW + (barGroupW - barW * 2 - 2) / 2;
        const runH = (b.runs / maxVal) * chartH;
        const errH = ((b.errors || 0) / maxVal) * chartH;
        return (
          <g key={b.bucket}>
            <rect x={x} y={padTop + chartH - runH} width={barW} height={runH} fill="#1677ff" rx={1}>
              <title>{`${fmtBucket(b.bucket)} — ${t.runs}: ${b.runs}, ${t.errors}: ${b.errors || 0}`}</title>
            </rect>
            <rect x={x + barW + 2} y={padTop + chartH - errH} width={barW} height={errH} fill="#ff4d4f" rx={1}>
              <title>{`${fmtBucket(b.bucket)} — ${t.errors}: ${b.errors || 0}`}</title>
            </rect>
            {i % labelEvery === 0 && (
              <text
                x={x + barW}
                y={height - 8}
                fontSize={9}
                fill={textColor}
                textAnchor="middle"
                transform={`rotate(-30 ${x + barW} ${height - 8})`}
              >
                {fmtBucket(b.bucket)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function statusTag(status: string) {
  const color =
    status === "success" ? "success" : status === "cancelled" ? "warning" : "error";
  return <Tag color={color}>{status}</Tag>;
}

function Dashboard() {
  const theme = window.QwenPaw.host.useTheme();
  const locale = window.QwenPaw.host.useLocale();
  const t = locale && locale.startsWith("zh") ? TEXTS.zh : TEXTS.en;
  const isDark = theme === "dark";

  const [hours, setHours] = useState(24);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [data, setData] = useState<any>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const base = "/api/ops-observer";
        const fetchJson = async (path: string) => {
          const resp = await window.QwenPaw.host.fetch(`${base}${path}`);
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          return resp.json();
        };
        const [overview, timeseries, tools, agents, llm, events, recent] = await Promise.all([
          fetchJson(`/stats/overview?hours=${hours}`),
          fetchJson(`/stats/timeseries?hours=${hours}`),
          fetchJson(`/stats/tools?hours=${hours}&limit=10`),
          fetchJson(`/stats/agents?hours=${hours}&limit=10`),
          fetchJson(`/stats/llm?hours=${hours}`),
          fetchJson(`/stats/events?hours=${hours}`),
          fetchJson(`/runs/recent?limit=20`),
        ]);
        if (!cancelled && mountedRef.current) {
          setData({ overview, timeseries, tools, agents, llm, events, recent });
          setFailed(false);
          setLoading(false);
        }
      } catch (e) {
        if (!cancelled && mountedRef.current) {
          setFailed(true);
          setLoading(false);
        }
      }
    }
    setLoading(true);
    load();
    const timer = setInterval(load, 30000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [hours]);

  const cardStyle = {
    background: isDark ? "#1f1f1f" : "#fff",
    height: "100%",
  };

  const ov = data?.overview;
  const llm = data?.llm;

  const toolColumns = [
    { title: t.toolName, dataIndex: "tool_name", key: "tool_name" },
    { title: t.calls, dataIndex: "calls", key: "calls", sorter: (a: any, b: any) => a.calls - b.calls },
    { title: t.errorCount, dataIndex: "errors", key: "errors" },
    {
      title: t.avgMs,
      dataIndex: "avg_duration_ms",
      key: "avg_duration_ms",
      render: (v: number) => fmtMs(v),
    },
  ];

  const agentColumns = [
    { title: t.agentId, dataIndex: "agent_id", key: "agent_id" },
    { title: t.runs, dataIndex: "runs", key: "runs" },
    {
      title: t.successRate,
      key: "success_rate",
      render: (_: any, row: any) => fmtPct(row.runs ? row.success_runs / row.runs : null),
    },
    {
      title: t.avgMs,
      dataIndex: "avg_duration_ms",
      key: "avg_duration_ms",
      render: (v: number) => fmtMs(v),
    },
    { title: t.toolCalls, dataIndex: "tool_calls", key: "tool_calls" },
  ];

  const runColumns = [
    { title: "run_id", dataIndex: "run_id", key: "run_id", ellipsis: true },
    { title: t.agentId, dataIndex: "agent_id", key: "agent_id", ellipsis: true },
    {
      title: t.channel,
      dataIndex: "channel",
      key: "channel",
      render: (v: string) => v || "-",
    },
    {
      title: t.status,
      dataIndex: "status",
      key: "status",
      render: (v: string) => statusTag(v),
    },
    { title: t.startedAt, dataIndex: "started_at", key: "started_at" },
    {
      title: t.duration,
      dataIndex: "duration_ms",
      key: "duration_ms",
      render: (v: number) => fmtMs(v),
    },
    { title: t.llmCalls, dataIndex: "llm_call_count", key: "llm_call_count" },
    { title: t.toolCalls, dataIndex: "tool_call_count", key: "tool_call_count" },
  ];

  const maxEventCount = Math.max(1, ...(data?.events?.events || []).map((e: any) => e.count));

  return (
    <div style={{ padding: 24, maxWidth: 1200, margin: "0 auto" }}>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Typography.Title level={3} style={{ margin: 0 }}>
            {t.title}
          </Typography.Title>
        </Col>
        <Col>
          <Segmented
            value={hours}
            onChange={(v: any) => setHours(v)}
            options={[
              { label: t.range24h, value: 24 },
              { label: t.range7d, value: 168 },
              { label: t.range30d, value: 720 },
            ]}
          />
        </Col>
      </Row>

      {loading && !data ? (
        <div style={{ textAlign: "center", padding: 80 }}>
          <Spin size="large" />
        </div>
      ) : failed && !data ? (
        <Empty description={t.loadFailed} style={{ padding: 80 }} />
      ) : (
        <>
          {ov && ov.total_runs === 0 && ov.total_user_events === 0 && (
            <Card style={cardStyle} styles={{ body: { textAlign: "center", padding: 40 } }}>
              <Empty description={t.noRuns} />
            </Card>
          )}
          <Row gutter={[16, 16]}>
            <Col xs={12} md={6}>
              <Card style={cardStyle}>
                <Statistic title={t.totalRuns} value={ov?.total_runs ?? 0} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card style={cardStyle}>
                <Statistic title={t.successRate} value={fmtPct(ov?.success_rate)} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card style={cardStyle}>
                <Statistic title={t.avgDuration} value={fmtMs(ov?.avg_duration_ms)} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card style={cardStyle}>
                <Statistic title={t.activeAgents} value={ov?.active_agents ?? 0} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card style={cardStyle}>
                <Statistic title={t.toolCalls} value={ov?.total_tool_calls ?? 0} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card style={cardStyle}>
                <Statistic title={t.toolErrorRate} value={fmtPct(ov?.tool_error_rate)} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card style={cardStyle}>
                <Statistic title={t.llmCalls} value={ov?.total_llm_calls ?? 0} />
              </Card>
            </Col>
            <Col xs={12} md={6}>
              <Card style={cardStyle}>
                <Statistic title={t.userEvents} value={ov?.total_user_events ?? 0} />
              </Card>
            </Col>
          </Row>

          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} lg={16}>
              <Card title={t.trend} style={cardStyle}>
                <TrendChart buckets={data?.timeseries?.buckets || []} isDark={isDark} t={t} />
              </Card>
            </Col>
            <Col xs={24} lg={8}>
              <Card title={t.llmStats} style={cardStyle}>
                <Statistic
                  title={t.avgTtft}
                  value={fmtMs(llm?.avg_ttft_ms)}
                  style={{ marginBottom: 16 }}
                />
                <Statistic title={t.avgDuration} value={fmtMs(llm?.avg_duration_ms)} />
                <div style={{ marginTop: 16 }}>
                  <Typography.Text type="secondary">
                    {t.errors}: {llm?.errors ?? 0} / {llm?.calls ?? 0}
                  </Typography.Text>
                  <Progress
                    percent={llm?.calls ? Math.round(((llm?.errors || 0) / llm.calls) * 100) : 0}
                    size="small"
                    status="exception"
                    showInfo={false}
                  />
                </div>
              </Card>
            </Col>
          </Row>

          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} lg={12}>
              <Card title={t.topTools} style={cardStyle}>
                <Table
                  size="small"
                  rowKey="tool_name"
                  columns={toolColumns}
                  dataSource={data?.tools?.tools || []}
                  pagination={false}
                  locale={{ emptyText: t.noData }}
                />
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title={t.agentStats} style={cardStyle}>
                <Table
                  size="small"
                  rowKey="agent_id"
                  columns={agentColumns}
                  dataSource={data?.agents?.agents || []}
                  pagination={false}
                  locale={{ emptyText: t.noData }}
                />
              </Card>
            </Col>
          </Row>

          <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
            <Col xs={24} lg={8}>
              <Card title={t.eventStats} style={cardStyle}>
                {(data?.events?.events || []).length === 0 ? (
                  <Empty description={t.noData} style={{ padding: 24 }} />
                ) : (
                  (data?.events?.events || []).map((e: any) => (
                    <div key={e.event_type} style={{ marginBottom: 12 }}>
                      <div style={{ display: "flex", justifyContent: "space-between" }}>
                        <span>{e.event_type}</span>
                        <span>{e.count}</span>
                      </div>
                      <Progress
                        percent={Math.round((e.count / maxEventCount) * 100)}
                        showInfo={false}
                        size="small"
                      />
                    </div>
                  ))
                )}
              </Card>
            </Col>
            <Col xs={24} lg={16}>
              <Card title={t.recentRuns} style={cardStyle}>
                <Table
                  size="small"
                  rowKey="run_id"
                  columns={runColumns}
                  dataSource={data?.recent?.runs || []}
                  pagination={{ pageSize: 8, showSizeChanger: false }}
                  locale={{ emptyText: t.noData }}
                />
              </Card>
            </Col>
          </Row>
        </>
      )}
    </div>
  );
}

window.QwenPaw.menu.add(pluginId, {
  id: "qwenpaw-ops-observer.dashboard",
  label: "Ops Observer",
  icon: React.createElement("span", { role: "img" }, "📊"),
  route: "qwenpaw-ops-observer.dashboard",
});

window.QwenPaw.route.add(pluginId, {
  id: "qwenpaw-ops-observer.dashboard",
  path: "/ops-observer",
  component: Dashboard,
});
