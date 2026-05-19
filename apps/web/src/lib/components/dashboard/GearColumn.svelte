<script lang="ts">
	export type Gear = 'P' | 'R' | 'N' | 'D';

	interface Props {
		/** Active gear (P/R/N/D). Tesla-style — only one is highlighted. */
		active: Gear;
		/**
		 * Orientation. Defaults to `row` (Tesla S/X cluster style).
		 * `column` gives the Model 3-ish vertical stack.
		 */
		orientation?: 'row' | 'column';
	}

	let { active, orientation = 'row' }: Props = $props();

	const GEARS: Gear[] = ['P', 'R', 'N', 'D'];
</script>

<div
	class="font-tesla flex select-none {orientation === 'row'
		? 'flex-row gap-2 sm:gap-3 items-center'
		: 'flex-col gap-1 items-center'}"
	data-testid="gear-column"
	role="group"
	aria-label="Transmission gear"
>
	{#each GEARS as gear}
		{@const isActive = gear === active}
		<span
			class="relative font-bold leading-none tracking-wide transition-all duration-200 flex items-center justify-center"
			style="
				font-size: 1.4rem;
				width: 1.9rem;
				height: 1.9rem;
				color: {isActive ? '#ffffff' : 'var(--color-tesla-text-muted)'};
				opacity: {isActive ? 1 : 0.35};
				background: {isActive
					? 'radial-gradient(circle at center, rgba(255,255,255,0.10) 0%, rgba(255,255,255,0) 70%)'
					: 'transparent'};
				border-radius: 0.4rem;
				text-shadow: {isActive ? '0 0 10px rgba(255,255,255,0.55)' : 'none'};
			"
			data-testid="gear-{gear.toLowerCase()}"
			data-active={isActive}
		>
			{gear}
		</span>
	{/each}
</div>
