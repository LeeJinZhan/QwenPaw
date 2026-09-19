#!/bin/sh
set -eu

: "${QWENPAW_SERVICE_TOKEN:?service_token_missing}"
: "${QWENPAW_RUNTIME_BASE_URL:?runtime_base_url_missing}"
: "${QWENPAW_TOOL_GATEWAY_BASE_URL:?tool_gateway_base_url_missing}"
: "${QWENPAW_SANDBOX_BROKER_BASE_URL:?sandbox_broker_base_url_missing}"

export PYTHONPATH="/opt/bank-runtime-plugin${PYTHONPATH:+:${PYTHONPATH}}"

working_dir="${QWENPAW_WORKING_DIR:-/app/working}"
secret_dir="${QWENPAW_SECRET_DIR:-/app/working.secret}"
backup_dir="${QWENPAW_BACKUP_DIR:-/app/working.backups}"
task_file_dir="${QWENPAW_TASK_FILE_ROOT:-/app/task-files}"
plugins_dir="${working_dir}/plugins"
root_config="${working_dir}/config.json"
agent_config="${QWENPAW_RUNTIME_AGENT_CONFIG:-${working_dir}/workspaces/bank-assistant/agent.json}"

for required_dir in "$working_dir" "$secret_dir" "$backup_dir" "$task_file_dir"; do
    if [ ! -d "$required_dir" ] || [ ! -w "$required_dir" ]; then
        echo "production_directory_not_writable" >&2
        exit 1
    fi
done

mkdir -p "$plugins_dir"
for plugin_id in bank-runtime bank-mineru-mcp; do
    plugin_dir="${plugins_dir}/${plugin_id}"
    plugin_source="/opt/${plugin_id}-plugin"
    if [ -e "$plugin_dir" ] && [ ! -L "$plugin_dir" ]; then
        echo "bank_plugin_not_immutable" >&2
        exit 1
    fi
    if [ ! -L "$plugin_dir" ]; then
        ln -s "$plugin_source" "$plugin_dir"
    fi
    if [ "$(readlink "$plugin_dir")" != "$plugin_source" ]; then
        echo "bank_plugin_source_mismatch" >&2
        exit 1
    fi
done

if [ ! -f "$root_config" ] || [ ! -f "$agent_config" ]; then
    echo "production_config_missing" >&2
    exit 1
fi

case "${QWENPAW_AUTH_ENABLED:-}" in
    1|true|TRUE|yes|YES) ;;
    *) echo "native_auth_required" >&2; exit 1;;
esac
if [ -n "${QWENPAW_AUTH_PASSWORD:-}" ]; then
    echo "native_auth_password_environment_forbidden" >&2
    exit 1
fi
python -m bank_runtime.admin_bootstrap verify --secret-dir "$secret_dir"

python -m bank_runtime.delivery_probe \
    --root-config "$root_config" \
    --agent-config "$agent_config"

exec qwenpaw app --host 0.0.0.0 --port "${QWENPAW_PORT:-8088}"
