<script lang="ts">
	/**
	 * Store-connected DriverDashboard.
	 *
	 * Subscribes to the driveSocket and v2xZones stores, runs the warning
	 * translation, and feeds the result into the props-based
	 * DriverDashboard. This wrapper lives in src/lib so it can be imported
	 * from +page.svelte while keeping the inner component testable in
	 * isolation.
	 */

	import { onMount, onDestroy } from 'svelte';
	import type { XoscFinishedEvent } from '$lib/types';
	import {
		telemetry,
		v2xAlerts,
		xoscLastResult,
	} from '$lib/stores/driveSocket';
	import { activeZoneAlerts } from '$lib/stores/v2xZones';
	import DriverDashboard from './DriverDashboard.svelte';
	import { buildDashboardWarnings } from './warnings';

	interface Props {
		/** Speed display unit. */
		speedUnit?: 'mph' | 'kmh';
	}

	let { speedUnit = 'mph' }: Props = $props();

	let now = $state(Date.now());
	let xoscResultSetAt = $state<number | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		timer = setInterval(() => {
			now = Date.now();
		}, 200);
	});

	onDestroy(() => {
		if (timer != null) clearInterval(timer);
	});

	// Mark the moment a new verdict arrives so we can apply the TTL window.
	let lastSeenVerdict: XoscFinishedEvent | null = null;
	$effect(() => {
		const current = $xoscLastResult;
		if (current && current !== lastSeenVerdict) {
			xoscResultSetAt = Date.now();
			lastSeenVerdict = current;
		} else if (!current && lastSeenVerdict !== null) {
			xoscResultSetAt = null;
			lastSeenVerdict = null;
		}
	});

	const warnings = $derived(
		buildDashboardWarnings({
			v2xAlerts: $v2xAlerts,
			activeZoneAlerts: $activeZoneAlerts,
			xoscLastResult: $xoscLastResult,
			xoscResultSetAt,
			now,
			detections: $telemetry.detections,
		})
	);
</script>

<DriverDashboard
	telemetry={$telemetry}
	{warnings}
	{now}
	{speedUnit}
/>
