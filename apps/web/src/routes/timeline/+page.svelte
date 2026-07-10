<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import Header from '$lib/components/Header.svelte';
	import ArchiveVideoCard from '$lib/components/ArchiveVideoCard.svelte';
	import LiveVideoCard from '$lib/components/LiveVideoCard.svelte';
	import TimelineStrip from '$lib/components/TimelineStrip.svelte';
	import RecentDetectionsPanel from '$lib/components/RecentDetectionsPanel.svelte';
	import { fetchDetectionTimeline, fetchVideoCoverage } from '$lib/api';
	import { loadRuntimeConfig, type RuntimeConfig } from '$lib/runtime-config';
	import {
		TIMELINE_SPAN_MS,
		mergeCoverageIntervals,
		parseIsoMs,
		toIsoMillis,
		windowForCursor,
		type PlaybackWindow
	} from '$lib/timeline';
	import type { DetectionTimeline, TimelineEvent, VideoCoverage } from '$lib/types';

	let runtimeConfig = $state<RuntimeConfig | null>(null);
	let mode = $state<'live' | 'archive'>('live');
	let nowMs = $state(Date.now());
	let cursorMs = $state(Date.now());
	let viewStartMs = $state(Date.now() - TIMELINE_SPAN_MS);
	let viewEndMs = $state(Date.now());
	let playing = $state(true);
	let playbackWindow = $state<PlaybackWindow | null>(null);
	let seekNonce = $state(0);
	let primaryCameraId = $state('ch1');
	let selectedObjectId = $state<string | null>(null);
	let timeline = $state<DetectionTimeline | null>(null);
	let coverageByCamera = $state<Record<string, VideoCoverage>>({});
	let timelineError = $state<string | null>(null);

	let cameraIds = $derived(runtimeConfig?.videoCameraIds ?? ['ch1', 'ch2', 'ch3', 'ch4']);
	// Quantised to 10s steps so playback doesn't re-query the DB on every tick.
	let dbRange = $derived.by(() => {
		if (mode !== 'archive') return null;
		const quantised = Math.floor(cursorMs / 10_000) * 10_000;
		return {
			start: toIsoMillis(quantised - 30_000),
			end: toIsoMillis(quantised + 30_000)
		};
	});

	let clockTimer: ReturnType<typeof setInterval> | null = null;
	let refreshTimer: ReturnType<typeof setInterval> | null = null;

	async function loadTimeline() {
		try {
			const end = Date.now();
			const start = end - TIMELINE_SPAN_MS;
			timeline = await fetchDetectionTimeline({
				start: toIsoMillis(start),
				end: toIsoMillis(end),
				bucketSeconds: 60
			});
			timelineError = null;
		} catch (err) {
			timelineError = err instanceof Error ? err.message : 'Failed to load timeline.';
		}
	}

	async function loadCoverage() {
		const end = Date.now();
		const start = end - TIMELINE_SPAN_MS;
		// The Lambda pages ListFragments sequentially and can't sweep 24h of
		// ~2s fragments inside API Gateway's 30s limit — fan out 4h chunks
		// per camera and merge the intervals client-side.
		const CHUNK_MS = 4 * 60 * 60 * 1000;
		const chunks: { start: number; end: number }[] = [];
		for (let t = start; t < end; t += CHUNK_MS) {
			chunks.push({ start: t, end: Math.min(t + CHUNK_MS, end) });
		}
		const results = await Promise.allSettled(
			cameraIds.map(async (cameraId) => {
				const parts = await Promise.allSettled(
					chunks.map((chunk) =>
						fetchVideoCoverage(cameraId, {
							start: toIsoMillis(chunk.start),
							end: toIsoMillis(chunk.end)
						})
					)
				);
				const intervals = parts.flatMap((part) =>
					part.status === 'fulfilled' ? part.value.intervals : []
				);
				const fragmentCount = parts.reduce(
					(sum, part) => sum + (part.status === 'fulfilled' ? part.value.fragmentCount : 0),
					0
				);
				return {
					cameraId,
					start: toIsoMillis(start),
					end: toIsoMillis(end),
					intervals: mergeCoverageIntervals(intervals),
					fragmentCount,
					truncated: parts.some(
						(part) => part.status === 'fulfilled' && part.value.truncated
					)
				} satisfies VideoCoverage;
			})
		);
		const next: Record<string, VideoCoverage> = {};
		results.forEach((result, i) => {
			if (result.status === 'fulfilled') {
				next[cameraIds[i]] = result.value;
			}
		});
		coverageByCamera = next;
	}

	function goLive() {
		mode = 'live';
		playing = true;
		selectedObjectId = null;
		cursorMs = Date.now();
	}

	function scrubTo(epochMs: number) {
		const now = Date.now();
		nowMs = now;
		// Scrubbing to (or past) the live edge returns to live mode.
		if (now - epochMs < 20_000) {
			goLive();
			return;
		}
		mode = 'archive';
		cursorMs = epochMs;
		const win = windowForCursor(epochMs, now);
		if (!playbackWindow || win.start !== playbackWindow.start || win.end !== playbackWindow.end) {
			playbackWindow = win;
		}
		seekNonce += 1;
	}

	function handleSelectEvent(event: TimelineEvent) {
		selectedObjectId = event.object_id;
		if (event.media_time_trusted !== true || event.timestamp_schema_version !== 2) {
			timelineError =
				'This event uses the legacy receipt-time clock; archive correlation is not trusted.';
			return;
		}
		const firstSeen = parseIsoMs(event.first_seen);
		if (firstSeen !== null) {
			scrubTo(Math.max(firstSeen - 10_000, Date.now() - TIMELINE_SPAN_MS));
		}
		if (event.device_id) {
			const channel = event.device_id.split('-').pop();
			if (channel && cameraIds.includes(channel)) {
				primaryCameraId = channel;
			}
		}
	}

	function handleViewChange(startMs: number, endMs: number) {
		viewStartMs = startMs;
		viewEndMs = endMs;
	}

	function handlePrimaryTime(epochMs: number) {
		cursorMs = epochMs;
		// Roll into the next playback window as playback approaches the edge.
		if (playbackWindow && epochMs >= playbackWindow.endMs - 1_000) {
			scrubTo(epochMs + 2_000);
		}
	}

	onMount(async () => {
		runtimeConfig = await loadRuntimeConfig();
		const now = Date.now();
		nowMs = now;
		cursorMs = now;
		viewStartMs = now - TIMELINE_SPAN_MS;
		viewEndMs = now;

		void loadTimeline();
		void loadCoverage();

		clockTimer = setInterval(() => {
			nowMs = Date.now();
			if (mode === 'live') {
				cursorMs = nowMs;
				viewEndMs = nowMs;
				viewStartMs = Math.max(viewStartMs, nowMs - TIMELINE_SPAN_MS);
			}
		}, 1000);
		refreshTimer = setInterval(() => {
			void loadTimeline();
			void loadCoverage();
		}, 60_000);
	});

	onDestroy(() => {
		if (clockTimer) clearInterval(clockTimer);
		if (refreshTimer) clearInterval(refreshTimer);
	});
