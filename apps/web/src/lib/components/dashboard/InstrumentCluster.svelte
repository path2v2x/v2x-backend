<script lang="ts">
	import ThrottleGauge, { type Gear } from './ThrottleGauge.svelte';
	import SteeringPath from './SteeringPath.svelte';

	interface Props {
		speed: number;
		gear: Gear;
		throttle: number;
		steer: number;
		speedUnit?: 'mph' | 'kmh';
	}

	let {
		speed = 0,
		gear = 'D',
		throttle = 0,
		steer = 0,
		speedUnit = 'mph',
	}: Props = $props();
</script>

<div
	class="relative flex h-full font-tesla overflow-hidden items-center gap-1 px-3"
	style="
		background:
			radial-gradient(ellipse at 50% 0%, rgba(62, 130, 247, 0.06) 0%, transparent 55%);
	"
	data-testid="instrument-cluster"
>
	<!-- LEFT: circular gas-car-style throttle gauge with speed + gear in centre -->
	<ThrottleGauge {throttle} {speed} {gear} unit={speedUnit} size={86} />

	<!-- RIGHT: Tesla-style path-prediction visualization (snug against the gauge) -->
	<SteeringPath {steer} width={86} height={82} />
</div>
