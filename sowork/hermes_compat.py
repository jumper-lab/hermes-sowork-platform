"""Narrow, checked compatibility boundary for seams not yet exposed by Hermes.

Normal cron identity uses the public PluginContext API. The legacy queue drain
in the two supported Hermes revisions omits that context; retain its ownership
fence here until Hermes binds it during delivery. Never patch the host.
"""
import inspect
import os
from contextvars import ContextVar


def _caption_helpers():
    try:
        from gateway.outbound.senders import _media_caption_split, _DEFAULT_CAPTION_LIMIT
    except ModuleNotFoundError as error:
        if error.name not in {"gateway.outbound", "gateway.outbound.senders"}:
            raise
        from tools.send_message_tool import _media_caption_split, _DEFAULT_CAPTION_LIMIT
    return _media_caption_split, _DEFAULT_CAPTION_LIMIT


def missing_delivery_contracts():
    from cron import delivery_queue
    from cron import scheduler_delivery
    from tools import approval_context
    missing = []
    for module, name, args, kwargs in (
        (delivery_queue, "queue_path", (), {}),
        (delivery_queue, "get_status", ("probe",), {}),
        (scheduler_delivery, "_resolve_delivery_targets", ({},), {"for_failure": False}),
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
    if not isinstance(getattr(approval_context, "_approval_tool_call_id", None), ContextVar):
        missing.append("tools.approval_context._approval_tool_call_id")
    try:
        splitter, limit = _caption_helpers()
        inspect.signature(splitter).bind("", [], max_caption_len=limit)
        if type(limit) is not int or limit <= 0:
            raise ValueError()
    except (ImportError, AttributeError, TypeError, ValueError):
        missing.append("native outbound caption helpers")
    return missing


def tool_call_id():
    # This is the executor-owned ContextVar used by the old browser wrapper too.
    # Reading it directly preserves identity when that unrelated wrapper is gone.
    from tools.approval_context import _approval_tool_call_id
    value = _approval_tool_call_id.get()
    if value is not None and not isinstance(value, str):
        raise ValueError("Invalid native tool execution identity")
    return value or ""


def split_caption(message, media_files):
    splitter, limit = _caption_helpers()
    return splitter(message, media_files, max_caption_len=limit)


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

