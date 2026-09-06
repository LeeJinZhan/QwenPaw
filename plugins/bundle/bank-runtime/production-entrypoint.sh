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
bank_plugin_dir="${plugins_dir}/bank-runtime"
root_config="${working_dir}/config.json"
agent_config="${QWENPAW_RUNTIME_AGENT_CONFIG:-${working_dir}/workspaces/bank-assistant/agent.json}"

for required_dir in "$working_dir" "$secret_dir" "$backup_dir" "$task_file_dir"; do
    if [ ! -d "$required_dir" ] || [ ! -w "$required_dir" ]; then
        echo "production_directory_not_writable" >&2
        exit 1
    fi
done

mkdir -p "$plugins_dir"
if [ -e "$bank_plugin_dir" ] && [ ! -L "$bank_plugin_dir" ]; then
    echo "bank_runtime_plugin_not_immutable" >&2
    exit 1
fi
if [ ! -L "$bank_plugin_dir" ]; then
    ln -s /opt/bank-runtime-plugin "$bank_plugin_dir"
fi
if [ "$(readlink "$bank_plugin_dir")" != "/opt/bank-runtime-plugin" ]; then
    echo "bank_runtime_plugin_source_mismatch" >&2
    exit 1
fi

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
