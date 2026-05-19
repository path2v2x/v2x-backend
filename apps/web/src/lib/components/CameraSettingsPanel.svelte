<script lang="ts">
	import { setCameraSettings, cameraAspect } from '$lib/stores/driveSocket';

	interface Props {
		onClose: () => void;
	}

	let { onClose }: Props = $props();

	type Preset = {
		id: string;
		label: string;
		ratio: string;
		width: number;
		height: number;
	};

	const ASPECT_PRESETS: Preset[] = [
		{ id: 'widescreen', label: 'Widescreen', ratio: '16:9',  width: 960, height: 540 },
		{ id: 'square',     label: 'Square',     ratio: '1:1',   width: 720, height: 720 },
		{ id: 'classic',    label: 'Classic',    ratio: '4:3',   width: 800, height: 600 },
		{ id: 'portrait',   label: 'Portrait',   ratio: '3:4',   width: 600, height: 800 },
		{ id: 'cinema',     label: 'Cinema',     ratio: '21:9',  width: 1008, height: 432 },
	];

	const FOV_PRESETS = [
		{ label: 'Narrow', value: 60 },
		{ label: 'Default', value: 90 },
		{ label: 'Wide',  value: 110 },
	];

	let activeAspect = $state<string>('square');
	let activeFov = $state<number>(90);
	let busy = $state(false);

	function applyAspect(preset: Preset) {
		if (busy) return;
		busy = true;
		activeAspect = preset.id;
		// Drive the visible viewport shape (frontend) and the CARLA capture
		// resolution (bridge) from the same preset.
		cameraAspect.set({ w: preset.width, h: preset.height });
		setCameraSettings({
			image_size_x: preset.width,
			image_size_y: preset.height,
		});
		setTimeout(() => { busy = false; }, 600);
	}

	function applyFov(value: number) {
		if (busy) return;
		busy = true;
		activeFov = value;
		setCameraSettings({ fov: value });
		setTimeout(() => { busy = false; }, 600);
	}
</script>

<div class="absolute bottom-16 left-2 z-30 w-64 bg-gray-900/95 border border-gray-700 rounded-xl overflow-hidden pointer-events-auto flex flex-col">
	<!-- Header -->
	<div class="p-2.5 border-b border-gray-700 flex items-center justify-between">
		<span class="text-xs font-semibold text-white tracking-wider uppercase">Camera</span>
		<button onclick={onClose}
			class="px-2 py-0.5 bg-gray-700 hover:bg-gray-600 rounded text-xs text-gray-300">
			X
		</button>
	</div>

	<!-- Aspect ratio presets -->
	<div class="p-2 flex flex-col gap-1">
		<span class="text-[10px] font-body text-gray-500 tracking-widest uppercase mb-0.5">Aspect Ratio</span>
		{#each ASPECT_PRESETS as p}
			<button
				onclick={() => applyAspect(p)}
				disabled={busy}
				class="flex items-center justify-between px-3 py-2 rounded text-left transition-colors
					{activeAspect === p.id
						? 'bg-cyan-600/30 border border-cyan-500/50 text-white'
						: 'bg-gray-800 hover:bg-gray-700 text-gray-300 border border-transparent'}
					{busy ? 'opacity-60 cursor-wait' : 'cursor-pointer'}"
			>
				<div class="flex items-center gap-2">
					<!-- Visual aspect ratio indicator -->
					<div class="flex items-center justify-center w-8 h-5">
						<div class="bg-gray-600" style="width: {Math.min(p.width / p.height, 1.6) * 18}px; height: {Math.min(p.height / p.width, 1.6) * 18}px; min-width: 8px; min-height: 8px;"></div>
					</div>
					<div class="flex flex-col">
						<span class="text-xs font-medium">{p.label}</span>
						<span class="text-[10px] text-gray-500">{p.ratio}</span>
					</div>
				</div>
				<span class="text-[10px] font-mono text-gray-500">{p.width}×{p.height}</span>
			</button>
		{/each}
	</div>

	<!-- FOV presets -->
	<div class="p-2 border-t border-gray-700">
		<span class="text-[10px] font-body text-gray-500 tracking-widest uppercase mb-1 block">Field of View</span>
		<div class="flex gap-1">
			{#each FOV_PRESETS as f}
				<button
					onclick={() => applyFov(f.value)}
					disabled={busy}
					class="flex-1 px-2 py-1.5 rounded text-[11px] transition-colors
						{activeFov === f.value
							? 'bg-cyan-600/30 border border-cyan-500/50 text-white'
							: 'bg-gray-800 hover:bg-gray-700 text-gray-300 border border-transparent'}
						{busy ? 'opacity-60 cursor-wait' : 'cursor-pointer'}"
				>
					<div>{f.label}</div>
					<div class="text-[9px] text-gray-500">{f.value}°</div>
				</button>
			{/each}
		</div>
	</div>

	<!-- Footer note -->
	<div class="px-2.5 py-1.5 border-t border-gray-700">
		<span class="text-[10px] text-gray-500">Resizing respawns the sensor (~0.5s pause)</span>
	</div>
</div>
