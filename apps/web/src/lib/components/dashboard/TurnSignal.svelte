<script lang="ts">
	interface Props {
		/** 'left', 'right', or null (off). */
		direction?: 'left' | 'right' | null;
	}

	let { direction = null }: Props = $props();

	const isLeft = $derived(direction === 'left');
	const isRight = $derived(direction === 'right');
</script>

<!-- Hard-edged blinking green chevron pair. Tesla pattern: step-end
	 opacity (no soft fade) at ~1.5 Hz. Each side is wired independently. -->
<style>
	@keyframes blink-step {
		0%, 49% { opacity: 1; }
		50%, 100% { opacity: 0; }
	}
	.blink {
		animation: blink-step 0.78s steps(2, end) infinite;
	}
</style>

<div class="flex items-center gap-3 select-none" data-testid="turn-signals">
	<!-- Left chevron -->
	<svg
		width="22"
		height="18"
		viewBox="0 0 22 18"
		class:blink={isLeft}
		style="
			color: var(--color-tesla-active);
			opacity: {isLeft ? 1 : 0.12};
			filter: {isLeft ? 'drop-shadow(0 0 6px var(--color-tesla-active))' : 'none'};
		"
		data-testid="turn-signal-left"
		data-active={isLeft}
		aria-hidden="true"
	>
		<path d="M10 1 L1 9 L10 17 L10 12 L21 12 L21 6 L10 6 Z" fill="currentColor" />
	</svg>

	<!-- Right chevron -->
	<svg
		width="22"
		height="18"
		viewBox="0 0 22 18"
		class:blink={isRight}
		style="
			color: var(--color-tesla-active);
			opacity: {isRight ? 1 : 0.12};
			filter: {isRight ? 'drop-shadow(0 0 6px var(--color-tesla-active))' : 'none'};
		"
		data-testid="turn-signal-right"
		data-active={isRight}
		aria-hidden="true"
	>
		<path d="M12 1 L21 9 L12 17 L12 12 L1 12 L1 6 L12 6 Z" fill="currentColor" />
	</svg>
</div>
