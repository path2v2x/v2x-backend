<script lang="ts" module>
	import type { Gear } from './InstrumentCluster.svelte';
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
		/** All active warnings (V2X, EVA, scenario verdict, etc.). */
		warnings?: DashboardWarning[];
		/** Override `now` for tests. */
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
</script>

<div
	class="relative flex w-full h-full font-tesla overflow-hidden"
	style="
		background:
			radial-gradient(ellipse at 50% -10%, rgba(62, 130, 247, 0.10) 0%, transparent 60%),
			linear-gradient(180deg,
				rgba(8, 9, 12, 0.78) 0%,
				rgba(14, 16, 21, 0.86) 50%,
				rgba(20, 23, 28, 0.92) 100%);
		backdrop-filter: blur(14px) saturate(120%);
		-webkit-backdrop-filter: blur(14px) saturate(120%);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-top: 1px solid rgba(255, 255, 255, 0.18);
		/* Arched binnacle-pod shape: top edge curves up like an instrument cluster cowl */
		border-radius: 36% 36% 18px 18px / 60% 60% 18px 18px;
		box-shadow:
			0 -1px 0 rgba(255, 255, 255, 0.06),
			0 8px 28px rgba(0, 0, 0, 0.55),
			0 24px 48px -16px rgba(0, 0, 0, 0.7),
			inset 0 1px 0 rgba(255, 255, 255, 0.06),
			inset 0 12px 28px -16px rgba(0, 0, 0, 0.7);
	"
	data-testid="driver-dashboard"
>
	<!-- Top recessed-screen highlight: thin glowing arc along the cowl edge -->
	<div
		class="absolute top-0 left-[8%] right-[8%] h-[2px] pointer-events-none"
		style="
			background: linear-gradient(90deg,
				transparent 0%,
				rgba(255, 255, 255, 0.10) 15%,
				rgba(62, 130, 247, 0.32) 50%,
				rgba(255, 255, 255, 0.10) 85%,
				transparent 100%);
			filter: blur(0.4px);
		"
		aria-hidden="true"
	></div>

	<!-- Left: instrument cluster -->
	<div class="shrink-0" style="width: 60%; min-width: 0;">
		<InstrumentCluster {speed} {gear} {throttle} {brake} {steer} {speedUnit} />
	</div>

	<!-- Center divider with subtle glow -->
	<div
		class="shrink-0 self-stretch w-px relative"
		style="background: linear-gradient(180deg, transparent 0%, rgba(255,255,255,0.18) 50%, transparent 100%);"
		aria-hidden="true"
	>
		<div
			class="absolute inset-y-2 left-0 w-px"
			style="
				background: linear-gradient(180deg, transparent 0%, rgba(62, 130, 247, 0.55) 50%, transparent 100%);
				filter: blur(2px);
			"
		></div>
	</div>

	<!-- Right: messages only -->
	<div class="grow relative" style="min-width: 0;">
		<CenterStack {warnings} now={effectiveNow} />
	</div>
</div>
