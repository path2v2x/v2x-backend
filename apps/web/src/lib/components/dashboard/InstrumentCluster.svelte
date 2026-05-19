<script lang="ts">
	import SpeedDisplay from './SpeedDisplay.svelte';
	import GearColumn, { type Gear } from './GearColumn.svelte';
	import ThrottleBrakeBars from './ThrottleBrakeBars.svelte';

	interface Props {
		/** Speed in km/h (matches bridge telemetry). */
		speed: number;
		/** Active gear (P/R/N/D). */
		gear: Gear;
		/** Throttle 0-1. */
		throttle: number;
		/** Brake 0-1. */
		brake: number;
		/** Steering input in [-1, 1] (CARLA normalized). */
		steer: number;
		/** Display unit for speed. */
		speedUnit?: 'mph' | 'kmh';
	}

	let {
		speed = 0,
		gear = 'D',
		throttle = 0,
		brake = 0,
		steer = 0,
		speedUnit = 'mph',
	}: Props = $props();

	const steerDegrees = $derived(Math.round(steer * 450));
	const steerDisplay = $derived(
		steerDegrees === 0
			? '0°'
			: `${steerDegrees > 0 ? '+' : ''}${steerDegrees}°`
	);

	// Live clock — matches Tesla cluster pattern.
	let now = $state(new Date());
	$effect(() => {
		const id = setInterval(() => {
			now = new Date();
		}, 30_000);
		return () => clearInterval(id);
	});
	const clock = $derived(
		now.toLocaleTimeString('en-US', {
			hour: 'numeric',
			minute: '2-digit',
		})
	);
</script>

<div
	class="relative flex h-full w-full font-tesla overflow-hidden"
	style="
		background:
			radial-gradient(ellipse at 30% 0%, rgba(62, 130, 247, 0.06) 0%, transparent 55%),
			linear-gradient(180deg, #0a0a0c 0%, #14171c 100%);
	"
	data-testid="instrument-cluster"
>
	<!-- Top edge highlight (like a screen recessed into the cabin) -->
	<div
		class="absolute top-0 left-0 right-0 h-px pointer-events-none"
		style="background: linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.18) 50%, transparent 100%);"
		aria-hidden="true"
	></div>

	<!-- Top-left chrome: clock -->
	<div
		class="absolute top-2.5 left-4 z-10 flex items-center gap-2"
		aria-hidden="true"
	>
		<span
			class="text-[10px] font-medium uppercase tracking-[0.28em] tabular-nums"
			style="color: var(--color-tesla-text-muted); font-feature-settings: 'tnum';"
		>
			{clock}
		</span>
	</div>

	<!-- Top-right chrome: drive-mode tag -->
	<div
		class="absolute top-2.5 right-4 z-10 flex items-center gap-2"
		aria-hidden="true"
	>
		<span
			class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-[0.22em]"
			style="
				color: var(--color-tesla-text-secondary);
				background: rgba(255, 255, 255, 0.04);
				border: 1px solid rgba(255, 255, 255, 0.08);
			"
		>
			<span
				class="inline-block w-1.5 h-1.5 rounded-full"
				style="background: var(--color-tesla-active); box-shadow: 0 0 6px var(--color-tesla-active);"
			></span>
			Drive
		</span>
	</div>

	<!-- Main row: pedals/speed on the left, gear + steer on the right -->
	<div class="flex items-center justify-between w-full h-full px-6 sm:px-10 pt-6 pb-5">
		<div class="flex items-center gap-5">
			<ThrottleBrakeBars {throttle} {brake} height="6.5rem" />
			<SpeedDisplay {speed} unit={speedUnit} />
		</div>

		<div class="flex flex-col items-end gap-4">
			<GearColumn active={gear} orientation="row" />
			<div
				class="flex items-center gap-2 text-[10px] uppercase tracking-[0.22em] font-medium"
				style="color: var(--color-tesla-text-secondary);"
				data-testid="steer-readout"
			>
				<svg
					width="14"
					height="14"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					stroke-width="2"
					stroke-linecap="round"
					stroke-linejoin="round"
					style="transform: rotate({steerDegrees * 0.4}deg); transition: transform 80ms linear;"
					aria-hidden="true"
				>
					<circle cx="12" cy="12" r="9" />
					<line x1="12" y1="3" x2="12" y2="6" />
					<line x1="3" y1="12" x2="6" y2="12" />
					<line x1="21" y1="12" x2="18" y2="12" />
					<line x1="12" y1="21" x2="12" y2="18" />
				</svg>
				<span
					class="tabular-nums"
					style="color: var(--color-tesla-text); font-feature-settings: 'tnum';"
					data-testid="steer-degrees"
				>
					{steerDisplay}
				</span>
			</div>
		</div>
	</div>
</div>
