<script lang="ts">
	import { onDestroy, tick } from 'svelte';
	import Hls from 'hls.js';
	import { fetchVideoSession } from '$lib/api';

	interface Props {
		cameraId: string;
		streamUrl?: string;
		sourceLabel?: string;
	}

	let { cameraId, streamUrl = '', sourceLabel = 'Raw' }: Props = $props();

	let videoElA = $state<HTMLVideoElement | null>(null);
	let videoElB = $state<HTMLVideoElement | null>(null);
	let activeVideoIndex = $state<0 | 1>(0);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let connected = $state(false);
	let sessionExpiresIn = $state<number | null>(null);
	let mjpegUrl = $state('');
	let players: [Hls | null, Hls | null] = [null, null];
	let refreshTimer: ReturnType<typeof setTimeout> | null = null;
	let connectionKey = '';
	let connectionRevision = 0;

	function clearRefreshTimer() {
		if (refreshTimer) {
			clearTimeout(refreshTimer);
			refreshTimer = null;
		}
	}

	function renewalLeadSeconds(): number {
		// Four cards are normally mounted together. Deterministic spacing avoids
		// briefly doubling all four decoders and starving the streams that are
		// still visible. Unknown camera IDs retain the conservative middle lead.
		const cameraOrder = ['ch1', 'ch2', 'ch3', 'ch4'];
		const cameraIndex = cameraOrder.indexOf(cameraId);
		return cameraIndex < 0 ? 45 : 60 - cameraIndex * 10;
	}

	function scheduleRefresh(expiresIn: number | null) {
		clearRefreshTimer();
		const leadSeconds = renewalLeadSeconds();
		if (!expiresIn || expiresIn <= leadSeconds + 5) return;
		refreshTimer = setTimeout(() => {
			void renewSession();
		}, (expiresIn - leadSeconds) * 1000);
	}

	function videoAt(index: 0 | 1): HTMLVideoElement | null {
		return index === 0 ? videoElA : videoElB;
	}

	function destroySlot(index: 0 | 1) {
		if (players[index]) {
			players[index]?.destroy();
			players[index] = null;
		}
		const video = videoAt(index);
		if (video) {
			video.pause();
			video.removeAttribute('src');
			video.load();
		}
	}

	function destroyPlayer() {
		clearRefreshTimer();
		mjpegUrl = '';
		destroySlot(0);
		destroySlot(1);
		connected = false;
	}

	async function attachHls(url: string, index: 0 | 1) {
		destroySlot(index);
		const video = videoAt(index);
		if (!video) throw new Error('Video element unavailable');

		if (Hls.isSupported()) {
			const player = new Hls({
				enableWorker: true,
				lowLatencyMode: false
			});
			players[index] = player;
			player.loadSource(url);
			player.attachMedia(video);
		} else if (video.canPlayType('application/vnd.apple.mpegurl')) {
			video.src = url;
		} else {
			throw new Error('HLS playback is not supported in this browser');
		}

		// The play promise resolves only once playback has actually begun. This
		// makes a standby player safe to reveal during a session handoff.
		let playbackTimeout: ReturnType<typeof setTimeout> | null = null;
		try {
			await Promise.race([
				video.play(),
				new Promise<never>((_resolve, reject) => {
					playbackTimeout = setTimeout(
						() => reject(new Error('Timed out preparing video session')),
						20_000
					);
				})
			]);
		} finally {
			if (playbackTimeout) clearTimeout(playbackTimeout);
		}
	}

	async function renewSession() {
		const revision = connectionRevision;
		const oldIndex = activeVideoIndex;
		const standbyIndex: 0 | 1 = oldIndex === 0 ? 1 : 0;
		try {
			const session = await fetchVideoSession(cameraId);
			if (revision !== connectionRevision) return;
			await attachHls(session.hlsUrl, standbyIndex);
			if (revision !== connectionRevision) {
				destroySlot(standbyIndex);
				return;
			}

			// Switch only after the replacement stream is already playing, then
			// retire the old expiring session on the next render turn.
			activeVideoIndex = standbyIndex;
			sessionExpiresIn = session.expiresIn;
			error = null;
			connected = true;
			scheduleRefresh(session.expiresIn);
			await tick();
			await new Promise((resolve) => setTimeout(resolve, 200));
			if (revision === connectionRevision) destroySlot(oldIndex);
		} catch (err) {
			if (revision !== connectionRevision) return;
			destroySlot(standbyIndex);
			error = err instanceof Error ? err.message : 'Video session renewal failed';
			// Keep the still-playing active session and retry while its early-renewal
			// safety margin remains available.
			clearRefreshTimer();
			refreshTimer = setTimeout(() => void renewSession(), 5_000);
		}
	}

	function isImageStream(url: string): boolean {
		const cleanUrl = url.split('?')[0].toLowerCase();
		return (
			cleanUrl.endsWith('.mjpg') ||
			cleanUrl.endsWith('.mjpeg') ||
			cleanUrl.endsWith('.jpg') ||
			cleanUrl.endsWith('.jpeg') ||
			cleanUrl.endsWith('.png') ||
			cleanUrl.endsWith('.svg')
		);
	}

	async function connect() {
		const revision = ++connectionRevision;
		loading = true;
		error = null;
		destroyPlayer();
		// MJPEG renders an <img> while HLS needs a <video>. Let Svelte apply
		// that element swap before attaching a newly selected HLS source.
		await tick();
		if (revision !== connectionRevision) return;

		try {
			const sourceUrl = streamUrl.trim();
			let hlsUrl = sourceUrl;
			if (sourceUrl) {
				sessionExpiresIn = null;
				scheduleRefresh(null);
			} else {
				const session = await fetchVideoSession(cameraId);
				if (revision !== connectionRevision) return;
				hlsUrl = session.hlsUrl;
				sessionExpiresIn = session.expiresIn;
				scheduleRefresh(session.expiresIn);
			}

			if (revision !== connectionRevision) return;
			if (sourceUrl && isImageStream(sourceUrl)) {
				mjpegUrl = sourceUrl;
				connected = true;
				return;
			}

			// Prefer hls.js whenever Media Source Extensions are available. Recent
			// Chromium builds report native HLS support on Linux but intermittently
			// fail fMP4 Kinesis playlists with a non-recovering demux/ORB error.
			// Safari has no hls.js MSE path and falls through to native HLS.
			activeVideoIndex = 0;
			await attachHls(hlsUrl, 0);
			if (revision !== connectionRevision) return;
			connected = true;
		} catch (err) {
			if (revision !== connectionRevision) return;
			error = err instanceof Error ? err.message : 'Unknown playback error';
			destroyPlayer();
		} finally {
			if (revision === connectionRevision) loading = false;
		}
	}

	$effect(() => {
		const key = `${cameraId}|${streamUrl}`;
		if (key === connectionKey) return;
		connectionKey = key;
		void connect();
	});

	onDestroy(() => {
		connectionRevision += 1;
		destroyPlayer();
	});
