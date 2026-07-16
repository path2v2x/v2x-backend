<script lang="ts">
	import {
		wireLog,
		wirePaused,
		wireTotal,
		clearWireLog,
		exportWireLog,
		type WirePacket
	} from '$lib/stores/driveSocket';

	interface Props {
		onClose: () => void;
	}
	let { onClose }: Props = $props();

	const WIRE_CAP = 3000;
	const RENDER_MAX = 500;

	let filters = $state({ control: true, telemetry: true, binary: true, other: true });

	function bucket(p: WirePacket): keyof typeof filters {
		if (p.kind === 'bin') return 'binary';
		if (p.ptype === 'control') return 'control';
		if (p.ptype === 'telemetry') return 'telemetry';
		return 'other';
	}

	let shown = $derived($wireLog.filter((p) => filters[bucket(p)]).slice(-RENDER_MAX));

	function rowClass(p: WirePacket): string {
		if (p.kind === 'bin') return 'text-gray-500 italic';
		if (p.kind === 'evt') return 'text-amber-300 italic';
		return p.dir === 'TX' ? 'text-blue-300' : 'text-emerald-300';
	}

	function download() {
		const blob = new Blob([exportWireLog()], { type: 'application/x-ndjson' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = 'drive-wire-log.jsonl';
		a.click();
		setTimeout(() => URL.revokeObjectURL(url), 5000);
	}

	// Stick-to-bottom auto scroll
	let streamEl = $state<HTMLDivElement | null>(null);
	let stick = true;
	$effect(() => {
		void shown;
		if (streamEl && stick) streamEl.scrollTop = streamEl.scrollHeight;
	});
	function onScroll() {
		if (!streamEl) return;
		stick = streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 40;
	}
</script>

<!-- Docked console (devtools-style): fills the side column the page provides. -->
<div
	class="flex h-full w-full flex-col overflow-hidden border-l border-gray-700 bg-gray-950 text-gray-200 pointer-events-auto"
	data-testid="packet-log-panel"
>
	<div class="flex items-center justify-between gap-2 border-b border-gray-700 px-3 py-2">
		<span class="text-sm font-semibold tracking-wide text-blue-400">Packets · live wire log</span>
		<div class="flex items-center gap-2 text-[11px]">
			<span class="font-mono text-gray-500">{shown.length} shown · {$wireTotal} total{$wireTotal > WIRE_CAP ? ` · last ${WIRE_CAP} kept` : ''}</span>
			<button
				type="button"
				onclick={onClose}
				class="cursor-pointer px-1 text-xl leading-none text-gray-400 hover:text-white"
				aria-label="Close packet log panel"
			>×</button>
		</div>
	</div>

	<div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-gray-800 px-3 py-1.5 text-[11px]">
		{#each Object.keys(filters) as key (key)}
			<label class="flex cursor-pointer select-none items-center gap-1 font-mono text-gray-400">
				<input
					type="checkbox"
					bind:checked={filters[key as keyof typeof filters]}
					class="cursor-pointer accent-blue-500"
				/>
				{key}
			</label>
		{/each}
		<span class="flex-1"></span>
		<button
			type="button"
			onclick={() => wirePaused.update((p) => !p)}
			class="cursor-pointer rounded bg-gray-700 px-2 py-0.5 text-white hover:bg-gray-600"
		>{$wirePaused ? 'Resume' : 'Pause'}</button>
		<button
			type="button"
			onclick={download}
			disabled={$wireTotal === 0}
			class="cursor-pointer rounded bg-gray-700 px-2 py-0.5 text-white hover:bg-gray-600 disabled:cursor-not-allowed disabled:opacity-40"
			title="Download the full buffer as JSON Lines"
		>.jsonl</button>
		<button
			type="button"
			onclick={clearWireLog}
			disabled={$wireTotal === 0}
			class="cursor-pointer rounded bg-gray-700 px-2 py-0.5 text-white hover:bg-gray-600 disabled:cursor-not-allowed disabled:opacity-40"
		>Clear</button>
	</div>

	<div
		bind:this={streamEl}
		onscroll={onScroll}
		class="flex-1 overflow-auto bg-gray-950/80 px-2 py-1 font-mono text-[11px] leading-relaxed"
	>
		{#if shown.length === 0}
			<p class="p-2 italic text-gray-600">
				No frames yet — every packet (start_session, control, telemetry, camera frames…) appears here verbatim.
			</p>
		{:else}
			{#each shown as p (p.i)}
				<div class="grid grid-cols-[78px_26px_1fr] items-baseline gap-2 whitespace-pre-wrap break-all px-1 hover:bg-white/5">
					<span class="text-[10px] text-gray-600 tabular-nums">{p.t}</span>
					<span class="text-[10px] font-bold {p.kind === 'evt' ? 'text-amber-400' : p.dir === 'TX' ? 'text-blue-400' : 'text-emerald-400'}">{p.dir === 'EVT' ? '•' : p.dir}</span>
					<span class={rowClass(p)}>{p.kind === 'bin' ? `${p.data} (${p.bytes} B)` : p.data}</span>
				</div>
			{/each}
		{/if}
	</div>
</div>
