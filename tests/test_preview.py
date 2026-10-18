"""Native consumer + private preview transport; drafts never replace durable finals."""
import asyncio
from dataclasses import replace
import re

import pytest

from test_contracts import adapter, event, credentials, identity, no_sleep  # noqa: F401
from test_media_feedback import rich  # noqa: F401
from test_threads import threaded, reply, ROOT_A, ROOT_B, drained  # noqa: F401


@pytest.fixture
def preview(threaded):
    adp, api, runner = threaded
    adp.settings = replace(adp.settings, preview_enabled=True)
    adp.config.extra['preview_enabled'] = True
    return adp, api, runner


async def wait_for(predicate):
    for _ in range(400):
        if predicate():
            return
        await asyncio.sleep(.01)
    pytest.fail('Preview consumer did not reach expected state')


def consumer(adp, *, root=None, current=lambda: True):
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    return GatewayStreamConsumer(adp, 'dm',
        StreamConsumerConfig(transport='auto', edit_interval=.01, buffer_threshold=1, chat_type='dm'),
        metadata={'thread_id': root} if root else None, run_still_current=current)


async def started(adp, raw=None):
    inbound = adp.normalize(raw or event())
    await adp.on_processing_start(inbound)
    feedback = adp._feedback[inbound.message_id]
    await feedback.task
    # Advance only the visual's monotonic budget; no timing-sensitive wall clock test.
    feedback.updated = 0
    return inbound, feedback


async def complete(adp, inbound):
    from gateway.platforms.base import ProcessingOutcome
    await adp.on_processing_complete(inbound, ProcessingOutcome.SUCCESS)


