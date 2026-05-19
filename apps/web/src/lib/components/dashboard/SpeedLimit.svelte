<script lang="ts">
	interface Props {
		/** Limit in the selected unit. Null = unknown (shows "—"). */
		limit?: number | null;
		unit?: 'mph' | 'kmh';
	}

	let { limit = null, unit = 'mph' }: Props = $props();
</script>

<!-- Stylized speed-limit sign placeholder: white circle, dark digit.
     Tesla shows the posted limit here when known. -->
<div
	class="flex flex-col items-center select-none"
	data-testid="speed-limit-sign"
>
	<div
		class="flex items-center justify-center rounded-full font-tesla font-bold tabular-nums"
		style="
			width: 44px;
			height: 44px;
			background: #ffffff;
			color: #0a0a0c;
			border: 3px solid #d4d6db;
			box-shadow: 0 0 8px rgba(255, 255, 255, 0.18), inset 0 0 0 2px rgba(0,0,0,0.04);
			font-feature-settings: 'tnum';
			font-size: 1.15rem;
		"
		role="img"
		aria-label={limit ? `Speed limit ${limit} ${unit}` : 'Speed limit unknown'}
	>
		{limit ?? '—'}
	</div>
	<span
		class="font-tesla mt-1 text-[9px] uppercase tracking-[0.22em]"
		style="color: var(--color-tesla-text-muted);"
	>
		{unit}
	</span>
</div>
