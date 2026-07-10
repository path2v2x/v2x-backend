<script lang="ts">
	import { fetchLivePerceptionDetections } from '$lib/api';
	import type { RuntimeConfig } from '$lib/runtime-config';
	import type {
		LivePerceptionCamera,
		LivePerceptionDetection,
		LivePerceptionDetections
	} from '$lib/types';
	import {
		isProducerTimestampFresh,
		latestProducerTimestamp
	} from '$lib/producer-time';

	const LIVE_DETECTION_STALE_AFTER_MS = 15_000;

	interface Props {
		config: RuntimeConfig | null;
		refreshMs?: number;
	}

	let { config, refreshMs = 2000 }: Props = $props();

	let snapshot = $state<LivePerceptionDetections | null>(null);
	let error = $state<string | null>(null);
	let isLoading = $state(false);
	let producerUpdatedAt = $state<string | null>(null);
	let evaluatedAtMs = $state(Date.now());
	let refreshTimer: ReturnType<typeof setInterval> | null = null;
	let sourceGeneration = 0;
	let latestRequestId = 0;

	let cameraRows = $derived(
		(config?.videoCameraIds || []).map((cameraId) => ({
			cameraId,
			camera: snapshot?.cameras?.[cameraId] || null
		}))
	);
	let currentCameraCount = $derived(
		cameraRows.filter((row) => cameraIsFresh(row.camera)).length
	);

	function stopPolling() {
		if (refreshTimer) {
			clearInterval(refreshTimer);
			refreshTimer = null;
		}
	}

	function hasPerceptionSource(activeConfig: RuntimeConfig): boolean {
		return Boolean(
			activeConfig.perceptionStreamBaseUrl ||
				Object.keys(activeConfig.perceptionStreamUrls).length > 0
		);
	}

	function invalidateRequests() {
		sourceGeneration += 1;
		latestRequestId += 1;
	}

	async function loadSnapshotFor(
		activeConfig: RuntimeConfig,
		generation: number,
		showLoading: boolean
	) {
		if (generation !== sourceGeneration || !hasPerceptionSource(activeConfig)) return;
		const requestId = ++latestRequestId;
		evaluatedAtMs = Date.now();
		error = null;
		isLoading = showLoading;

		try {
			const next = await fetchLivePerceptionDetections();
			if (generation !== sourceGeneration || requestId !== latestRequestId) return;
			evaluatedAtMs = Date.now();
			snapshot = next;
			producerUpdatedAt = latestProducerTimestamp(
				Object.values(next.cameras || {}).map((camera) => camera.updated_at)
			);
		} catch (err) {
			if (generation !== sourceGeneration || requestId !== latestRequestId) return;
			error = err instanceof Error ? err.message : 'Failed to fetch live detections.';
		} finally {
			if (generation === sourceGeneration && requestId === latestRequestId) {
				isLoading = false;
			}
		}
	}

	function loadSnapshot() {
		const activeConfig = config;
		if (!activeConfig || !hasPerceptionSource(activeConfig)) return;
		return loadSnapshotFor(activeConfig, sourceGeneration, snapshot === null);
	}

	function cameraIsFresh(camera: LivePerceptionCamera | null): boolean {
		return isProducerTimestampFresh(
			camera?.updated_at,
			LIVE_DETECTION_STALE_AFTER_MS,
			evaluatedAtMs
		);
	}

	function producerTime(value: string | null | undefined): string | null {
		if (!value) return null;
		const timestamp = Date.parse(value);
		return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleTimeString() : null;
	}

	function typeLabel(value: LivePerceptionDetection['object_type']): string {
		return value ? String(value).replace(/_/g, ' ') : 'unknown';
	}

	function confidenceLabel(value: LivePerceptionDetection['confidence_score']): string {
		const confidence = typeof value === 'number' ? value : Number(value);
		if (!Number.isFinite(confidence)) return '--';
		return `${Math.round(confidence * 100)}%`;
	}

	function trackLabel(detection: LivePerceptionDetection): string {
		if (detection.track_id != null) return `track ${detection.track_id}`;
		if (detection.object_id) return detection.object_id;
		return '';
	}

	function countLabel(camera: LivePerceptionCamera | null): string {
		const count = camera?.detections?.length || 0;
		return count === 1 ? '1 object' : `${count} objects`;
	}

	$effect(() => {
		stopPolling();
		const activeConfig = config;
		const activeRefreshMs = refreshMs;
		invalidateRequests();
		const generation = sourceGeneration;
		snapshot = null;
		producerUpdatedAt = null;
		error = null;
		isLoading = false;
		evaluatedAtMs = Date.now();
		if (!activeConfig || !hasPerceptionSource(activeConfig)) {
			return () => {
				if (sourceGeneration === generation) invalidateRequests();
			};
		}

		void loadSnapshotFor(activeConfig, generation, true);
		refreshTimer = setInterval(() => {
			void loadSnapshotFor(activeConfig, generation, false);
		}, activeRefreshMs);

		return () => {
			stopPolling();
			if (sourceGeneration === generation) invalidateRequests();
		};
	});
