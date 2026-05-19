<script lang="ts" module>
	import type { NearbyActor as TelemetryNearbyActor } from '$lib/types';
	import type { Gear } from './GearColumn.svelte';
	import type { DashboardWarning } from './WarningStack.svelte';

	export type { DashboardWarning };

	/** Convert CARLA's gear int to a Tesla-style P/R/N/D letter.
	 * CARLA: positive = forward, 0 = neutral, negative = reverse. */
	export function gearFromCarla(gear: number): Gear {
		if (gear > 0) return 'D';
		if (gear < 0) return 'R';
		return 'N';
	}
</script>

<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import type { VehicleTelemetry } from '$lib/types';
	import InstrumentCluster from './InstrumentCluster.svelte';
	import CenterStack from './CenterStack.svelte';

	interface Props {
		/** Latest telemetry from the bridge. May be null on initial mount. */
		telemetry: VehicleTelemetry | null;
		/** All active warnings (V2X, EVA, scenario, verdict, etc.). */
		warnings?: DashboardWarning[];
		/** Override `now` for tests (skips the internal ticker). */
		now?: number;
		/** Speed display unit. */
		speedUnit?: 'mph' | 'kmh';
	}

	let { telemetry, warnings = [], now, speedUnit = 'mph' }: Props = $props();

	let tick = $state(Date.now());
	let timerId: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		if (now === undefined) {
			timerId = setInterval(() => {
				tick = Date.now();
			}, 150);
		}
	});

	onDestroy(() => {
		if (timerId != null) clearInterval(timerId);
	});

	const effectiveNow = $derived(now ?? tick);

	const speed = $derived(telemetry?.speed ?? 0);
	const gear = $derived(gearFromCarla(telemetry?.gear ?? 1));
	const throttle = $derived(telemetry?.throttle ?? 0);
	const brake = $derived(telemetry?.brake ?? 0);
	const steer = $derived(telemetry?.steer ?? 0);
	const egoPos = $derived<[number, number]>(
		telemetry ? [telemetry.pos[0], telemetry.pos[1]] : [0, 0]
	);
	const egoYaw = $derived(telemetry?.rot?.[1] ?? 0);
	const nearby = $derived<TelemetryNearbyActor[]>(
		telemetry?.nearby_actors ?? []
	);
</script>

<div
	class="relative flex w-full h-full font-tesla overflow-hidden"
	style="
		background: linear-gradient(180deg, #050608 0%, #0a0a0c 25%, #14171c 100%);
		box-shadow:
			inset 0 1px 0 rgba(255, 255, 255, 0.04),
			inset 0 14px 28px -16px rgba(0, 0, 0, 0.85);
	"
	data-testid="driver-dashboard"
>
	<!-- Top recessed-screen highlight -->
	<div
		class="absolute top-0 left-0 right-0 h-[2px] pointer-events-none"
		style="
			background: linear-gradient(90deg,
				transparent 0%,
				rgba(255, 255, 255, 0.08) 18%,
				rgba(62, 130, 247, 0.28) 50%,
				rgba(255, 255, 255, 0.08) 82%,
				transparent 100%);
		"
		aria-hidden="true"
	></div>

	<!-- Left: instrument cluster -->
	<div class="shrink-0" style="width: 50%; min-width: 0;">
		<InstrumentCluster {speed} {gear} {throttle} {brake} {steer} {speedUnit} />
	</div>

	<!-- Center divider with subtle glow -->
	<div
		class="shrink-0 self-stretch w-px relative"
		style="background: linear-gradient(180deg, transparent 0%, rgba(255,255,255,0.18) 50%, transparent 100%);"
		aria-hidden="true"
	>
		<div
			class="absolute inset-y-4 left-0 w-px"
			style="
				background: linear-gradient(180deg, transparent 0%, rgba(62, 130, 247, 0.55) 50%, transparent 100%);
				filter: blur(2px);
			"
		></div>
	</div>

	<!-- Right: center stack (viz + warnings) -->
	<div class="grow relative" style="min-width: 0;">
		<CenterStack
			{egoPos}
			{egoYaw}
			{steer}
			{nearby}
			{warnings}
			now={effectiveNow}
		/>
	</div>
</div>
