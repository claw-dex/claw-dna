"""Data loading layer — re-exports all public functions for backward compatibility.

All existing ``from app.data import load_state`` imports continue to work unchanged.
"""

# Cache infrastructure (used by server.py and tab modules)
from app.data._cache import _cache_clear_all  # noqa: F401

# State
from app.data.state import (
    load_state,
    load_services,
    load_services_full,
    load_service_logs,
)  # noqa: F401

# Goals
from app.data.goal import load_goals, load_goal_stats  # noqa: F401

# Cycles
from app.data.cycle import (  # noqa: F401
    load_cycles,
    load_cycle_velocity,
    load_cycle_logs,
    load_cycle_log_content,
    load_balance,
    load_activity,
)

# Journal
from app.data.journal import load_journal  # noqa: F401

# Messages
from app.data.message import (  # noqa: F401
    load_inbox,
    load_inbox_history,
    load_outbox,
    load_outbox_history,
)

# Logs
from app.data.log import load_logs, load_log_detail  # noqa: F401

# Memory files
from app.data.memory import (  # noqa: F401
    load_ltm_size,
    load_memory_files,
    read_memory_file,
)

# Workspace files
from app.data.workspace import load_workspace_files, read_workspace_file  # noqa: F401

# System
from app.data.system import (  # noqa: F401
    load_errors,
    load_system_info,
    load_validate,
    load_plugins,
    load_scheduled_tasks,
)

# Scripts
from app.data.script import load_scripts  # noqa: F401

# Suggestions
from app.data.suggest import load_suggest  # noqa: F401

# Pre-computed metrics (DuckDB store — see scripts/metrics_db.py)
from app.data.metrics import (  # noqa: F401
    load_health,
    load_day_glance,
    load_suggestions,
    load_balance_metrics,
    load_goal_metrics,
    load_velocity_metrics,
    load_improvements,
    load_cycle_velocity_metric,
    load_workspace_mb,
    load_memory_overview,
    load_memory_file_health,
    load_agent_error_metrics,
)

# Write operations
from app.data.write import (  # noqa: F401
    queue_to_inbox,
    run_script,
    write_first_goal,
    trigger_bootstrap_heartbeat,
    remove_service,
    stop_service,
    start_service,
    update_goal_status,
    archive_goals,
    is_archivable_goal,
    delete_inbox_item,
    clear_outbox,
    create_scheduled_task,
    update_scheduled_task,
    delete_scheduled_task,
    save_portal_config,
)
