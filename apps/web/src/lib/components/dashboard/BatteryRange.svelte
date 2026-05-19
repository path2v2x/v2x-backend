<script lang="ts">
	interface Props {
		/** Battery percentage 0-100. Mocked in the sim — defaults to 82%. */
		percent?: number;
		/** Estimated range in selected unit. Mocked. */
		range?: number;
		unit?: 'mi' | 'km';
	}

	let { percent = 82, range = 312, unit = 'mi' }: Props = $props();

	const clamped = $derived(Math.max(0, Math.min(100, percent)));

	// Color ramp: green > yellow > red as battery depletes (Tesla-style).
	const fillColor = $derived(
		clamped > 30
			? 'var(--color-tesla-active)'
			: clamped > 15
				? 'var(--color-tesla-warning)'
				: 'var(--color-tesla-critical)'
	);
</script>

<div
	class="flex flex-col items-center gap-2 font-tesla select-none"
	data-testid="battery-widget"
>
	<!-- Vertical battery bar with cap on top -->
	<div class="flex flex-col items-center gap-0.5">
		<!-- Battery cap -->
		<div
			class="rounded-t-[2px]"
			style="
				width: 12px;
				height: 4px;
				background: var(--color-tesla-divider);
				border: 1px solid rgba(255, 255, 255, 0.1);
				border-bottom: none;
			"
		></div>
		<!-- Battery body -->
		<div
			class="relative rounded-md overflow-hidden"
			style="
				width: 22px;
				height: 80px;
				background: linear-gradient(180deg, rgba(58, 63, 71, 0.5) 0%, rgba(20, 23, 28, 0.7) 100%);
				border: 1px solid rgba(255, 255, 255, 0.08);
				box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.6);
			"
			role="meter"
			aria-label="Battery"
			aria-valuenow={clamped}
			aria-valuemin="0"
			aria-valuemax="100"
		>
			<div
				class="absolute bottom-0 left-0 right-0 transition-[height] duration-500 ease-out rounded-b-md"
				style="
					height: {clamped}%;
					background: linear-gradient(180deg, {fillColor} 0%, {fillColor} 60%, rgba(0,0,0,0.0) 100%);
					box-shadow: 0 0 12px {fillColor};
				"
				data-testid="battery-fill"
				data-fill={clamped.toFixed(0)}
			></div>
			<!-- Tick marks (4 levels) -->
			{#each [0.25, 0.5, 0.75] as t}
				<div
					class="absolute left-0 right-0 pointer-events-none"
					style="
						bottom: {t * 100}%;
						height: 1px;
						background: rgba(255, 255, 255, 0.06);
					"
				></div>
			{/each}
		</div>
	</div>

	<!-- Percent + range readout -->
	<div class="flex flex-col items-center gap-0">
		<span
			class="font-semibold text-base tabular-nums leading-none"
			style="color: var(--color-tesla-text); font-feature-settings: 'tnum';"
			data-testid="battery-percent"
		>
			{clamped.toFixed(0)}<span class="text-xs font-medium" style="color: var(--color-tesla-text-secondary);">%</span>
		</span>
		<span
			class="text-[10px] tabular-nums tracking-wider mt-1"
			style="color: var(--color-tesla-text-muted); font-feature-settings: 'tnum';"
			data-testid="battery-range"
		>
			{range} {unit}
		</span>
	</div>
</div>
