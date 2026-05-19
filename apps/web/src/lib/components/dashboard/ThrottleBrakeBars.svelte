<script lang="ts">
	interface Props {
		/** Throttle input 0-1. Bar fills from bottom up on the right. */
		throttle: number;
		/** Brake input 0-1. Bar fills from bottom up on the left. */
		brake: number;
		/** Height of the bars in CSS units (e.g. "6rem"). */
		height?: string;
	}

	let { throttle = 0, brake = 0, height = '6.5rem' }: Props = $props();

	const clampedThrottle = $derived(Math.max(0, Math.min(1, throttle)));
	const clampedBrake = $derived(Math.max(0, Math.min(1, brake)));
</script>

<div
	class="flex items-end gap-4 select-none"
	style="height: {height};"
	data-testid="throttle-brake-bars"
>
	<!-- Brake (left, red, bottom-up fill) -->
	<div class="flex flex-col items-center gap-2">
		<div
			class="relative rounded-full overflow-hidden"
			style="
				width: 6px;
				height: 100%;
				background: linear-gradient(180deg, rgba(58, 63, 71, 0.4) 0%, rgba(58, 63, 71, 0.7) 100%);
				border: 1px solid rgba(255, 255, 255, 0.05);
				box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.7);
			"
			data-testid="brake-bar"
			data-fill={clampedBrake.toFixed(3)}
			role="meter"
			aria-label="Brake"
			aria-valuenow={clampedBrake}
			aria-valuemin="0"
			aria-valuemax="1"
		>
			<div
				class="absolute bottom-0 left-0 right-0 transition-[height] duration-100 ease-out rounded-full"
				style="
					height: {clampedBrake * 100}%;
					background: linear-gradient(180deg, #ff5e63 0%, var(--color-tesla-critical) 100%);
					box-shadow:
						0 0 8px var(--color-tesla-critical),
						0 0 16px rgba(232, 33, 39, 0.5);
					opacity: {clampedBrake > 0.01 ? 1 : 0};
				"
			></div>
		</div>
		<span
			class="font-tesla text-[9px] uppercase tracking-[0.2em] font-medium"
			style="color: var(--color-tesla-text-muted);"
		>
			Brk
		</span>
	</div>

	<!-- Throttle (right, green, bottom-up fill) -->
	<div class="flex flex-col items-center gap-2">
		<div
			class="relative rounded-full overflow-hidden"
			style="
				width: 6px;
				height: 100%;
				background: linear-gradient(180deg, rgba(58, 63, 71, 0.4) 0%, rgba(58, 63, 71, 0.7) 100%);
				border: 1px solid rgba(255, 255, 255, 0.05);
				box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.7);
			"
			data-testid="throttle-bar"
			data-fill={clampedThrottle.toFixed(3)}
			role="meter"
			aria-label="Throttle"
			aria-valuenow={clampedThrottle}
			aria-valuemin="0"
			aria-valuemax="1"
		>
			<div
				class="absolute bottom-0 left-0 right-0 transition-[height] duration-100 ease-out rounded-full"
				style="
					height: {clampedThrottle * 100}%;
					background: linear-gradient(180deg, #5dffa1 0%, var(--color-tesla-active) 100%);
					box-shadow:
						0 0 8px var(--color-tesla-active),
						0 0 16px rgba(34, 197, 94, 0.45);
					opacity: {clampedThrottle > 0.01 ? 1 : 0};
				"
			></div>
		</div>
		<span
			class="font-tesla text-[9px] uppercase tracking-[0.2em] font-medium"
			style="color: var(--color-tesla-text-muted);"
		>
			Thr
		</span>
	</div>
</div>