</script>

<aside class="border-t border-gray-800 bg-gray-950 lg:border-t-0 lg:border-l">
	<div class="sticky top-0 flex flex-col gap-4 px-4 py-4 lg:w-80">
		<div class="flex items-start justify-between gap-3">
			<div>
				<h2 class="text-sm font-semibold text-white">Live detections</h2>
				{#if producerTime(producerUpdatedAt)}
					<p class="mt-1 text-[11px] {currentCameraCount === cameraRows.length ? 'text-emerald-500' : 'text-amber-400'}">
						{currentCameraCount}/{cameraRows.length} camera snapshots current · latest producer {producerTime(producerUpdatedAt)}
					</p>
				{:else}
					<p class="mt-1 text-[11px] text-gray-500">Waiting for perception metadata</p>
				{/if}
			</div>
			<button
				class="border border-gray-700 bg-black px-2.5 py-1.5 text-[10px] font-medium tracking-[0.14em] text-gray-300 uppercase transition hover:border-gray-500 hover:text-white"
				onclick={loadSnapshot}
			>
				Refresh
			</button>
		</div>

		{#if !config}
			<div class="h-6 w-6 animate-spin rounded-full border-2 border-gray-700 border-t-cyan-300"></div>
		{:else if !hasPerceptionSource(config)}
			<p class="text-xs leading-5 text-gray-500">Perception streams are not configured.</p>
		{:else if error}
			<p class="text-xs leading-5 text-rose-300">{error}</p>
		{:else if isLoading}
			<p class="text-xs text-gray-500">Loading live detector output...</p>
		{/if}

		<div class="flex flex-col gap-3">
			{#each cameraRows as row}
				{@const fresh = cameraIsFresh(row.camera)}
				{@const detections = fresh ? (row.camera?.detections || []) : []}
				<section class="border border-gray-800 bg-black/70">
					<div class="flex items-center justify-between border-b border-gray-800 px-3 py-2">
						<span class="text-[11px] font-semibold tracking-[0.16em] text-gray-300 uppercase">
							{row.cameraId}
						</span>
						<div class="text-right text-[11px] text-gray-500">
							<div>{fresh ? countLabel(row.camera) : 'not current'}</div>
							{#if producerTime(row.camera?.updated_at)}
								<div class="font-mono text-[10px] {fresh ? 'text-emerald-500' : 'text-amber-400'}">
									{fresh ? 'CURRENT' : 'STALE'} · {producerTime(row.camera?.updated_at)}
								</div>
							{/if}
						</div>
					</div>

					{#if row.camera && !fresh}
						<p class="px-3 py-3 text-xs text-amber-300">
							Stale detection snapshot hidden
						</p>
					{:else if detections.length === 0}
						<p class="px-3 py-3 text-xs text-gray-500">No active detections</p>
					{:else}
						<div class="divide-y divide-gray-900">
							{#each detections.slice(0, 4) as detection}
								<div class="grid grid-cols-[minmax(0,1fr)_auto] gap-2 px-3 py-2">
									<div class="min-w-0">
										<p class="truncate text-sm font-medium capitalize text-white">
											{typeLabel(detection.object_type)}
										</p>
										{#if trackLabel(detection)}
											<p class="mt-0.5 truncate font-mono text-[10px] text-gray-500">
												{trackLabel(detection)}
											</p>
										{/if}
									</div>
									<span class="self-start font-mono text-sm font-semibold text-cyan-200">
										{confidenceLabel(detection.confidence_score)}
									</span>
								</div>
							{/each}
							{#if detections.length > 4}
								<p class="px-3 py-2 text-[11px] text-gray-500">
									+{detections.length - 4} more
								</p>
							{/if}
						</div>
					{/if}
				</section>
			{/each}
		</div>
	</div>
</aside>
