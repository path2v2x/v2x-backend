<script lang="ts">
	import {
		driveConnected,
		resetTeleportStatus,
		sessionState,
		telemetry,
		teleportStatus,
		teleportVehicle,
		TELEPORT_COORD_ABS_LIMIT_M,
		TELEPORT_MAX_ABS_YAW_DEG,
		TELEPORT_MAX_Z_M,
		TELEPORT_MIN_Z_M
	} from '$lib/stores/driveSocket';

	interface Props {
		onClose: () => void;
	}

	let { onClose }: Props = $props();

	let x = $state<number | undefined>(undefined);
	let y = $state<number | undefined>(undefined);
	let z = $state<number | undefined>(undefined);
	let yaw = $state<number | undefined>(undefined);

	let pending = $derived($teleportStatus.state === 'pending');
	let sessionActive = $derived($driveConnected && $sessionState === 'driving');
	let validCoordinates = $derived(
		typeof x === 'number'
			&& Number.isFinite(x)
			&& Math.abs(x) <= TELEPORT_COORD_ABS_LIMIT_M
			&& typeof y === 'number'
			&& Number.isFinite(y)
			&& Math.abs(y) <= TELEPORT_COORD_ABS_LIMIT_M
	);

	function optionalNumber(value: number | undefined): number | undefined {
		return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
	}

	function apply(event: SubmitEvent) {
		event.preventDefault();
		if (x === undefined || y === undefined) return;
		teleportVehicle(x, y, optionalNumber(z), optionalNumber(yaw));
	}

	function useCurrent() {
		const current = $telemetry.pos;
		x = Math.round(current[0] * 10) / 10;
		y = Math.round(current[1] * 10) / 10;
		z = Math.round(current[2] * 10) / 10;
		resetTeleportStatus();
	}

	function clearSettledStatus() {
		if (!pending) resetTeleportStatus();
	}
</script>

<div
	class="absolute bottom-16 right-2 z-30 flex w-72 flex-col overflow-hidden rounded-xl border border-gray-700 bg-gray-900/95 text-gray-200 shadow-xl backdrop-blur-md pointer-events-auto"
	data-testid="teleport-panel"
>
	<div class="flex items-center justify-between border-b border-gray-700 px-3 py-2">
		<span class="text-sm font-semibold tracking-wide text-emerald-400">Teleport</span>
		<button
			type="button"
			onclick={onClose}
			class="cursor-pointer px-1 text-xl leading-none text-gray-400 hover:text-white"
			aria-label="Close teleport panel"
		>×</button>
	</div>

	<form class="flex flex-col gap-2 p-3 text-xs" onsubmit={apply}>
		<p class="leading-relaxed text-gray-400">
			Move this session's ego car to a world coordinate. Leave
			<span class="font-mono text-gray-300">Z</span> blank to snap to the nearest road.
		</p>

		<label class="flex items-center gap-2">
			<span class="w-9 font-mono text-gray-400">X</span>
			<input
				type="number"
				bind:value={x}
				oninput={clearSettledStatus}
				min={-TELEPORT_COORD_ABS_LIMIT_M}
				max={TELEPORT_COORD_ABS_LIMIT_M}
				step="any"
				required
				disabled={pending}
				aria-label="Teleport X coordinate"
				class="flex-1 rounded border border-gray-600 bg-gray-800 px-2 py-1 font-mono text-gray-100 focus:border-emerald-500 focus:outline-none disabled:opacity-60"
			/>
		</label>
		<label class="flex items-center gap-2">
			<span class="w-9 font-mono text-gray-400">Y</span>
			<input
				type="number"
				bind:value={y}
				oninput={clearSettledStatus}
				min={-TELEPORT_COORD_ABS_LIMIT_M}
				max={TELEPORT_COORD_ABS_LIMIT_M}
				step="any"
				required
				disabled={pending}
				aria-label="Teleport Y coordinate"
				class="flex-1 rounded border border-gray-600 bg-gray-800 px-2 py-1 font-mono text-gray-100 focus:border-emerald-500 focus:outline-none disabled:opacity-60"
			/>
		</label>
		<label class="flex items-center gap-2">
			<span class="w-9 font-mono text-gray-400">Z</span>
			<input
				type="number"
				bind:value={z}
				oninput={clearSettledStatus}
				min={TELEPORT_MIN_Z_M}
				max={TELEPORT_MAX_Z_M}
				step="any"
				placeholder="auto (road)"
				disabled={pending}
				aria-label="Teleport Z coordinate"
				class="flex-1 rounded border border-gray-600 bg-gray-800 px-2 py-1 font-mono text-gray-100 placeholder:text-gray-600 focus:border-emerald-500 focus:outline-none disabled:opacity-60"
			/>
		</label>
		<label class="flex items-center gap-2">
			<span class="w-9 font-mono text-gray-400">Yaw°</span>
			<input
				type="number"
				bind:value={yaw}
				oninput={clearSettledStatus}
				min={-TELEPORT_MAX_ABS_YAW_DEG}
				max={TELEPORT_MAX_ABS_YAW_DEG}
				step="any"
				placeholder="keep"
				disabled={pending}
				aria-label="Teleport yaw"
				class="flex-1 rounded border border-gray-600 bg-gray-800 px-2 py-1 font-mono text-gray-100 placeholder:text-gray-600 focus:border-emerald-500 focus:outline-none disabled:opacity-60"
			/>
		</label>

		<div class="mt-1 flex gap-2">
			<button
				type="button"
				onclick={useCurrent}
				disabled={pending}
				class="flex-1 cursor-pointer rounded bg-gray-700 px-2 py-1.5 text-white hover:bg-gray-600 disabled:cursor-wait disabled:opacity-60"
				title="Fill with the car's current position"
			>Use current</button>
			<button
				type="submit"
				disabled={!sessionActive || !validCoordinates || pending}
				class="flex-1 cursor-pointer rounded bg-emerald-600 px-2 py-1.5 font-medium text-white hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-gray-700 disabled:text-gray-400"
			> {pending ? 'Waiting…' : 'Teleport'} </button>
		</div>

		{#if !sessionActive}
			<p class="mt-0.5 text-amber-300" data-testid="teleport-session-warning">
				Start an active drive session before teleporting.
			</p>
		{:else if $teleportStatus.message}
			<p
				class="mt-0.5 font-mono {$teleportStatus.state === 'error' ? 'text-red-300' : $teleportStatus.state === 'succeeded' ? 'text-emerald-400' : 'text-amber-300'}"
				role={$teleportStatus.state === 'error' ? 'alert' : 'status'}
				aria-live="polite"
				data-testid="teleport-status"
			>
				{$teleportStatus.message}
			</p>
		{/if}
	</form>
</div>