</script>

<div class="relative overflow-hidden border border-gray-900 bg-black" style="aspect-ratio: 4 / 3;">
	<div class="absolute top-2 left-2 z-10 bg-black/70 px-2 py-1 text-[10px] font-medium tracking-[0.18em] text-gray-200 uppercase">
		{cameraId}
	</div>

	<div class="absolute top-2 right-2 z-10 flex items-center gap-2">
		<span class="bg-black/70 px-2 py-1 text-[10px] font-medium tracking-[0.16em] text-gray-200 uppercase">
			{sourceLabel}
		</span>
		{#if connected}
			<span class="bg-red-600 px-2 py-1 text-[10px] font-semibold tracking-[0.16em] text-white uppercase">
				Live
			</span>
		{/if}
		{#if sessionExpiresIn}
			<span class="bg-black/70 px-2 py-1 text-[10px] text-gray-400">
				{sessionExpiresIn}s
			</span>
		{/if}
	</div>

	<div class="absolute inset-0">
		{#if mjpegUrl}
			<img
				src={mjpegUrl}
				alt={`${cameraId} perception stream`}
				class="h-full w-full object-cover"
				onload={() => {
					connected = true;
				}}
				onerror={() => {
					error = 'Perception stream unavailable';
					connected = false;
				}}
			/>
		{:else}
			<video
				bind:this={videoElA}
				class="absolute inset-0 h-full w-full object-cover transition-opacity duration-150"
				class:opacity-0={activeVideoIndex !== 0}
				class:opacity-100={activeVideoIndex === 0}
				aria-hidden={activeVideoIndex !== 0}
				playsinline
				muted
			></video>
			<video
				bind:this={videoElB}
				class="absolute inset-0 h-full w-full object-cover transition-opacity duration-150"
				class:opacity-0={activeVideoIndex !== 1}
				class:opacity-100={activeVideoIndex === 1}
				aria-hidden={activeVideoIndex !== 1}
				playsinline
				muted
			></video>
		{/if}

		{#if !connected && !loading}
			<div class="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/55">
				<button
					class="border border-gray-600 bg-black/80 px-4 py-2 text-[11px] font-medium tracking-[0.16em] text-white uppercase hover:border-gray-400"
					onclick={connect}
				>
					Reconnect
				</button>
			</div>
		{/if}

		{#if loading}
			<div class="absolute inset-0 flex items-center justify-center bg-black/45">
				<div class="h-8 w-8 animate-spin rounded-full border-2 border-gray-700 border-t-cyan-300"></div>
			</div>
		{/if}
	</div>

	{#if error}
		<div class="absolute right-0 bottom-0 left-0 z-10 bg-black/85 px-3 py-2 text-[11px] text-rose-300">
			{error}
		</div>
	{/if}
</div>
