(function () {
  var QwenPaw = window.QwenPaw;
  if (!QwenPaw || !QwenPaw.host || !QwenPaw.registerRoutes) return;

  var host = QwenPaw.host;
  var React = host.React;
  var antd = host.antd;
  var h = React.createElement;
  var IDENTITY_KEY = "bank-runtime-console:identity";

  function readIdentity() {
    try {
      var value = JSON.parse(sessionStorage.getItem(IDENTITY_KEY) || "null");
      return value && value.user_id && value.org_id ? value : null;
    } catch (_error) {
      return null;
    }
  }

  function apiFetch(path, options, identity) {
    options = options || {};
    var headers = { "Content-Type": "application/json" };
    var hostAccess = host.getApiToken ? host.getApiToken() : "";
    if (hostAccess) headers.Authorization = "Bearer " + hostAccess;
    if (identity) {
      headers["X-Runtime-External-User-Id"] = identity.user_id;
      headers["X-Runtime-External-Org-Id"] = identity.org_id;
    }
    return fetch(host.getApiUrl("/bank-runtime-console" + path), {
      method: options.method || "GET",
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function RuntimeConsole() {
    var identityState = React.useState(readIdentity());
    var identity = identityState[0];
    var setIdentity = identityState[1];
    var userState = React.useState(identity ? identity.user_id : "");
    var userId = userState[0];
    var setUserId = userState[1];
    var orgState = React.useState(identity ? identity.org_id : "");
    var orgId = orgState[0];
    var setOrgId = orgState[1];
    var listState = React.useState([]);
    var conversations = listState[0];
    var setConversations = listState[1];
    var detailState = React.useState(null);
    var detail = detailState[0];
    var setDetail = detailState[1];
    var loadingState = React.useState(false);
    var loading = loadingState[0];
    var setLoading = loadingState[1];

    function loadList(activeIdentity) {
      setLoading(true);
      return apiFetch("/conversations?page=1&page_size=100", { method: "GET" }, activeIdentity)
        .then(function (payload) {
          var data = payload.data || payload;
          setConversations(Array.isArray(data.items) ? data.items : []);
        })
        .finally(function () { setLoading(false); });
    }

    React.useEffect(function () {
      if (identity) loadList(identity).catch(function () { antd.message.error("无法读取 Runtime 会话"); });
    }, []);

    function connect() {
      var next = { user_id: userId.trim(), org_id: orgId.trim() };
      if (!next.user_id || !next.org_id) return;
      setLoading(true);
      apiFetch("/connect", { method: "POST", body: next }, null)
        .then(function () {
          sessionStorage.setItem(IDENTITY_KEY, JSON.stringify(next));
          setIdentity(next);
          setDetail(null);
          return loadList(next);
        })
        .catch(function () { antd.message.error("连接 Runtime 失败"); })
        .finally(function () { setLoading(false); });
    }

    function disconnect() {
      sessionStorage.removeItem(IDENTITY_KEY);
      setIdentity(null);
      setConversations([]);
      setDetail(null);
    }

    function openConversation(item) {
      apiFetch(
        "/conversations/" + encodeURIComponent(item.conversation_id),
        { method: "GET" },
        identity,
      )
        .then(function (payload) { setDetail(payload.data || payload); })
        .catch(function () { antd.message.error("当前身份无权读取该会话"); });
    }

    if (!identity) {
      return h("div", { style: styles.page },
        h("div", { style: styles.connectCard },
          h("div", { style: styles.eyebrow }, "LOCAL / DEV · READ ONLY"),
          h("h2", { style: styles.title }, "Runtime 托管会话检查器"),
          h("p", { style: styles.muted }, "输入外部用户与机构上下文。应用凭据固定在服务端，不会进入浏览器。"),
          h(antd.Input, { value: userId, placeholder: "外部用户 ID", onChange: function (e) { setUserId(e.target.value); }, style: styles.input }),
          h(antd.Input, { value: orgId, placeholder: "机构 ID", onChange: function (e) { setOrgId(e.target.value); }, style: styles.input }),
          h(antd.Button, { type: "primary", block: true, loading: loading, onClick: connect }, "建立只读连接"),
        ),
      );
    }

    return h("div", { style: styles.page },
      h("div", { style: styles.header },
        h("div", null,
          h("div", { style: styles.eyebrow }, "RUNTIME MANAGED · READ ONLY"),
          h("h2", { style: styles.title }, "托管会话"),
          h("div", { style: styles.muted }, identity.user_id + " · " + identity.org_id),
        ),
        h("div", null,
          h(antd.Button, { loading: loading, onClick: function () { loadList(identity); }, style: { marginRight: 8 } }, "刷新"),
          h(antd.Button, { onClick: disconnect }, "断开本标签页"),
        ),
      ),
      h("div", { style: styles.content },
        h("div", { style: styles.list },
          conversations.length ? conversations.map(function (item) {
            return h("button", { key: item.conversation_id, onClick: function () { openConversation(item); }, style: styles.row },
              h("strong", null, item.title || item.conversation_id),
              h("span", { style: styles.muted }, item.updated_at || item.status || ""),
            );
          }) : h(antd.Empty, { description: "当前身份暂无可见会话" }),
        ),
        h("div", { style: styles.detail }, detail
          ? h("pre", { style: styles.pre }, JSON.stringify(detail, null, 2))
          : h(antd.Empty, { description: "选择会话查看脱敏详情" })),
      ),
    );
  }

  var styles = {
    page: { height: "100%", padding: 24, background: "#f4f1ea", color: "#24231f", overflow: "auto" },
    connectCard: { width: 440, margin: "10vh auto", padding: 32, background: "#fffdf8", border: "1px solid #ded8cc", borderRadius: 12 },
    eyebrow: { color: "#8a6a32", fontSize: 11, fontWeight: 700, letterSpacing: "0.14em" },
    title: { margin: "6px 0 8px", fontFamily: "Georgia, serif", fontWeight: 500 },
    muted: { color: "#777268", fontSize: 13 },
    input: { marginBottom: 12 },
    header: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 },
    content: { display: "grid", gridTemplateColumns: "minmax(280px, 36%) 1fr", gap: 16, minHeight: 520 },
    list: { background: "#fffdf8", border: "1px solid #ded8cc", borderRadius: 10, padding: 10 },
    detail: { background: "#fffdf8", border: "1px solid #ded8cc", borderRadius: 10, padding: 18, overflow: "auto" },
    row: { width: "100%", display: "flex", flexDirection: "column", gap: 5, textAlign: "left", padding: "12px 10px", border: 0, borderBottom: "1px solid #eee8dc", background: "transparent", cursor: "pointer" },
    pre: { whiteSpace: "pre-wrap", wordBreak: "break-word", margin: 0, fontSize: 12, lineHeight: 1.6 },
  };

  QwenPaw.registerRoutes("bank-runtime-console", [{
    path: "/apps/bank-runtime-console",
    component: RuntimeConsole,
    label: "Bank Runtime Console",
    icon: "🔎",
  }]);
})();
