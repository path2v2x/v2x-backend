<script lang="ts" module>
	export type Gear = 'P' | 'R' | 'N' | 'D';
</script>

<script lang="ts">
	/**
	 * Circular gas-car-style throttle gauge.
	 *
	 * - Arc sweeps 270° (lower-left → over the top → lower-right). The
	 *   bottom 90° is open (the classic speedometer "smile" gap).
	 * - Needle position is driven by `throttle` in [0, 1].
	 *   throttle=0 → needle at the lower-left start; throttle=1 → needle
	 *   at the lower-right end.
	 * - Center shows the live speed numeral and active gear letter.
	 */

	interface Props {
		/** Throttle 0-1; drives needle rotation along the 270° arc. */
		throttle: number;
		/** Speed in km/h (matches bridge telemetry). */
		speed: number;
		/** Active gear letter. */
		gear: Gear;
		/** mph or kmh (display unit for the centre numeral). */
		unit?: 'mph' | 'kmh';
		/** Diameter of the gauge in px. */
		size?: number;
	}

	let {
		throttle = 0,
		speed = 0,
		gear = 'D',
		unit = 'mph',
		size = 86,
	}: Props = $props();

	const clamped = $derived(Math.max(0, Math.min(1, throttle)));

	// Arc geometry (in SVG units, viewBox 100×100).
	const CX = 50;
	const CY = 50;
	const R = 40;
	const STROKE = 4;

	// Start at -135° (lower-left, 7 o'clock); end at +135° (lower-right,
	// 5 o'clock). Sweep clockwise across the top → 270° total.
	const START_DEG = -135;
	const END_DEG = 135;
	const ARC_DEG = END_DEG - START_DEG; // 270

	function polar(angleDeg: number): { x: number; y: number } {
		const r = (angleDeg * Math.PI) / 180;
		return { x: CX + R * Math.sin(r), y: CY - R * Math.cos(r) };
	}

	const start = polar(START_DEG);
	const end = polar(END_DEG);
	const bgPath = `M ${start.x},${start.y} A ${R},${R} 0 1 1 ${end.x},${end.y}`;

	// Total arc length for stroke-dasharray (270° of 2πR).
	const arcLength = $derived((ARC_DEG / 360) * 2 * Math.PI * R);
	const dashOffset = $derived(arcLength * (1 - clamped));

	// Needle rotation
	const needleDeg = $derived(START_DEG + clamped * ARC_DEG);

	// Tick marks every 30° along the arc (9 ticks).
	const TICKS = Array.from({ length: 9 }, (_, i) => START_DEG + i * (ARC_DEG / 8));

	const displayedSpeed = $derived(
		unit === 'mph' ? Math.round(speed * 0.6213711922) : Math.round(speed)
	);
	const speedLabel = $derived(unit === 'mph' ? 'mph' : 'km/h');
	const gearColor = $derived(
		gear === 'R'
			? 'var(--color-tesla-critical)'
			: gear === 'N'
				? 'var(--color-tesla-text-secondary)'
				: 'var(--color-tesla-text)'
	);
</script>

<div
	class="relative flex items-center justify-center shrink-0 select-none"
	style="width: {size}px; height: {size}px;"
	data-testid="throttle-gauge"
	data-fill={clamped.toFixed(3)}
>
	<svg viewBox="0 0 100 100" class="absolute inset-0 w-full h-full overflow-visible" aria-hidden="true">
		<!-- Background arc (subtle dark ring) -->
		<path
			d={bgPath}
			fill="none"
			stroke="rgba(58, 63, 71, 0.55)"
			stroke-width={STROKE}
			stroke-linecap="round"
		/>

		<!-- Tick marks -->
		{#each TICKS as t}
			{@const inner = polar(t)}
			{@const outerR = R + 4}
			{@const outer = {
				x: CX + outerR * Math.sin((t * Math.PI) / 180),
				y: CY - outerR * Math.cos((t * Math.PI) / 180),
			}}
			<line
				x1={inner.x}
				y1={inner.y}
				x2={outer.x}
				y2={outer.y}
				stroke="rgba(255, 255, 255, 0.18)"
				stroke-width="0.8"
				stroke-linecap="round"
			/>
		{/each}

		<!-- Active fill arc (drawn via stroke-dashoffset) -->
		<path
			d={bgPath}
			fill="none"
			stroke="var(--color-tesla-active)"
			stroke-width={STROKE}
			stroke-linecap="round"
			stroke-dasharray={arcLength}
			stroke-dashoffset={dashOffset}
			style="
				filter: drop-shadow(0 0 4px rgba(34, 197, 94, 0.55))
					drop-shadow(0 0 8px rgba(34, 197, 94, 0.35));
				transition: stroke-dashoffset 90ms linear;
			"
		/>

		<!-- Needle (rotates around center) -->
		<g
			transform="translate({CX}, {CY}) rotate({needleDeg})"
			style="transition: transform 90ms linear;"
			data-testid="throttle-needle"
			data-deg={needleDeg.toFixed(1)}
		>
			<!-- Needle line: from just outside the centre hub to just inside the arc -->
			<line
				x1="0"
				y1="-8"
				x2="0"
				y2={-(R - STROKE / 2 - 1)}
				stroke="#ffffff"
				stroke-width="2.2"
				stroke-linecap="round"
				style="filter: drop-shadow(0 0 4px rgba(255, 255, 255, 0.6));"
			/>
			<!-- Centre pivot dot -->
			<circle cx="0" cy="0" r="3.2" fill="#ffffff" />
			<circle cx="0" cy="0" r="1.4" fill="rgba(0,0,0,0.55)" />
		</g>
	</svg>

	<!-- Center text: speed numeral + gear letter -->
	<div class="relative flex flex-col items-center pointer-events-none">
		<span
			class="font-tesla font-semibold tabular-nums leading-none"
			style="
				color: var(--color-tesla-text);
				font-size: 1.35rem;
				font-feature-settings: 'tnum';
				text-shadow: 0 0 8px rgba(255, 255, 255, 0.3);
			"
			data-testid="gauge-speed"
		>
			{displayedSpeed}
		</span>
		<span
			class="font-tesla mt-0.5 leading-none"
			style="
				color: var(--color-tesla-text-muted);
				font-size: 0.55rem;
				letter-spacing: 0.16em;
				text-transform: uppercase;
			"
		>
			{speedLabel}
		</span>
		<span
			class="font-tesla font-bold tabular-nums leading-none mt-1"
			style="
				color: {gearColor};
				font-size: 0.85rem;
				font-feature-settings: 'tnum';
				text-shadow: 0 0 6px {gear === 'R' ? 'rgba(232,33,39,0.5)' : 'rgba(255,255,255,0.35)'};
			"
			data-testid="gauge-gear"
			data-gear={gear}
		>
			{gear}
		</span>
	</div>
</div>
