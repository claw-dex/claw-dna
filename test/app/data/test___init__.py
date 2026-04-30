"""Smoke tests for app.data public re-export surface."""

from __future__ import annotations


def test_state_loaders_resolve_to_callables():
    from app.data import (
        load_state,
        load_services,
        load_services_full,
        load_service_logs,
    )

    for fn in (load_state, load_services, load_services_full, load_service_logs):
        assert callable(fn)


def test_goal_and_cycle_loaders_resolve():
    from app.data import (
        load_goals,
        load_goal_stats,
        load_cycles,
        load_cycle_velocity,
        load_cycle_logs,
        load_cycle_log_content,
        load_balance,
        load_activity,
    )

    for fn in (
        load_goals,
        load_goal_stats,
        load_cycles,
        load_cycle_velocity,
        load_cycle_logs,
        load_cycle_log_content,
        load_balance,
        load_activity,
    ):
        assert callable(fn)


def test_message_journal_log_loaders_resolve():
    from app.data import (
        load_journal,
        load_inbox,
        load_inbox_history,
        load_outbox,
        load_outbox_history,
        load_history,
        load_logs,
        load_log_detail,
    )

    for fn in (
        load_journal,
        load_inbox,
        load_inbox_history,
        load_outbox,
        load_outbox_history,
        load_history,
        load_logs,
        load_log_detail,
    ):
        assert callable(fn)


def test_memory_workspace_system_loaders_resolve():
    from app.data import (
        load_memory_files,
        read_memory_file,
        load_workspace_files,
        read_workspace_file,
        load_errors,
        load_system_info,
        load_validate,
        load_plugins,
        load_scheduled_tasks,
    )

    for fn in (
        load_memory_files,
        read_memory_file,
        load_workspace_files,
        read_workspace_file,
        load_errors,
        load_system_info,
        load_validate,
        load_plugins,
        load_scheduled_tasks,
    ):
        assert callable(fn)


def test_script_search_suggest_resolve():
    from app.data import load_scripts, search, load_suggest

    for fn in (load_scripts, search, load_suggest):
        assert callable(fn)


def test_write_operations_resolve():
    from app.data import (
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

    for fn in (
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
    ):
        assert callable(fn)


def test_cache_clear_all_exposed():
    from app.data import _cache_clear_all

    assert callable(_cache_clear_all)
    # No exception expected
    _cache_clear_all()


def test_module_exposes_expected_names():
    import app.data as data_pkg

    # Spot-check key public surface
    expected = {
        "load_state",
        "load_goals",
        "load_inbox",
        "load_logs",
        "load_scripts",
        "search",
        "load_suggest",
        "queue_to_inbox",
        "save_portal_config",
        "_cache_clear_all",
    }
    missing = expected - set(dir(data_pkg))
    assert not missing, f"missing public names: {missing}"