</script>

<svelte:head>
	<title>V2X Street Camera Timeline</title>
</svelte:head>

<div class="flex h-screen flex-col overflow-hidden bg-gray-950">
	<Header />

	<div class="min-h-0 flex-1 overflow-y-auto bg-black">
		<!-- Camera grid -->
		<div
			class="grid gap-px bg-gray-900"
			style="grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));"
		>
			{#each cameraIds as cameraId}
				<div
					class={`relative ${cameraId === primaryCameraId ? 'ring-1 ring-amber-400/60 ring-inset' : ''}`}
					role="button"
					tabindex="0"
					onclick={() => (primaryCameraId = cameraId)}
					onkeydown={(e) => e.key === 'Enter' && (primaryCameraId = cameraId)}
				>
					{#if mode === 'archive' && playbackWindow}
						<ArchiveVideoCard
							{cameraId}
							windowStart={playbackWindow.start}
							windowEnd={playbackWindow.end}
							windowStartMs={playbackWindow.startMs}
							{cursorMs}
							{seekNonce}
							{playing}
							isPrimary={cameraId === primaryCameraId}
							onTimeUpdate={handlePrimaryTime}
						/>
					{:else}
						<LiveVideoCard {cameraId} />
					{/if}
				</div>
			{/each}
		</div>

		<!-- Controls + timeline -->
		<div class="flex flex-col gap-2 px-4 py-3">
			<div class="flex items-center gap-2">
				<button
					class={`border px-3 py-1.5 text-[11px] font-semibold tracking-[0.16em] uppercase transition ${
						mode === 'live'
							? 'border-emerald-400/60 bg-emerald-400/10 text-emerald-200'
							: 'border-gray-700 bg-gray-900 text-gray-300 hover:border-gray-500 hover:text-white'
					}`}
					onclick={goLive}
				>
					Live
				</button>
				{#if mode === 'archive'}
					<button
						class="border border-gray-700 bg-gray-900 px-3 py-1.5 text-[11px] font-semibold tracking-[0.16em] text-gray-200 uppercase hover:border-gray-500"
						onclick={() => (playing = !playing)}
					>
						{playing ? 'Pause' : 'Play'}
					</button>
					<button
						class="border border-gray-700 bg-gray-900 px-3 py-1.5 text-[11px] tracking-[0.16em] text-gray-300 uppercase hover:border-gray-500 hover:text-white"
						onclick={() => scrubTo(cursorMs - 30_000)}
					>
						-30s
					</button>
					<button
						class="border border-gray-700 bg-gray-900 px-3 py-1.5 text-[11px] tracking-[0.16em] text-gray-300 uppercase hover:border-gray-500 hover:text-white"
						onclick={() => scrubTo(cursorMs + 30_000)}
					>
						+30s
					</button>
				{/if}
				{#if timeline}
					<span class="ml-auto text-[11px] text-gray-500">
						{timeline.events.length} events / {timeline.totalDetections} detections in the past 24h
						{#if timeline.truncated}<span class="text-amber-400">(truncated)</span>{/if}
					</span>
				{/if}
			</div>

			{#if timelineError}
				<p class="text-[11px] text-rose-300">{timelineError}</p>
			{/if}

			<TimelineStrip
				{viewStartMs}
				{viewEndMs}
				{cursorMs}
				liveEdgeMs={nowMs}
				events={timeline?.events ?? []}
				histogram={timeline?.histogram ?? []}
				bucketSeconds={timeline?.bucketSeconds ?? 60}
				coverage={coverageByCamera[primaryCameraId]?.intervals ?? []}
				{selectedObjectId}
				onScrub={scrubTo}
				onSelectEvent={handleSelectEvent}
				onViewChange={handleViewChange}
			/>
		</div>

		<!-- Objects DB: live-polling, or time-locked to the scrub cursor -->
		<RecentDetectionsPanel limit={50} range={dbRange} highlightObjectId={selectedObjectId} />
	</div>
</div>
