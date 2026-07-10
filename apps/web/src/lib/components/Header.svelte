<script lang="ts">
	import { page } from '$app/state';
	import { bridgeStatus } from '$lib/stores/objects';
	import { wsConnected } from '$lib/stores/websocket';

	let status = $derived($bridgeStatus);
	let connected = $derived($wsConnected);

	let statusColor = $derived(
		status.status === 'connected'
			? 'bg-green-500'
			: status.status === 'stale'
				? 'bg-amber-500'
			: status.status === 'error'
					? 'bg-red-500'
					: 'bg-gray-500'
	);

	let statusLabel = $derived(
		status.status === 'connected'
			? 'Connected'
			: status.status === 'stale'
				? 'Stale data'
			: status.status === 'error'
					? 'Error'
					: 'Disconnected'
	);

	function producerTime(value: string | null): string | null {
		if (!value) return null;
		const timestamp = Date.parse(value);
		return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleTimeString() : null;
	}

	let sourceTimestamp = $derived(status.updated_at ?? status.last_heartbeat);
	let sourceTimeLabel = $derived(producerTime(sourceTimestamp));
	let statusTextColor = $derived(
		status.status === 'connected'
			? 'text-green-400'
			: status.status === 'stale'
				? 'text-amber-300'
				: status.status === 'error'
					? 'text-red-400'
					: 'text-gray-400'
	);

	let pathname = $derived(page.url.pathname);

	function navClass(href: string): string {
		const active = href === '/'
			? pathname === '/'
			: pathname.startsWith(href);
		return active
			? 'border-cyan-400/40 bg-cyan-400/10 text-cyan-200'
			: 'border-gray-700/70 bg-gray-900 text-gray-300 hover:border-gray-600 hover:text-white';
	}
</script>

<header class="flex h-14 shrink-0 items-center justify-between border-b border-gray-800 bg-gray-950/80 px-4 backdrop-blur-sm">
	<!-- Left: title -->
	<div class="flex items-center gap-5">
		<div class="flex items-center gap-3">
			<img src="/logo.png" alt="V2X logo" class="h-8" />
			<div>
				<h1 class="text-sm font-semibold text-white">V2X Cyber-Physical System</h1>
				<p class="text-[10px] text-gray-500">Digital twin monitoring dashboard</p>
			</div>
		</div>

	<nav class="hidden items-center gap-2 md:flex">
		<a
			href="/drive?view=twin"
			class={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${navClass('/drive')}`}
			>
				Digital Twin View
			</a>
			<a
				href="/live"
				class={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${navClass('/live')}`}
			>
				Street View Live
			</a>
			<a
				href="/timeline"
				class={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${navClass('/timeline')}`}
			>
				Timeline
			</a>
			<a
				href="/demo-videos"
				class={`rounded-full border px-3 py-1.5 text-xs font-medium transition-colors ${navClass('/demo-videos')}`}
			>
				Demo Videos
			</a>
		</nav>
	</div>

	<div class="flex items-center gap-5">
		<nav class="flex items-center gap-2 md:hidden">
			<a
				href="/drive?view=twin"
				class={`rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors ${navClass('/drive')}`}
			>
				Digital Twin
			</a>
			<a
				href="/live"
				class={`rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors ${navClass('/live')}`}
			>
				Street View Live
			</a>
			<a
				href="/timeline"
				class={`rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors ${navClass('/timeline')}`}
			>
				Timeline
			</a>
			<a
				href="/demo-videos"
				class={`rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors ${navClass('/demo-videos')}`}
			>
				Demo Videos
			</a>
		</nav>
		<!-- Stats -->
		<div class="hidden items-center gap-4 sm:flex">
			<div class="text-center">
				<p class="text-xs font-semibold text-white">{status.objects_tracked}</p>
				<p class="text-[10px] text-gray-500">Objects</p>
			</div>
			<div class="h-6 w-px bg-gray-800"></div>
			<div class="text-center">
				<p class="text-xs font-semibold text-white">{status.cameras_active}</p>
				<p class="text-[10px] text-gray-500">Cameras</p>
			</div>
			<div class="h-6 w-px bg-gray-800"></div>
			<div class="text-center">
				<p class="text-xs font-semibold text-white">{status.carla_fps.toFixed(0)}</p>
				<p class="text-[10px] text-gray-500">FPS</p>
			</div>
		</div>

		<!-- Connection badge -->
			<div
				class="flex items-center gap-2 rounded-full border border-gray-700/50 bg-gray-900 px-3 py-1.5"
				title={sourceTimestamp ? `Producer update: ${sourceTimestamp}` : 'No producer timestamp'}
			>
			<span class="relative flex h-2 w-2">
				{#if connected}
					<span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-75"></span>
				{/if}
				<span class="relative inline-flex h-2 w-2 rounded-full {statusColor}"></span>
			</span>
				<span class="text-xs font-medium {statusTextColor}">
					{statusLabel}
				</span>
				{#if sourceTimeLabel}
					<span class="border-l border-gray-700 pl-2 font-mono text-[10px] text-gray-500">
						Source {sourceTimeLabel}
					</span>
				{/if}
			</div>
	</div>
</header>