async def test_real_consumer_single_cumulative_preview_and_authoritative_final(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    sc = consumer(adp)
    task = asyncio.create_task(sc.run())
    sc.on_delta('Vou verificar o farol azul. ')
    await wait_for(lambda: 'farol azul' in feedback.text)
    assert not api.deliveries
    sc.on_delta(None)  # Tool boundary must not create a permanent preamble.
    await asyncio.sleep(.1)
    feedback.updated = 0
    sc.on_delta('A ferramenta terminou. ')
    await wait_for(lambda: 'ferramenta terminou' in feedback.text)
    assert len(api.visuals) == 1 and not api.deliveries
    assert not sc.already_sent and not sc.final_content_delivered
    assert not sc.has_durably_delivered_text('Vou verificar o farol azul.')
    final = 'Resposta final completa, com rodapé do verificador.'
    sc.finish(final)
    await task
    assert sc.delivered_final_matches(final)
    assert [body['text'] for body, _ in api.deliveries.values()] == [final]
    await complete(adp, inbound)
    assert not api.visuals and not adp.store.unfinished_visuals()
    await adp.disconnect()


async def test_preview_throttled_bounded_and_status_does_not_overwrite_it(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    await adp.send_draft('dm', 1, 'Primeira parte')
    count = len(api.requests)
    await adp.send_draft('dm', 1, 'Texto novo ' * 3000)
    await adp.send_typing('dm')
    assert len(api.requests) == count
    feedback.updated = 0
    await adp.send_typing('dm')
    visual = next(iter(api.visuals.values()))
    assert visual['visibleToUserId'] == 'human' and visual['expiresInSeconds'] == 120
    assert visual['text'].startswith('✍️ Prévia\n\nTexto novo')
    assert len(visual['text']) < 13000 and '…' in visual['text']
    assert not api.deliveries
    await complete(adp, inbound)
    await adp.disconnect()


@pytest.mark.parametrize('failure', [404, 403, 429, 500])
async def test_visual_failure_does_not_publish_partial_or_lose_final(preview, failure):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    api.visual_failure = failure
    sc = consumer(adp)
    task = asyncio.create_task(sc.run())
    sc.on_delta('Prévia descartável')
    await wait_for(lambda: feedback.failed)
    assert not api.deliveries
    assert not sc.final_content_delivered
    sc.finish('Resposta definitiva')
    await task
    assert [body['text'] for body, _ in api.deliveries.values()] == ['Resposta definitiva']
    api.visual_failure = None
    await complete(adp, inbound)
    assert not api.visuals
    await adp.disconnect()


async def test_expired_preview_is_not_recreated(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    api.visuals.clear()
    await adp.send_draft('dm', 1, 'Novo trecho')
    assert feedback.failed
    feedback.updated = 0
    await adp.send_draft('dm', 1, 'Outro trecho')
    assert not api.visuals
    assert (await adp.send('dm', 'Final', metadata={'notify': True})).success
    await complete(adp, inbound)
    await adp.disconnect()


@pytest.mark.parametrize('case', ['different_dm', 'different_root', 'group', 'allowlist_changed', 'disabled', 'ended'])
async def test_preview_gate_fails_closed_before_network(preview, case):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    kwargs = {'chat_type': 'dm', 'chat_id': 'dm', 'metadata': None}
    if case == 'different_dm':kwargs['chat_id'] = 'other'
    if case == 'different_root':kwargs['metadata'] = {'thread_id': ROOT_B}
    if case == 'group':kwargs['chat_type'] = 'group'
    if case == 'allowlist_changed':adp.config.extra['allow_from'] = ['other']
    if case == 'disabled':adp.settings = replace(adp.settings, preview_enabled=False)
    if case == 'ended':await complete(adp, inbound)
    count = len(api.requests)
    assert not adp.supports_draft_streaming(**kwargs)
    if case != 'group':
        assert not (await adp.send_draft(kwargs['chat_id'], 1, 'Denied', kwargs['metadata'])).success
    assert len(api.requests) == count
    await adp.disconnect()


async def test_native_parallel_threads_preview_and_final_keep_own_destinations(preview):
    adp, api, _ = preview
    entered = asyncio.Event()
    count = 0
    async def handler(inbound):
        nonlocal count
        root = inbound.source.thread_id
        feedback = adp._feedback[inbound.message_id]
        await feedback.task
        feedback.updated = 0
        sc = consumer(adp, root=root)
        task = asyncio.create_task(sc.run())
        sc.on_delta('farol azul' if root == ROOT_A else 'ponte vermelha')
        await wait_for(lambda: feedback.preview)
        count += 1
        if count == 2:entered.set()
        await entered.wait()
        assert {v.get('parentId') for v in api.visuals.values()} == {ROOT_A, ROOT_B}
        sc.finish('Resposta ' + root)
        await task
        return None
    adp.set_message_handler(handler)
    await adp.ingest('a', reply())
    await adp.ingest('b', reply(ROOT_B, 'reply-b', 'event-b'))
    await drained(adp, 2)
    assert {body.get('parentId') for body, _ in api.deliveries.values()} == {ROOT_A, ROOT_B}
    assert not api.visuals
    await adp.disconnect()


async def test_stale_consumer_cleans_preview_without_sending_partial(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    live = [True]
    sc = consumer(adp, current=lambda: live[0])
    task = asyncio.create_task(sc.run())
    sc.on_delta('Texto ainda incompleto')
    await wait_for(lambda: feedback.preview)
    live[0] = False
    await task
    assert not api.visuals and not api.deliveries
    await complete(adp, inbound)
    await adp.disconnect()


async def test_long_final_split_only_after_completion(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    sc = consumer(adp)
    task = asyncio.create_task(sc.run())
    text = 'x' * (adp.MAX_MESSAGE_LENGTH + 1000)
    sc.on_delta(text)
    await wait_for(lambda: feedback.preview)
    assert not api.deliveries
    sc.finish(text)
    await task
    # Native long-message splitting adds ordinal suffixes; none of the answer is lost.
    assert ''.join(re.sub(r' \(\d+/\d+\)$', '', body['text']) for body, _ in api.deliveries.values()) == text
    assert all(len(body['text']) <= adp.MAX_MESSAGE_LENGTH for body, _ in api.deliveries.values())
    await complete(adp, inbound)
    await adp.disconnect()


async def test_ambiguous_final_retry_and_gateway_fallback_reuse_receipt(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    # Server accepted all four attempts, but every response was lost.
    api.failures = ['timeout'] * 4
    sc = consumer(adp)
    task = asyncio.create_task(sc.run())
    sc.on_delta('Trecho inicial')
    await wait_for(lambda: feedback.preview)
    sc.finish('Final único')
    await task
    # The real native outer fallback has notify=True as well.
    assert (await adp.send('dm', 'Final único', metadata={'notify': True})).success
    assert len(api.deliveries) == 1
    posts = [r for r in api.requests if r.url.path.endswith('/messages')]
    assert len({r.headers['Idempotency-Key'] for r in posts}) == 1
    await complete(adp, inbound)
    await adp.disconnect()


async def test_edit_fallback_never_creates_permanent_partial(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    sc = GatewayStreamConsumer(adp, 'dm', StreamConsumerConfig(transport='edit', edit_interval=.01, buffer_threshold=1))
    task = asyncio.create_task(sc.run())
    sc.on_delta('Parte transitória')
    await asyncio.sleep(.15)
    assert not api.deliveries
    sc.finish('Resposta inteira')
    await task
    assert [body['text'] for body, _ in api.deliveries.values()] == ['Resposta inteira']
    await complete(adp, inbound)
    await adp.disconnect()


async def test_media_and_reasoning_markers_not_in_preview(preview, tmp_path):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    path = tmp_path / 'neutral.txt';path.write_text('neutral file contents')
    sc = consumer(adp)
    task = asyncio.create_task(sc.run())
    sc.on_delta('<think>Private reasoning</think>Texto público\nMEDIA:' + str(path))
    await wait_for(lambda: feedback.preview)
    assert 'Private reasoning' not in feedback.text and 'MEDIA:' not in feedback.text
    sc.finish('Texto público\nMEDIA:' + str(path))
    await task
    assert sc.delivered_final_matches('Texto público')
    assert (await adp.send_document('dm', str(path))).success
    assert sum(bool(body.get('attachments')) for body, _ in api.deliveries.values()) == 1
    await complete(adp, inbound)
    await adp.disconnect()


async def test_flag_defaults_off_and_rejects_non_boolean(adapter):
    adp, _, _ = adapter
    cls = type(adp.settings)
    assert not cls.from_extra(adp.config.extra).preview_enabled
    for invalid in ('true', 1, None):
        with pytest.raises(ValueError):cls.from_extra({**adp.config.extra, 'preview_enabled': invalid})


async def test_native_turn_factory_propagates_config_and_worker_deltas(preview):
    from types import SimpleNamespace
    from gateway.config import StreamingConfig
    from gateway.display_config import resolve_display_setting
    from gateway.run_turn_runner import TurnRunner
    adp, api, runner = preview
    inbound, feedback = await started(adp)
    runner.config.streaming = StreamingConfig(enabled=True, transport='auto')
    ctx = SimpleNamespace(mute_notification_reply=False, streaming_tts_consumer_holder=[None],
        user_config={'display': {'platforms': {'sowork': {'streaming': True}}}},
        source=inbound.source, scheduled_heartbeat=False, interim_assistant_messages_enabled=False,
        resolve_display_setting=resolve_display_setting, _status_thread_metadata=None,
        progress_queue=None, event_message_id=inbound.message_id, _run_still_current=lambda: True,
        stream_consumer_holder=[None])
    worker = TurnRunner(runner, ctx)
    sc, delta, _, interim = worker._setup_stream_consumer('sowork')
    assert sc is not None and callable(delta) and not interim
    task = asyncio.create_task(sc.run())
    await asyncio.to_thread(delta, 'Texto do worker nativo com farol azul')
    await wait_for(lambda: feedback.preview)
    assert not api.deliveries
    sc.finish('Resposta pelo consumidor nativo')
    await task
    assert sc.delivered_final_matches('Resposta pelo consumidor nativo')
    await complete(adp, inbound)
    await adp.disconnect()


async def test_cancelled_consumer_cleans_only_its_preview(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    sc = consumer(adp)
    task = asyncio.create_task(sc.run())
    sc.on_delta('Resposta ainda em andamento')
    await wait_for(lambda: feedback.preview)
    task.cancel()
    await task
    assert not api.visuals and not api.deliveries
    await complete(adp, inbound)
    await adp.disconnect()


async def test_visual_timeout_does_not_hold_completed_response(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    async def slow_update(*args):
        await asyncio.Event().wait()
    adp.client.update_progress = slow_update
    sc = consumer(adp)
    task = asyncio.create_task(sc.run())
    sc.on_delta('Prévia com rede lenta')
    await wait_for(lambda: feedback.preview)
    sc.finish('Final apesar da rede lenta')
    await asyncio.wait_for(task, 10)
    assert feedback.failed
    assert sc.delivered_final_matches('Final apesar da rede lenta')
    assert [body['text'] for body, _ in api.deliveries.values()] == ['Final apesar da rede lenta']
    await complete(adp, inbound)
    await adp.disconnect()


async def test_silence_marker_not_sent_or_left_as_preview(preview):
    adp, api, _ = preview
    inbound, feedback = await started(adp)
    sc = consumer(adp)
    task = asyncio.create_task(sc.run())
    sc.on_delta('NO_RE')
    sc.finish('NO_REPLY')
    await task
    assert not api.deliveries
    assert all('NO_RE' not in v['text'] for v in api.visuals.values())
    await complete(adp, inbound)
    assert not api.visuals
    await adp.disconnect()
