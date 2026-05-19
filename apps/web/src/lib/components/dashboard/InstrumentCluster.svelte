<script lang="ts">
	import SpeedDisplay from './SpeedDisplay.svelte';
	import GearColumn, { type Gear } from './GearColumn.svelte';
	import ThrottleBrakeBars from './ThrottleBrakeBars.svelte';
	import BatteryRange from './BatteryRange.svelte';
	import SpeedLimit from './SpeedLimit.svelte';
	import TurnSignal from './TurnSignal.svelte';

	interface Props {
		speed: number;
		gear: Gear;
		throttle: number;
		brake: number;
		steer: number;
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

	// Derive turn signal from steer magnitude — visual chrome only.
	const turnDirection = $derived<'left' | 'right' | null>(
		steer < -0.25 ? 'left' : steer > 0.25 ? 'right' : null
	);

	// Live clock
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

	// Trip mock — accumulate while moving so it feels live. The accumulator
	// resets on page reload; that's fine for a sim chrome readout.
	let tripMi = $state(0);
	let lastTickMs = $state(performance.now());
	$effect(() => {
		const id = setInterval(() => {
			const t = performance.now();
			const dtSec = Math.min(0.5, (t - lastTickMs) / 1000);
			lastTickMs = t;
			// speed (km/h) → mph → miles per second
			tripMi += (speed * 0.6213711922 / 3600) * dtSec;
		}, 250);
		return () => clearInterval(id);
	});
	const tripDisplay = $derived(tripMi.toFixed(1));
</script>

<div
	class="relative flex flex-col h-full w-full font-tesla overflow-hidden"
	style="
		background:
			radial-gradient(ellipse at 30% 0%, rgba(62, 130, 247, 0.08) 0%, transparent 55%),
			linear-gradient(180deg, #0a0a0c 0%, #14171c 100%);
	"
	data-testid="instrument-cluster"
>
	<!-- Top recessed-screen highlight -->
	<div
		class="absolute top-0 left-0 right-0 h-px pointer-events-none"
		style="background: linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.18) 50%, transparent 100%);"
		aria-hidden="true"
	></div>

	<!-- ── Top chrome bar ── -->
	<div class="flex items-center justify-between px-4 pt-2.5 text-[10px] uppercase tracking-[0.24em]">
		<div class="flex items-center gap-3">
			<span
				class="font-medium tabular-nums"
				style="color: var(--color-tesla-text-secondary); font-feature-settings: 'tnum';"
			>
				{clock}
			</span>
			<span
				class="tabular-nums"
				style="color: var(--color-tesla-text-muted); font-feature-settings: 'tnum';"
			>
				72°F
			</span>
		</div>

		<TurnSignal direction={turnDirection} />

		<div class="flex items-center gap-2">
			<span
				class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-medium tracking-[0.22em]"
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
			<span
				class="px-2 py-0.5 rounded-full text-[9px] tracking-[0.2em]"
				style="
					color: var(--color-tesla-text-muted);
					background: rgba(255, 255, 255, 0.02);
					border: 1px solid rgba(255, 255, 255, 0.05);
				"
			>
				Standard
			</span>
		</div>
	</div>

	<!-- ── Main row ── -->
	<div class="flex flex-1 items-center justify-between px-5 sm:px-7 pt-1 pb-1 gap-3">
		<!-- LEFT: pedal bars + speed + speed limit -->
		<div class="flex items-center gap-4">
			<ThrottleBrakeBars {throttle} {brake} height="5.5rem" />
			<SpeedDisplay {speed} unit={speedUnit} />
			<div class="hidden sm:block">
				<SpeedLimit limit={null} unit={speedUnit} />
			</div>
		</div>

		<!-- RIGHT: gear / steer / battery -->
		<div class="flex items-center gap-5">
			<div class="flex flex-col items-end gap-3">
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

			<BatteryRange />
		</div>
	</div>

	<!-- ── Bottom chrome bar: trip stats ── -->
	<div
		class="flex items-center justify-between px-5 pb-2 text-[9px] uppercase tracking-[0.24em]"
		style="color: var(--color-tesla-text-muted); border-top: 1px solid rgba(255, 255, 255, 0.04);"
	>
		<div class="flex gap-5 pt-1.5">
			<span class="tabular-nums" style="font-feature-settings: 'tnum';">
				<span class="opacity-60">Trip</span>
				<span style="color: var(--color-tesla-text-secondary);"> {tripDisplay} mi</span>
			</span>
			<span class="tabular-nums" style="font-feature-settings: 'tnum';">
				<span class="opacity-60">Odo</span>
				<span style="color: var(--color-tesla-text-secondary);"> 24,182 mi</span>
			</span>
		</div>
		<span class="tabular-nums pt-1.5" style="font-feature-settings: 'tnum';">
			<span class="opacity-60">Avg</span>
			<span style="color: var(--color-tesla-text-secondary);">
				{Math.round(speed * 0.6213711922)} mph
			</span>
		</span>
	</div>
</div>
