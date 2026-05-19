<script lang="ts">
	interface Props {
		/** Speed in km/h (matches the bridge's telemetry shape). */
		speed: number;
		/** Display unit. Defaults to mph (Tesla US convention). */
		unit?: 'mph' | 'kmh';
	}

	let { speed = 0, unit = 'mph' }: Props = $props();

	const displayed = $derived(
		unit === 'mph' ? Math.round(speed * 0.6213711922) : Math.round(speed)
	);
	const label = $derived(unit === 'mph' ? 'MPH' : 'KM/H');
</script>

<div
	class="relative flex flex-col items-center justify-center select-none"
	data-testid="speed-display"
>
	<!-- Soft radial glow behind the speed numeral -->
	<div
		class="absolute inset-0 pointer-events-none"
		style="
			background: radial-gradient(
				ellipse at center,
				rgba(62, 130, 247, 0.10) 0%,
				rgba(62, 130, 247, 0.04) 35%,
				transparent 70%
			);
			filter: blur(2px);
		"
		aria-hidden="true"
	></div>

	<span
		class="relative font-tesla font-semibold leading-[0.9] tracking-tight text-white tabular-nums"
		style="
			font-size: clamp(4rem, 10vw, 7.5rem);
			font-feature-settings: 'tnum', 'ss01';
			text-shadow:
				0 0 18px rgba(255, 255, 255, 0.32),
				0 0 38px rgba(62, 130, 247, 0.18),
				0 2px 0 rgba(0, 0, 0, 0.6);
		"
		data-testid="speed-value"
	>
		{displayed}
	</span>
	<span
		class="relative font-tesla mt-1 text-[11px] uppercase tracking-[0.32em] font-medium"
		style="
			color: var(--color-tesla-text-secondary);
			text-shadow: 0 1px 0 rgba(0, 0, 0, 0.6);
		"
		data-testid="speed-unit"
	>
		{label}
	</span>
</div>
