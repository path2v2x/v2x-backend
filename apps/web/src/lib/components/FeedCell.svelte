<script lang="ts">
	import type { TrackedObject, FreshnessLevel } from '$lib/types';
	import { FRESHNESS_THRESHOLDS, OBJECT_COLORS } from '$lib/constants';

	interface Props {
		object: TrackedObject;
		onselect?: (objectId: string) => void;
	}

	let { object, onselect }: Props = $props();

	let now = $state(Date.now());
	let imageError = $state(false);

	// The currently displayed (loaded) URL — only swapped once the
	// new image has finished loading in the background, so there is
	// never a blank flash between snapshots.
	let displayUrl = $state<string | null>(null);
	let displaySnapshotTimestamp = $state<string | null>(null);
	let displayUrlValue: string | null = null;
	let displaySnapshotMs: number | null = null;
	let preloadingUrl: string | null = null;
	let preloadingSnapshotMs: number | null = null;
	let desiredUrl: string | null = null;
	let desiredSnapshotTimestamp: string | null = null;
	let preloadGeneration = 0;

	// Tick every second to update freshness
	$effect(() => {
		const interval = setInterval(() => {
			now = Date.now();
		}, 1000);

		return () => clearInterval(interval);
	});

	function timestampMs(value: string | null): number | null {
		if (!value) return null;
		const parsed = Date.parse(value);
		return Number.isFinite(parsed) ? parsed : null;
	}

	function invalidatePreload() {
		preloadGeneration += 1;
		preloadingUrl = null;
		preloadingSnapshotMs = null;
	}

	// Preload a new snapshot before swapping it into view. The producer clock
	// and generation guard prevent a late load event from restoring a URL that
	// has since been cleared or superseded.
	$effect(() => {
		const newUrl = object.snapshot_url;
		const newTimestamp = object.snapshot_timestamp;
		const newSnapshotMs = timestampMs(newTimestamp);

		if (!newUrl) {
			desiredUrl = null;
			desiredSnapshotTimestamp = null;
			invalidatePreload();
			displayUrlValue = null;
			displaySnapshotMs = null;
			displayUrl = null;
			displaySnapshotTimestamp = null;
			imageError = false;
			return;
		}

		const newestKnownMs = Math.max(
			displaySnapshotMs ?? Number.NEGATIVE_INFINITY,
			preloadingSnapshotMs ?? Number.NEGATIVE_INFINITY
		);
		if (
			newestKnownMs !== Number.NEGATIVE_INFINITY &&
			(newSnapshotMs === null || newSnapshotMs < newestKnownMs)
		) {
			return;
		}

		desiredUrl = newUrl;
		desiredSnapshotTimestamp = newTimestamp;
		if (newUrl === displayUrlValue) {
			invalidatePreload();
			displaySnapshotMs = newSnapshotMs;
			displaySnapshotTimestamp = newTimestamp;
			imageError = false;
			return;
		}
		if (newUrl === preloadingUrl && newSnapshotMs === preloadingSnapshotMs) return;

		const generation = ++preloadGeneration;
		preloadingUrl = newUrl;
		preloadingSnapshotMs = newSnapshotMs;
		imageError = false;

		const img = new Image();
		img.onload = () => {
			if (
				generation !== preloadGeneration ||
				desiredUrl !== newUrl ||
				desiredSnapshotTimestamp !== newTimestamp
			) {
				return;
			}
			displayUrlValue = newUrl;
			displaySnapshotMs = newSnapshotMs;
			displayUrl = newUrl;
			displaySnapshotTimestamp = newTimestamp;
			preloadingUrl = null;
			preloadingSnapshotMs = null;
			imageError = false;
		};
		img.onerror = () => {
			if (generation !== preloadGeneration || desiredUrl !== newUrl) return;
			preloadingUrl = null;
			preloadingSnapshotMs = null;
			imageError = displayUrlValue === null;
		};
		img.src = newUrl;
	});

	$effect(() => () => {
		desiredUrl = null;
		invalidatePreload();
	});

	let freshnessMs = $derived(
		displaySnapshotTimestamp
			? now - new Date(displaySnapshotTimestamp).getTime()
			: Infinity
	);

	let freshnessLevel: FreshnessLevel = $derived(
		freshnessMs < FRESHNESS_THRESHOLDS.fresh
			? 'fresh'
			: freshnessMs < FRESHNESS_THRESHOLDS.stale
				? 'stale'
				: 'old'
	);

	let freshnessText = $derived(
		displaySnapshotTimestamp
			? freshnessMs < 1000
				? 'just now'
				: freshnessMs < 60_000
					? `${Math.floor(freshnessMs / 1000)}s ago`
					: freshnessMs < 3_600_000
						? `${Math.floor(freshnessMs / 60_000)}m ago`
						: 'stale'
			: 'no data'
	);

	let freshnessColor = $derived(
		freshnessLevel === 'fresh'
			? 'bg-green-500'
			: freshnessLevel === 'stale'
				? 'bg-amber-500'
				: 'bg-red-500'
	);

	let objectColor = $derived(
		OBJECT_COLORS[object.object_type] ?? OBJECT_COLORS.default
	);

	let objectTypeLabel = $derived(
		object.object_type
			.replace(/_/g, ' ')
			.replace(/\b\w/g, (c) => c.toUpperCase())
	);

	function handleClick() {
		onselect?.(object.object_id);
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Enter' || e.key === ' ') {
			e.preventDefault();
			handleClick();
		}
	}
