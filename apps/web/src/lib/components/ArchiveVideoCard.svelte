<script lang="ts">
	import { onDestroy } from 'svelte';
	import Hls from 'hls.js';
	import { fetchVideoSession } from '$lib/api';
	import {
		archiveCursorNeedsCorrection,
		archiveMediaTimeForEpoch,
		formatClock
	} from '$lib/timeline';

	interface Props {
		cameraId: string;
		/** ISO window bounds; changing them loads a fresh ON_DEMAND session. */
		windowStart: string;
		windowEnd: string;
		windowStartMs: number;
		/** Target wall-clock position; bump seekNonce to force a seek. */
		cursorMs: number;
		seekNonce: number;
		playing: boolean;
		/** Only the primary card reports time back to the page. */
		isPrimary?: boolean;
		onTimeUpdate?: (epochMs: number) => void;
	}

	let {
		cameraId,
		windowStart,
		windowEnd,
		windowStartMs,
		cursorMs,
		seekNonce,
		playing,
		isPrimary = false,
		onTimeUpdate
	}: Props = $props();

	let videoEl = $state<HTMLVideoElement | null>(null);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let currentEpochMs = $state<number | null>(null);
	let hls: Hls | null = null;
	let loadedWindowKey = '';
	// Maps media time (s) to wall clock: epochMs = pdtOffsetMs + mediaTime * 1000.
	// Recomputed on every fragment change so discontinuities stay accurate.
	let pdtOffsetMs: number | null = null;
	let appliedSeekNonce = -1;

	function destroyPlayer() {
		if (hls) {
			hls.destroy();
			hls = null;
		}
		pdtOffsetMs = null;
		loadedWindowKey = '';
	}

	function mediaTimeForEpoch(epochMs: number): number {
		// Before the first fragment lands, use the playback window as a
		// provisional base. FRAG_CHANGED corrects this with the real PDT.
		return archiveMediaTimeForEpoch(epochMs, pdtOffsetMs, windowStartMs);
	}

	async function loadWindow() {
		const key = `${cameraId}|${windowStart}|${windowEnd}`;
		if (key === loadedWindowKey) return;
		loading = true;
		error = null;
		destroyPlayer();
		loadedWindowKey = key;

		try {
			const session = await fetchVideoSession(cameraId, { start: windowStart, end: windowEnd });
			if (!videoEl) throw new Error('Video element unavailable');
			if (Hls.isSupported()) {
				hls = new Hls({ enableWorker: true, lowLatencyMode: false });
				hls.on(Hls.Events.FRAG_CHANGED, (_event, data) => {
					const pdt = data.frag.programDateTime;
					if (pdt) {
						pdtOffsetMs = pdt - data.frag.start * 1000;
						// The initial seek runs before HLS has exposed its real PDT
						// mapping. Correct it as soon as a timestamped fragment is
						// active; otherwise archive video can remain seconds away from
						// the replay clock until a later manual scrub.
						if (videoEl) {
							const target = mediaTimeForEpoch(cursorMs);
							const currentEpoch = pdtOffsetMs + videoEl.currentTime * 1000;
							if (archiveCursorNeedsCorrection(cursorMs, currentEpoch)) {
								videoEl.currentTime = target;
							}
						}
					}
				});
				hls.on(Hls.Events.ERROR, (_event, data) => {
					if (data.fatal) {
						error = `Playback error: ${data.details}`;
					}
				});
				hls.loadSource(session.hlsUrl);
				hls.attachMedia(videoEl);
			} else if (videoEl.canPlayType('application/vnd.apple.mpegurl')) {
				videoEl.src = session.hlsUrl;
			} else {
				throw new Error('HLS playback is not supported in this browser');
			}
			videoEl.currentTime = mediaTimeForEpoch(cursorMs);
			if (playing) {
				await videoEl.play().catch(() => {});
			}
		} catch (err) {
			error = err instanceof Error ? err.message : 'Unknown playback error';
			loadedWindowKey = '';
		} finally {
			loading = false;
		}
	}

	function handleTimeUpdate() {
		if (!videoEl) return;
		const base = pdtOffsetMs ?? windowStartMs;
		currentEpochMs = base + videoEl.currentTime * 1000;
		if (isPrimary) {
			onTimeUpdate?.(currentEpochMs);
		}
	}

	$effect(() => {
		void windowStart;
		void windowEnd;
		if (videoEl) {
			void loadWindow();
		}
	});

	$effect(() => {
		if (seekNonce === appliedSeekNonce || !videoEl) return;
		appliedSeekNonce = seekNonce;
		videoEl.currentTime = mediaTimeForEpoch(cursorMs);
	});

	// Followers drift-correct against the shared cursor instead of emitting time.
	$effect(() => {
		if (isPrimary || !videoEl || currentEpochMs === null) return;
		if (archiveCursorNeedsCorrection(cursorMs, currentEpochMs)) {
			videoEl.currentTime = mediaTimeForEpoch(cursorMs);
		}
	});

	$effect(() => {
		if (!videoEl) return;
		if (playing) {
			void videoEl.play().catch(() => {});
		} else {
			videoEl.pause();
		}
	});

	onDestroy(() => {
		destroyPlayer();
	});
</script>

<div class="relative overflow-hidden border border-gray-900 bg-black" style="aspect-ratio: 4 / 3;">
	<div class="absolute top-2 left-2 z-10 bg-black/70 px-2 py-1 text-[10px] font-medium tracking-[0.18em] text-gray-200 uppercase">
		{cameraId}
	</div>

	<div class="absolute top-2 right-2 z-10 flex items-center gap-2">
		<span class="bg-amber-500/90 px-2 py-1 text-[10px] font-semibold tracking-[0.16em] text-black uppercase">
			Archive
		</span>
		{#if currentEpochMs}
			<span class="bg-black/70 px-2 py-1 font-mono text-[10px] text-gray-300">
				{formatClock(currentEpochMs)}
			</span>
		{/if}
	</div>

	<video
		bind:this={videoEl}
		class="h-full w-full object-cover"
		playsinline
		muted
		ontimeupdate={handleTimeUpdate}
	></video>

	{#if loading}
		<div class="absolute inset-0 flex items-center justify-center bg-black/45">
			<div class="h-8 w-8 animate-spin rounded-full border-2 border-gray-700 border-t-amber-300"></div>
		</div>
	{/if}

	{#if error}
		<div class="absolute right-0 bottom-0 left-0 z-10 flex items-center justify-between gap-2 bg-black/85 px-3 py-2 text-[11px] text-rose-300">
			<span>{error}</span>
			<button
				class="border border-gray-600 px-2 py-1 text-[10px] tracking-[0.14em] text-white uppercase hover:border-gray-400"
				onclick={() => {
					loadedWindowKey = '';
					void loadWindow();
				}}
			>
				Retry
			</button>
		</div>
	{/if}
</div>
