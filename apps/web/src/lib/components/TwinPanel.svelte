<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import TwinCameraView from './TwinCameraView.svelte';
	import LiveVideoCard from './LiveVideoCard.svelte';
	import ArchiveVideoCard from './ArchiveVideoCard.svelte';
	import RecentDetectionsPanel from './RecentDetectionsPanel.svelte';
	import TimelineStrip from './TimelineStrip.svelte';
	import { fetchDetectionTimeline } from '$lib/api';
	import { loadRuntimeConfig, type RuntimeConfig } from '$lib/runtime-config';
	import {
		TIMELINE_SPAN_MS,
		formatClock,
		parseIsoMs,
		toIsoMillis,
		windowForCursor,
		type PlaybackWindow
	} from '$lib/timeline';
	import type { DetectionTimeline, TimelineEvent, TwinObjectEvidence } from '$lib/types';

	interface Props {
		/** Drive server WS base URL (the selected tunnel). */
		wsBaseUrl: string;
	}

	let { wsBaseUrl }: Props = $props();

	let config = $state<RuntimeConfig | null>(null);
	let selectedCamera = $state('ch1');
	let showAll = $state(false);

	// ── Replay control channel (shared world: one mode for all viewers) ──
	let mode = $state<'live' | 'replay'>('live');
	let replaySupported = $state(false);
	let replayClockMs = $state<number | null>(null);
	let controlError = $state<string | null>(null);
	let controlWs: WebSocket | null = null;
	let controlKey = '';
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let twinObjects = $state<TwinObjectEvidence[]>([]);
	let lastStatusRequestMs = 0;

	// ── Timeline (markers over the past 24h of recorded detections) ──
	let timeline = $state<DetectionTimeline | null>(null);
	let nowMs = $state(Date.now());
	let viewStartMs = $state(Date.now() - TIMELINE_SPAN_MS);
	let viewEndMs = $state(Date.now());
	let selectedObjectId = $state<string | null>(null);
	let clockTimer: ReturnType<typeof setInterval> | null = null;
	let refreshTimer: ReturnType<typeof setInterval> | null = null;

	// Right-pane archive playback window follows the replay clock.
	let playbackWindow = $state<PlaybackWindow | null>(null);
	let seekNonce = $state(0);

	let cameraIds = $derived(config?.videoCameraIds ?? ['ch1', 'ch2', 'ch3', 'ch4']);
	let cursorMs = $derived(mode === 'replay' && replayClockMs !== null ? replayClockMs : nowMs);
	let selectedTwinObject = $derived(
		selectedObjectId
			? (twinObjects.find((object) => object.object_id === selectedObjectId) ?? null)
			: null
	);
	let mappedActorCount = $derived(
		twinObjects.filter(
			(object) =>
				object.actor_present === true &&
				typeof object.actor_id === 'number' &&
				object.carla_transform !== null &&
				object.carla_transform !== undefined
		).length
	);

	function perceptionStreamUrl(cameraId: string): string {
		if (!config) return '';
		const explicitUrl = config.perceptionStreamUrls[cameraId];
		if (explicitUrl) return explicitUrl;
		if (!config.perceptionStreamBaseUrl) return '';
		const path = config.perceptionStreamPathTemplate.replace(
			'{camera_id}',
			encodeURIComponent(cameraId)
		);
		return `${config.perceptionStreamBaseUrl}${path.startsWith('/') ? path : `/${path}`}`;
	}

	function disconnectControl() {
		if (reconnectTimer) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
		if (controlWs) {
			controlWs.onclose = null;
			controlWs.close();
			controlWs = null;
		}
	}

	function connectControl() {
		disconnectControl();
		const base = wsBaseUrl.replace(/\/+$/, '');
		if (!base) return;
		try {
			controlWs = new WebSocket(`${base}/twin?control=1`);
		} catch {
			return;
		}
		controlWs.onmessage = (event) => {
			if (typeof event.data !== 'string') return;
			try {
				const msg = JSON.parse(event.data);
				if (msg.type === 'twin_hello' && msg.sync) {
					mode = msg.sync.mode === 'replay' ? 'replay' : 'live';
					replaySupported = Boolean(msg.sync.replay_supported);
					replayClockMs = parseIsoMs(msg.sync.replay_clock);
					twinObjects = Array.isArray(msg.sync.objects) ? msg.sync.objects : [];
				} else if (msg.type === 'twin_mode' || msg.type === 'twin_clock') {
					mode = msg.mode === 'replay' ? 'replay' : 'live';
					replaySupported = Boolean(msg.replay_supported ?? replaySupported);
					replayClockMs = parseIsoMs(msg.replay_clock);
					if (Array.isArray(msg.objects)) twinObjects = msg.objects;
					if (msg.type === 'twin_mode') controlError = null;
					if (msg.type === 'twin_clock' && msg.mode === 'replay') {
						const now = Date.now();
						if (now - lastStatusRequestMs >= 1000) {
							lastStatusRequestMs = now;
							controlWs?.send(JSON.stringify({ type: 'twin_status' }));
						}
					}
				} else if (msg.type === 'twin_error') {
					controlError = msg.message ?? 'Twin control error';
				}
			} catch {
				// ignore malformed frames
			}
		};
		controlWs.onclose = () => {
			reconnectTimer = setTimeout(connectControl, 3000);
		};
	}

	function sendControl(payload: Record<string, unknown>) {
		if (!controlWs || controlWs.readyState !== WebSocket.OPEN) {
			controlError = 'Twin control channel not connected';
			return;
		}
		controlError = null;
		controlWs.send(JSON.stringify(payload));
	}

	function replayAt(epochMs: number) {
		const clamped = Math.min(Math.max(epochMs, Date.now() - TIMELINE_SPAN_MS), Date.now() - 1000);
		sendControl({ type: 'twin_replay', start: toIsoMillis(clamped) });
		replayClockMs = clamped;
		updatePlaybackWindow(clamped, true);
	}

	function goLive() {
		sendControl({ type: 'twin_live' });
		selectedObjectId = null;
		twinObjects = [];
		playbackWindow = null;
	}

	function updatePlaybackWindow(epochMs: number, forceSeek = false) {
		const win = windowForCursor(epochMs, Date.now());
		if (!playbackWindow || win.start !== playbackWindow.start || win.end !== playbackWindow.end) {
			playbackWindow = win;
		}
		if (forceSeek) seekNonce += 1;
	}

	function handleScrub(epochMs: number) {
		if (Date.now() - epochMs < 20_000) {
			goLive();
			return;
		}
		replayAt(epochMs);
	}

	function handleSelectEvent(event: TimelineEvent) {
		selectedObjectId = event.object_id;
		if (event.media_time_trusted !== true || event.timestamp_schema_version !== 2) {
			controlError =
				'This event predates the trusted HLS media clock and cannot be used for twin correlation.';
			return;
		}
		const firstSeen = parseIsoMs(event.first_seen);
		if (firstSeen !== null) {
			replayAt(firstSeen - 5_000);
		}
		if (event.device_id) {
			const channel = event.device_id.split('-').pop();
			if (channel && cameraIds.includes(channel)) {
				selectedCamera = channel;
				showAll = false;
			}
		}
	}

	async function loadTimeline() {
		try {
			const end = Date.now();
			timeline = await fetchDetectionTimeline({
				start: toIsoMillis(end - TIMELINE_SPAN_MS),
				end: toIsoMillis(end),
				bucketSeconds: 60
			});
		} catch {
			// markers are best-effort; the strip still allows blind scrubbing
		}
	}

	// Keep the archive window tracking the advancing replay clock.
	$effect(() => {
		if (mode === 'replay' && replayClockMs !== null) {
			updatePlaybackWindow(replayClockMs);
		}
	});

	$effect(() => {
		const key = wsBaseUrl;
		if (key === controlKey) return;
		controlKey = key;
		connectControl();
	});

	onMount(async () => {
		config = await loadRuntimeConfig();
		void loadTimeline();
		clockTimer = setInterval(() => {
			nowMs = Date.now();
			if (mode === 'live') {
				viewEndMs = nowMs;
				viewStartMs = nowMs - TIMELINE_SPAN_MS;
			}
		}, 1000);
		refreshTimer = setInterval(() => void loadTimeline(), 60_000);
	});

	onDestroy(() => {
		disconnectControl();
		if (clockTimer) clearInterval(clockTimer);
		if (refreshTimer) clearInterval(refreshTimer);
	});