</script>

<div
	class="group relative cursor-pointer overflow-hidden rounded-lg border border-gray-700/50
	       bg-gray-900 transition-all duration-200 hover:border-gray-500 hover:shadow-lg
	       hover:shadow-black/30 focus-visible:outline-2 focus-visible:outline-blue-500"
	style="aspect-ratio: 4 / 3;"
	role="button"
	tabindex="0"
	onclick={handleClick}
	onkeydown={handleKeydown}
	aria-label="View details for {object.object_id}"
>
	<!-- Snapshot image or placeholder -->
	<div class="absolute inset-0">
		{#if displayUrl && !imageError}
			<img
				src={displayUrl}
				alt="Snapshot of {object.object_id}"
				class="h-full w-full object-cover"
			/>
		{:else if object.snapshot_url && !displayUrl && !imageError}
			<!-- First image still loading -->
			<div class="absolute inset-0 flex items-center justify-center bg-gray-900">
				<div class="h-6 w-6 animate-spin rounded-full border-2 border-gray-600 border-t-gray-300"></div>
			</div>
		{:else}
			<!-- Placeholder -->
			<div class="flex h-full flex-col items-center justify-center gap-2 bg-gray-900">
				<div
					class="flex h-12 w-12 items-center justify-center rounded-full opacity-60"
					style="background-color: {objectColor}20; border: 1px solid {objectColor}40;"
				>
					{#if object.object_type === 'vehicle'}
						<svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke={objectColor} stroke-width="1.5">
							<path stroke-linecap="round" stroke-linejoin="round" d="M8.25 18.75a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m3 0h6m-9 0H3.375a1.125 1.125 0 0 1-1.125-1.125V14.25m17.25 4.5a1.5 1.5 0 0 1-3 0m3 0a1.5 1.5 0 0 0-3 0m3 0h1.125c.621 0 1.129-.504 1.09-1.124a17.902 17.902 0 0 0-3.213-9.193 2.056 2.056 0 0 0-1.58-.86H14.25M16.5 18.75h-2.25m0-11.177v-.958c0-.568-.422-1.048-.987-1.106a48.554 48.554 0 0 0-10.026 0 1.106 1.106 0 0 0-.987 1.106v7.635m12-6.677v6.677m0 4.5v-4.5m0 0h-12" />
						</svg>
					{:else if object.object_type === 'walker'}
						<svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke={objectColor} stroke-width="1.5">
							<path stroke-linecap="round" stroke-linejoin="round" d="M15.75 6a3.75 3.75 0 1 1-7.5 0 3.75 3.75 0 0 1 7.5 0ZM4.501 20.118a7.5 7.5 0 0 1 14.998 0A17.933 17.933 0 0 1 12 21.75c-2.676 0-5.216-.584-7.499-1.632Z" />
						</svg>
					{:else if object.object_type === 'traffic_cone'}
						<svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke={objectColor} stroke-width="1.5">
							<path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
						</svg>
					{:else}
						<svg class="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke={objectColor} stroke-width="1.5">
							<path stroke-linecap="round" stroke-linejoin="round" d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 5.25h.008v.008H12v-.008Z" />
						</svg>
					{/if}
				</div>
				<span class="text-xs text-gray-500">{objectTypeLabel}</span>
				<span class="text-[10px] text-gray-600">No snapshot</span>
			</div>
		{/if}
	</div>

	<!-- Top-left: object type badge -->
	<div class="absolute top-2 left-2 z-10">
		<span
			class="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white/90"
			style="background-color: {objectColor}cc;"
		>
			{objectTypeLabel}
		</span>
	</div>

	<!-- Top-right: freshness dot -->
	<div class="absolute top-2 right-2 z-10">
		<div class="flex items-center gap-1 rounded-full bg-black/60 px-2 py-0.5 backdrop-blur-sm">
			<span class="relative flex h-2 w-2">
				{#if freshnessLevel === 'fresh'}
					<span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75"></span>
				{/if}
				<span class="relative inline-flex h-2 w-2 rounded-full {freshnessColor}"></span>
			</span>
			<span class="text-[10px] text-gray-300">{freshnessText}</span>
		</div>
	</div>

	<!-- Bottom overlay bar -->
	<div class="absolute right-0 bottom-0 left-0 z-10 bg-gradient-to-t from-black/80 via-black/50 to-transparent px-3 pt-6 pb-2">
		<div class="flex items-end justify-between gap-2">
			<div class="min-w-0 flex-1">
				<p class="truncate text-xs font-medium text-white">
					{object.object_id}
				</p>
				{#if object.street_name}
					<p class="truncate text-[10px] text-gray-400">
						{object.street_name}
					</p>
				{/if}
			</div>
			{#if object.confidence != null}
				<span class="shrink-0 text-[10px] text-gray-400">
					{Math.round(object.confidence * 100)}%
				</span>
			{/if}
		</div>
	</div>

	<!-- Hover overlay -->
	<div class="absolute inset-0 bg-white/5 opacity-0 transition-opacity duration-200 group-hover:opacity-100"></div>
</div>
