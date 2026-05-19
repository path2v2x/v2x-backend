<script lang="ts">
	import VehicleVisualization, {
		type NearbyActor,
	} from './VehicleVisualization.svelte';
	import WarningStack, {
		type DashboardWarning,
	} from './WarningStack.svelte';

	interface Props {
		egoPos: [number, number];
		egoYaw: number;
		steer: number;
		nearby?: NearbyActor[];
		warnings?: DashboardWarning[];
		/** Override `now` for tests. */
		now?: number;
	}

	let {
		egoPos = [0, 0],
		egoYaw = 0,
		steer = 0,
		nearby = [],
		warnings = [],
		now = Date.now(),
	}: Props = $props();
</script>

<div
	class="relative w-full h-full overflow-hidden"
	style="background: var(--color-tesla-bg);"
	data-testid="center-stack"
>
	<!-- Bottom layer: traffic visualization (always present, like Tesla's driver block). -->
	<div class="absolute inset-0">
		<VehicleVisualization {egoPos} {egoYaw} {steer} {nearby} />
	</div>

	<!-- Top layer: warning cards stack down from the top edge.
	     Transparent container — viz is visible through gaps. -->
	<div class="absolute inset-0">
		<WarningStack {warnings} {now} />
	</div>
</div>