</script>

{#snippet realPane(cameraId: string)}
	{#if mode === 'replay' && playbackWindow}
		<ArchiveVideoCard
			{cameraId}
			windowStart={playbackWindow.start}
			windowEnd={playbackWindow.end}
			windowStartMs={playbackWindow.startMs}
			cursorMs={replayClockMs ?? playbackWindow.startMs}
			{seekNonce}
			playing={true}
		/>
	{:else}
		<LiveVideoCard
			{cameraId}
			streamUrl={perceptionStreamUrl(cameraId)}
			sourceLabel={perceptionStreamUrl(cameraId) ? 'Perception' : 'Raw'}
		/>
	{/if}
{/snippet}

<div class="flex h-full flex-col overflow-y-auto bg-gray-950">
	<div class="flex flex-wrap items-center gap-2 border-b border-gray-800 px-4 py-3">
		<span class="text-[11px] font-semibold tracking-[0.18em] text-gray-300 uppercase">
			Digital Twin
		</span>
		<button
			class={`border px-3 py-1 text-[11px] font-semibold tracking-[0.14em] uppercase transition ${
				mode === 'live'
					? 'border-emerald-400/60 bg-emerald-400/10 text-emerald-200'
					: 'border-gray-700 bg-gray-900 text-gray-300 hover:border-gray-500 hover:text-white'
			}`}
			onclick={goLive}
		>
			Live
		</button>
		{#if mode === 'replay'}
			<span class="border border-amber-400/60 bg-amber-400/10 px-3 py-1 font-mono text-[11px] text-amber-200">
				Replay · {replayClockMs !== null ? formatClock(replayClockMs) : '…'}
			</span>
			<span class="border border-gray-700 bg-gray-900 px-3 py-1 font-mono text-[11px] text-gray-300">
				{mappedActorCount} mapped actor{mappedActorCount === 1 ? '' : 's'}
			</span>
		{/if}
		<div class="ml-2 flex items-center gap-1">
			{#each cameraIds as cameraId}
				<button
					class={`border px-3 py-1 text-[11px] font-medium tracking-[0.14em] uppercase transition ${
						!showAll && selectedCamera === cameraId
							? 'border-cyan-400/60 bg-cyan-400/10 text-cyan-200'
							: 'border-gray-700 bg-gray-900 text-gray-300 hover:border-gray-500 hover:text-white'
					}`}
					onclick={() => {
						selectedCamera = cameraId;
						showAll = false;
					}}
				>
					{cameraId}
				</button>
			{/each}
			<button
				class={`border px-3 py-1 text-[11px] font-medium tracking-[0.14em] uppercase transition ${
					showAll
						? 'border-cyan-400/60 bg-cyan-400/10 text-cyan-200'
						: 'border-gray-700 bg-gray-900 text-gray-300 hover:border-gray-500 hover:text-white'
				}`}
				onclick={() => (showAll = !showAll)}
			>
				All
			</button>
		</div>
		<span class="ml-auto text-[11px] text-gray-500">
			Left: CARLA twin · Right: {mode === 'replay' ? 'recorded street video' : 'real street camera'}
		</span>
	</div>

	{#if mode === 'replay' && selectedObjectId}
		<div class="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-gray-800 bg-gray-950 px-4 py-2 font-mono text-[11px]">
			<span class="text-amber-300">object {selectedObjectId}</span>
			{#if selectedTwinObject?.actor_present === true && selectedTwinObject.actor_id && selectedTwinObject.carla_transform}
				<span class="text-cyan-300">
					CARLA actor #{selectedTwinObject.actor_id} · {selectedTwinObject.actor_type ?? 'unknown type'}
				</span>
				{#if selectedTwinObject.event_id}
					<span class="text-gray-400">event {selectedTwinObject.event_id}</span>
				{/if}
				{#if selectedTwinObject.media_timestamp_utc}
					<span class="text-gray-400">media {selectedTwinObject.media_timestamp_utc}</span>
				{/if}
				{#if selectedTwinObject.media_time_trusted === true && selectedTwinObject.timestamp_schema_version === 2}
					<span class="text-emerald-300">trusted HLS clock</span>
				{:else}
					<span class="text-rose-300">untrusted media clock</span>
				{/if}
			{:else}
				<span class="text-gray-500">waiting for this object at the replay clock</span>
			{/if}
		</div>
	{/if}

	{#if controlError}
		<p class="border-b border-gray-800 px-4 py-2 text-[11px] text-rose-300">{controlError}</p>
	{/if}

	{#if showAll}
		<div class="grid grid-cols-1 gap-px bg-gray-900 xl:grid-cols-2">
			{#each cameraIds as cameraId}
				<div class="grid grid-cols-2 gap-px bg-gray-900">
					<TwinCameraView {cameraId} {wsBaseUrl} />
					{@render realPane(cameraId)}
				</div>
			{/each}
		</div>
	{:else}
		<div class="grid grid-cols-1 gap-px bg-gray-900 lg:grid-cols-2">
			<TwinCameraView cameraId={selectedCamera} {wsBaseUrl} />
			{@render realPane(selectedCamera)}
		</div>
	{/if}

	<!-- Scrub the twin through the past 24h of recorded detections -->
	<div class="px-4 py-3">
		<TimelineStrip
			{viewStartMs}
			{viewEndMs}
			{cursorMs}
			liveEdgeMs={nowMs}
			events={timeline?.events ?? []}
			histogram={timeline?.histogram ?? []}
			bucketSeconds={timeline?.bucketSeconds ?? 60}
			coverage={[]}
			{selectedObjectId}
			onScrub={handleScrub}
			onSelectEvent={handleSelectEvent}
			onViewChange={(startMs, endMs) => {
				viewStartMs = startMs;
				viewEndMs = endMs;
			}}
		/>
		{#if !replaySupported && mode === 'live'}
			<p class="mt-1 text-[11px] text-gray-600">
				Scrub to replay the twin from recorded detections (past 24h).
			</p>
		{/if}
	</div>

	<!-- Objects DB: live, or time-locked to the replay clock -->
	<RecentDetectionsPanel
		limit={25}
		range={mode === 'replay' && replayClockMs !== null
			? {
					start: toIsoMillis(Math.floor(replayClockMs / 10_000) * 10_000 - 30_000),
					end: toIsoMillis(Math.floor(replayClockMs / 10_000) * 10_000 + 30_000)
				}
			: null}
		highlightObjectId={selectedObjectId}
	/>
</div>
