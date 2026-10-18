# Proposed generic Hermes delivery-context contract

Consumer: this standalone platform plugin must correlate a cron result with its
executor-owned occurrence before writing to its durable outbox. The existing
public `PluginContext.current_cron_execution()` covers direct executions; it
returns None during the pinned core's restart-safe worker queue drain.

## Minimal upstream change

Use the existing `cron.execution_identity` enter/exit functions around each
claimed row's `send()` in `cron.delivery_queue.drain()`. Bind only after claiming
ownership; reset in finally on success, exception and cancellation. Keep the
same context through the scheduler's existing copy_context()/asyncio handoff to
the live adapter. Do not accept execution IDs from message metadata, tool args or
provider events, and do not add a SoWork-specific branch or model tool.

The public getter should return the job ID, occurrence ID and owning profile of
that row during delivery and None outside it. Preserve its delegated-child
isolation. No new public API is needed if the existing getter's contract is
extended to delivery.

## Upstream acceptance tests

- Enqueue a real row, drain it, and observe its exact identity through a real
  PluginContext getter in the sender and live adapter coroutine.
- Two concurrent drains/profiles never observe each other's job or occurrence.
- Exceptions and cancellation clear the context; no identity remains afterward.
- A direct run and delivery retry for the same occurrence produce the same
  immutable outbox operation, while different occurrences remain distinct.
- A model-supplied job_id/execution_id does not establish this context.

## Migration here

The plugin already prefers the public getter. Once the pinned stable/main
revisions supply it during queue delivery, remove queued_execution(), its private
ownership/target-resolution imports and the corresponding startup probes. Keep
the real queue and full gateway regression tests. Until then the checked legacy
fallback preserves current proactive delivery without patching the host or
claiming an unsupported public contract. This proposal is prepared here; it has
not been merged into Hermes upstream.
