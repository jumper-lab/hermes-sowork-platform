"""Narrow, checked compatibility boundary for seams not yet exposed by Hermes.

Normal cron identity uses the public PluginContext API. The legacy queue drain
in the two supported Hermes revisions omits that context; retain its ownership
fence here until Hermes binds it during delivery. Never patch the host.
"""
import inspect
import os


def missing_delivery_contracts():
    from cron import delivery_queue
    from cron import scheduler_delivery
    from tools import browser_extension_router
    missing = []
    for module, name, args, kwargs in (
        (delivery_queue, "queue_path", (), {}),
        (delivery_queue, "get_status", ("probe",), {}),
        (scheduler_delivery, "_resolve_delivery_targets", ({},), {"for_failure": False}),
        (browser_extension_router, "current_tool_call_id", (), {}),
    ):
        try:
            inspect.signature(getattr(module, name)).bind(*args, **kwargs)
        except (AttributeError, TypeError, ValueError):
            missing.append(module.__name__ + "." + name)
    for name, expected in (("_ACTIVE_DELIVERIES", set), ("_PROCESS_ID", str)):
        if not isinstance(getattr(delivery_queue, name, None), expected):
            missing.append("cron.delivery_queue." + name)
    lock = getattr(delivery_queue, "_lock", None)
    if not callable(getattr(lock, "__enter__", None)):
        missing.append("cron.delivery_queue._lock")
    return missing


def tool_call_id():
    from tools.browser_extension_router import current_tool_call_id
    return current_tool_call_id()


def queued_execution(home, metadata):
    """Attest a restart-safe worker result claimed by THIS native gateway process.

    The native queue drain currently does not bind current_cron_execution(). Read its
    existing ownership fence instead; never accept a model-supplied job/execution ID.
    No queue state is changed here. Ambiguous/unowned/foreign-profile rows fail closed.
    """
    import json
    from cron import delivery_queue
    from cron.scheduler_delivery import _resolve_delivery_targets

    path = delivery_queue.queue_path()
    if path.resolve() != home / "cron" / "deliveries.db" or not path.is_file():
        return None
    with delivery_queue._lock:
        active = tuple(delivery_queue._ACTIVE_DELIVERIES)
    matches = []
    for execution_id in active:
        row = delivery_queue.get_status(execution_id)
        if (not row or row["status"] != "delivering" or row["owner_pid"] != os.getpid()
                or row["owner_process_id"] != delivery_queue._PROCESS_ID):
            continue
        job = json.loads(row["job_json"])
        if job.get("execution_id") != execution_id or ((metadata or {}).get("job_id") not in (None, job["id"])):
            continue
        targets = _resolve_delivery_targets(job, for_failure=bool(row["for_failure"]))
        matches.append((job["id"], execution_id, targets))
    return matches[0] if len(matches) == 1 else None

