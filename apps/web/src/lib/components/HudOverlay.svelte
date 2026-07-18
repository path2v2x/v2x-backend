<script lang="ts">
	import type { VehicleTelemetry } from '$lib/types';

	interface Props {
		telemetry: VehicleTelemetry;
		isRecording?: boolean;
	}
	let { telemetry, isRecording = false }: Props = $props();

	let steerPct = $derived((telemetry.steer + 1) / 2);
	let throttlePct = $derived(telemetry.throttle);
	let brakePct = $derived(telemetry.brake);
</script>

<div class="absolute inset-0 pointer-events-none select-none">
	<!-- Bottom HUD bar -->
	<div class="absolute bottom-0 inset-x-0 flex items-end justify-center pb-3 sm:pb-5 gap-4 sm:gap-8">
		<!-- Throttle bar -->
		<div class="flex flex-col items-center gap-1">
			<div class="w-3 sm:w-4 h-14 sm:h-20 bg-white/5 backdrop-blur-sm rounded-full relative overflow-hidden border border-white/10">
				<div class="absolute bottom-0 w-full bg-green-500 rounded-full transition-all duration-75"
					style="height: {throttlePct * 100}%; box-shadow: 0 0 10px rgba(34,197,94,{throttlePct * 0.6})"></div>
			</div>
			<span class="text-[8px] sm:text-[10px] font-body text-gray-500 tracking-widest">T</span>
		</div>

		<!-- Steering + Speed -->
		<div class="flex flex-col items-center gap-1.5">
			<!-- Steering track -->
			<div class="w-28 sm:w-40">
				<div class="h-1 sm:h-1.5 bg-white/5 rounded-full relative backdrop-blur-sm border border-white/5">
					<div class="absolute top-1/2 -translate-y-1/2 w-2.5 sm:w-3 h-2.5 sm:h-3 bg-white rounded-full transition-all duration-75"
						style="left: {steerPct * 100}%; box-shadow: 0 0 8px rgba(255,255,255,0.4)"></div>
					<div class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-px h-2.5 bg-white/20"></div>
				</div>
			</div>

			<!-- Speed + Gear -->
			<div class="flex items-baseline gap-2">
				<span class="text-3xl sm:text-5xl font-display font-bold text-white tabular-nums leading-none"
					style="text-shadow: 0 0 20px rgba(255,255,255,0.15)">
					{Math.round(telemetry.speed)}
				</span>
				<span class="text-[10px] sm:text-xs font-body text-gray-500 tracking-wider">KM/H</span>
				<span class="text-base sm:text-xl font-display font-bold tabular-nums ml-1
					{telemetry.gear < 0 ? 'text-accent' : 'text-gray-500'}">
					{telemetry.gear > 0 ? `D${telemetry.gear}` : telemetry.gear === 0 ? 'N' : 'R'}
				</span>
			</div>
		</div>

		<!-- Brake bar -->
		<div class="flex flex-col items-center gap-1">
			<div class="w-3 sm:w-4 h-14 sm:h-20 bg-white/5 backdrop-blur-sm rounded-full relative overflow-hidden border border-white/10">
				<div class="absolute bottom-0 w-full bg-accent rounded-full transition-all duration-75"
					style="height: {brakePct * 100}%; box-shadow: 0 0 10px rgba(220,38,38,{brakePct * 0.6})"></div>
			</div>
			<span class="text-[8px] sm:text-[10px] font-body text-gray-500 tracking-widest">B</span>
		</div>
	</div>

	<!-- Recording indicator -->
	{#if isRecording}
		<div class="absolute top-12 sm:top-14 right-2 sm:right-4 flex items-center gap-1.5">
			<div class="w-2 h-2 bg-accent rounded-full animate-pulse shadow-[0_0_6px_rgba(220,38,38,0.5)]"></div>
			<span class="text-[10px] font-body text-accent/80 tracking-widest">REC</span>
		</div>
	{/if}

	<!-- GPS position -->
	<div class="absolute top-12 sm:top-14 left-2 sm:left-4 text-[10px] sm:text-xs font-mono text-gray-600 tracking-wider">
		{telemetry.pos[0].toFixed(1)}, {telemetry.pos[1].toFixed(1)}
	</div>
</div>
