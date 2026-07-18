<script lang="ts" module>
	import type { Gear } from './ThrottleGauge.svelte';
	import type { DashboardWarning } from './WarningStack.svelte';

	export type { DashboardWarning };

	/** Convert CARLA's gear int to a Tesla-style P/R/N/D letter.
	 * CARLA: positive = forward, 0 = neutral, negative = reverse. */
	export function gearFromCarla(g: number): Gear {
		if (g > 0) return 'D';
		if (g < 0) return 'R';
		return 'N';
	}
</script>

<script lang="ts">
	import type { VehicleTelemetry } from '$lib/types';
	import InstrumentCluster from './InstrumentCluster.svelte';
	import PerceptionPanel from './PerceptionPanel.svelte';
	import WarningStack from './WarningStack.svelte';

	interface Props {
		/** Latest telemetry from the bridge. May be null on initial mount. */
		telemetry: VehicleTelemetry | null;
		/** All active warnings (V2X, EVA, scenario verdict, etc.). */
		warnings?: DashboardWarning[];
		/** Current time in ms for warning fade-out decisions. Required —
		 * the connected wrapper supplies a live-ticking value. */
		now: number;
		/** Speed display unit. */
		speedUnit?: 'mph' | 'kmh';
	}

	let { telemetry, warnings = [], now, speedUnit = 'mph' }: Props = $props();

	const speed = $derived(telemetry?.speed ?? 0);
	const gear = $derived(gearFromCarla(telemetry?.gear ?? 1));
	const throttle = $derived(telemetry?.throttle ?? 0);
	const brake = $derived(telemetry?.brake ?? 0);
	const steer = $derived(telemetry?.steer ?? 0);
	const detections = $derived(telemetry?.detections ?? []);
</script>

<div
	class="relative flex w-full h-full font-tesla overflow-hidden"
	style="
		background:
			radial-gradient(ellipse at 50% 0%, rgba(62, 130, 247, 0.08) 0%, transparent 60%),
			linear-gradient(180deg,
				rgba(8, 9, 12, 0.25) 0%,
				rgba(14, 16, 21, 0.32) 50%,
				rgba(20, 23, 28, 0.38) 100%);
		backdrop-filter: blur(16px) saturate(130%);
		-webkit-backdrop-filter: blur(16px) saturate(130%);
		border: 1px solid rgba(255, 255, 255, 0.10);
		border-top: 1px solid rgba(255, 255, 255, 0.18);
		border-radius: 14px;
		box-shadow:
			0 8px 24px rgba(0, 0, 0, 0.45),
			0 18px 36px -16px rgba(0, 0, 0, 0.55),
			inset 0 1px 0 rgba(255, 255, 255, 0.08);
	"
	data-testid="driver-dashboard"
>
	<!-- Top edge highlight: thin glowing line -->
	<div
		class="absolute top-0 left-[10%] right-[10%] h-px pointer-events-none"
		style="
			background: linear-gradient(90deg,
				transparent 0%,
				rgba(255, 255, 255, 0.10) 18%,
				rgba(62, 130, 247, 0.28) 50%,
				rgba(255, 255, 255, 0.10) 82%,
				transparent 100%);
		"
		aria-hidden="true"
	></div>

	<!-- Left: instrument cluster — sized to its content, no wasted width -->
	<div class="shrink-0">
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

	<!-- Right: perception viz with warning cards overlaying it -->
	<div class="grow relative overflow-hidden" style="min-width: 0;">
		<PerceptionPanel {detections} />
		<div class="absolute inset-0 pointer-events-none">
			<WarningStack {warnings} {now} />
		</div>
	</div>
</div>
