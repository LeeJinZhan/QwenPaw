"""Bank-only familiar tool signatures with no host-side implementation."""
from types import MethodType

from agentscope.permission import PermissionBehavior, PermissionDecision
from qwenpaw.runtime.tool_guard import GuardedFunctionTool

NATIVE_TOOL_NAMES = frozenset({'execute_shell_command', 'read_file', 'write_file', 'edit_file',
                             'append_file', 'glob_search', 'grep_search'})


async def _intercept_only():
    # GatewayMiddleware consumes the permit and executes through Runtime before
    # it can reach this body. Calling the body directly is always fail-closed.
    raise RuntimeError('Bank native tools require the Runtime Gateway middleware')


async def execute_shell_command(command: str, timeout: float = 300.0, cwd: str | None = None):
    """Run a command in the current task container. Returns real exit status, stdout and stderr.

    Use supplied authorized input paths and installed libraries directly. Combine related
    sheet inspection and requested calculations; loop over data batches inside the script.
    A heredoc can create and execute a scratch .py file in one call, avoiding nested quoting.
    Each call is a new process; scratch files persist only within this task, not Python variables.
    Reuse compatible task-local intermediate results; correct actual errors before retrying.
    stdout can be plain text: JSON serialization is optional. Return requested results and
    necessary samples rather than dumping the full input. Output limits and truncation are real.
    """
    return await _intercept_only()


async def read_file(file_path: str, start_line: int | str | None = None, end_line: int | str | None = None):
    """Read a bounded inclusive line range in the current task workspace."""
    return await _intercept_only()


async def write_file(file_path: str, content: str):
    """Write a task scratch script or output file. Input and public files are read-only."""
    return await _intercept_only()


async def edit_file(file_path: str, old_text: str, new_text: str):
    """Replace all exact occurrences in a scratch or output file."""
    return await _intercept_only()


async def append_file(file_path: str, content: str):
    """Append text to a scratch or output file in the current task."""
    return await _intercept_only()


async def glob_search(pattern: str, path: str | None = None):
    """Find files by glob under a task workspace directory (default scratch)."""
    return await _intercept_only()


async def grep_search(pattern: str, path: str | None = None, is_regex: bool = False,
                      case_sensitive: bool = True, context_lines: int = 0,
                      include_pattern: str | None = None, show_file: bool = True):
    """Search text by literal or regex, with case, include and context controls."""
    return await _intercept_only()


def native_function_tools(*, agent_id, approval_level, request_context, guard_paths=None, active_scope=None):
    if not agent_id or approval_level not in {'auto', 'smart', 'strict'}:
        raise RuntimeError('Bank native tools require an enabled trusted Tool Guard profile')
    tools = []
    for function in (execute_shell_command, read_file, write_file, edit_file, append_file, glob_search, grep_search):
        tool = GuardedFunctionTool(function, agent_id=agent_id,
            request_context={**request_context, 'approval_level': approval_level}, is_read_only=False)
        original_check = tool.check_permissions
        async def fail_closed_check(self, input_data=None, context=None, _check=original_check, **kwargs):
            from qwenpaw.security.tool_guard.engine import get_guard_engine
            level = self._resolve_execution_level()
            if level not in {'auto', 'smart', 'strict'} or not get_guard_engine().enabled:
                return PermissionDecision(behavior=PermissionBehavior.DENY,
                                          message='Bank native Tool Guard is unavailable or disabled')
            from qwenpaw.security.tool_guard.virtual_paths import ContainerGuardPaths, container_path_view
            if not isinstance(guard_paths,ContainerGuardPaths) or active_scope is None or not active_scope():
                return PermissionDecision(behavior=PermissionBehavior.DENY,message='Trusted container Guard scope is unavailable')
            guard_input = dict(input_data or {})
            try:
                if self.name == 'execute_shell_command':
                    paths = guard_paths.for_cwd(guard_input.get('cwd'))
                    if guard_input.get('cwd') is not None:
                        guard_input['cwd'] = paths.cwd
                else:
                    paths = guard_paths.for_file_arguments()
                    for key in ('file_path', 'path'):
                        if isinstance(guard_input.get(key), str):
                            guard_input[key] = str(paths.argument_path(guard_input[key]))
            except (TypeError,ValueError):
                return PermissionDecision(behavior=PermissionBehavior.DENY,message='Container working directory is outside the task scope')
            with container_path_view(paths):
                return await _check(guard_input, context, **kwargs)
        tool.check_permissions = MethodType(fail_closed_check, tool)
        tool._qp_bank_native_tool = True
        tools.append(tool)
    return tools
