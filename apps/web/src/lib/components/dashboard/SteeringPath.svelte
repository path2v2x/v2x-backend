<script lang="ts">
	/**
	 * Tesla-style predicted-path visualization.
	 *
	 * Ego car sits at the bottom facing forward (up the SVG). Two guide
	 * lines extend from the front of the car and curve left or right
	 * based on the `steer` input — the same pattern used in backup-cam
	 * parking guidelines and Tesla's forward-path overlay.
	 */

	interface Props {
		/** Steer in [-1, 1]. Negative = left, positive = right. */
		steer: number;
		/** Width in px. */
		width?: number;
		/** Height in px. */
		height?: number;
	}

	let { steer = 0, width = 90, height = 84 }: Props = $props();

	const s = $derived(Math.max(-1, Math.min(1, steer)));

	// SVG viewBox is fixed at 100×100. The container handles physical size.
	const CX = 50;       // horizontal centre
	const CAR_TOP_Y = 76; // y of the front of the car
	const CAR_BOTTOM_Y = 96;
	const CAR_HALF_WIDTH = 7;

	// Guide-line geometry — interpolated from "straight ahead" to "fully turned".
	const LANE_HALF_WIDTH = 9;   // half-spacing between left and right guides at the start (front of car)
	const STRAIGHT_END_Y = 14;   // y of guide endpoint when steer = 0
	const TURNED_END_Y = 30;     // y of guide endpoint at full lock (lines shorten as they curve)
	const TURN_OFFSET_PX = 36;   // how far the far end shifts horizontally at full lock

	// Endpoint y eases up as |s| increases (curved paths look shorter).
	const endY = $derived(STRAIGHT_END_Y + Math.abs(s) * (TURNED_END_Y - STRAIGHT_END_Y));
	// Lateral shift of the far end of each guide; positive shift = turn right.
	const farShift = $derived(s * TURN_OFFSET_PX);

	const leftStartX = CX - CAR_HALF_WIDTH;
	const rightStartX = CX + CAR_HALF_WIDTH;
	const leftEndX = $derived(CX - LANE_HALF_WIDTH + farShift);
	const rightEndX = $derived(CX + LANE_HALF_WIDTH + farShift);
	const ctrlY = (CAR_TOP_Y + STRAIGHT_END_Y) / 2;

	// Quadratic Bezier control point sits directly above the start point.
	// That keeps the tangent VERTICAL at the car (forward) and pushes all
	// the lateral motion to the tip end — so the lines stay straight near
	// the car and fan outward in the direction of the turn (funnel shape,
	// like Tesla's forward-path / backup-cam parking guidelines).
	const leftCtrlX = leftStartX;
	const rightCtrlX = rightStartX;
	const centerCtrlX = CX;

	const leftPath = $derived(
		`M ${leftStartX},${CAR_TOP_Y} Q ${leftCtrlX},${ctrlY} ${leftEndX},${endY}`
	);
	const rightPath = $derived(
		`M ${rightStartX},${CAR_TOP_Y} Q ${rightCtrlX},${ctrlY} ${rightEndX},${endY}`
	);

	// Center hint line between the two guides (subtle).
	const centerEndX = $derived(CX + farShift);
	const centerPath = $derived(
		`M ${CX},${CAR_TOP_Y} Q ${centerCtrlX},${ctrlY} ${centerEndX},${endY}`
	);
</script>

<div
	class="relative select-none shrink-0"
	style="width: {width}px; height: {height}px;"
	data-testid="steering-path"
	data-steer={s.toFixed(3)}
>
	<svg viewBox="0 0 100 100" class="absolute inset-0 w-full h-full overflow-visible" aria-hidden="true">
		<defs>
			<!-- Guide lines fade out toward the far end -->
			<linearGradient id="guide-grad" x1="0%" y1="100%" x2="0%" y2="0%">
				<stop offset="0%" stop-color="rgba(120, 170, 255, 0.95)" />
				<stop offset="55%" stop-color="rgba(120, 170, 255, 0.65)" />
				<stop offset="100%" stop-color="rgba(120, 170, 255, 0.05)" />
			</linearGradient>

			<linearGradient id="guide-grad-soft" x1="0%" y1="100%" x2="0%" y2="0%">
				<stop offset="0%" stop-color="rgba(255, 255, 255, 0.35)" />
				<stop offset="60%" stop-color="rgba(255, 255, 255, 0.15)" />
				<stop offset="100%" stop-color="rgba(255, 255, 255, 0)" />
			</linearGradient>
		</defs>

		<!-- Centre dashed hint line (subtle white) -->
		<path
			d={centerPath}
			fill="none"
			stroke="url(#guide-grad-soft)"
			stroke-width="1"
			stroke-dasharray="3 4"
			stroke-linecap="round"
		/>

		<!-- Left guide -->
		<path
			d={leftPath}
			fill="none"
			stroke="url(#guide-grad)"
			stroke-width="2.4"
			stroke-linecap="round"
			style="
				filter: drop-shadow(0 0 4px rgba(62, 130, 247, 0.55));
				transition: d 80ms linear;
			"
			data-testid="path-guide-left"
		/>
		<!-- Right guide -->
		<path
			d={rightPath}
			fill="none"
			stroke="url(#guide-grad)"
			stroke-width="2.4"
			stroke-linecap="round"
			style="
				filter: drop-shadow(0 0 4px rgba(62, 130, 247, 0.55));
				transition: d 80ms linear;
			"
			data-testid="path-guide-right"
		/>

		<!-- Ego car at the bottom, facing forward (up) -->
		<g data-testid="path-ego-car">
			<!-- Body shadow -->
			<rect
				x={CX - CAR_HALF_WIDTH - 0.5}
				y={CAR_TOP_Y - 0.5}
				width={CAR_HALF_WIDTH * 2 + 1}
				height={CAR_BOTTOM_Y - CAR_TOP_Y + 1}
				rx="3.5"
				fill="rgba(0,0,0,0.55)"
				transform="translate(0, 1.2)"
				style="filter: blur(1.4px);"
			/>
			<!-- Body -->
			<rect
				x={CX - CAR_HALF_WIDTH}
				y={CAR_TOP_Y}
				width={CAR_HALF_WIDTH * 2}
				height={CAR_BOTTOM_Y - CAR_TOP_Y}
				rx="3"
				fill="var(--color-tesla-vehicle, #c8ccd1)"
				stroke="rgba(255,255,255,0.18)"
				stroke-width="0.6"
			/>
			<!-- Windshield hint (darker stripe near the front) -->
			<rect
				x={CX - CAR_HALF_WIDTH * 0.7}
				y={CAR_TOP_Y + 2}
				width={CAR_HALF_WIDTH * 1.4}
				height="4"
				rx="1.2"
				fill="rgba(10, 14, 22, 0.55)"
			/>
			<!-- Headlight bloom -->
			<circle
				cx={CX - CAR_HALF_WIDTH * 0.55}
				cy={CAR_TOP_Y + 0.5}
				r="1"
				fill="#fff6c2"
				style="filter: drop-shadow(0 0 2.5px rgba(255, 246, 194, 0.95));"
			/>
			<circle
				cx={CX + CAR_HALF_WIDTH * 0.55}
				cy={CAR_TOP_Y + 0.5}
				r="1"
				fill="#fff6c2"
				style="filter: drop-shadow(0 0 2.5px rgba(255, 246, 194, 0.95));"
			/>
		</g>
	</svg>
</div>
