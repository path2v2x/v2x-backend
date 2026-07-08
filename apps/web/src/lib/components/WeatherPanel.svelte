<script lang="ts">
	import { setWeather, setCameraSettings } from '$lib/stores/driveSocket';

	interface Props {
		onClose: () => void;
	}

	let { onClose }: Props = $props();

	let activeTab = $state<'weather' | 'graphics'>('weather');
	const ENABLE_GRAPHICS_CONTROLS = false;

	// ── Weather parameters ──
	let cloudiness = $state(5);
	let precipitation = $state(0);
	let precipitationDeposits = $state(0);
	let windIntensity = $state(10);
	let sunAzimuth = $state(45);
	let sunAltitude = $state(45);
	let fogDensity = $state(2);
	let fogDistance = $state(100);
	let fogFalloff = $state(0.1);
	let wetness = $state(0);
	let scatteringIntensity = $state(1);
	let mieScattering = $state(0.03);
	let rayleighScattering = $state(0.0331);
	let dustStorm = $state(0);

	// ── Graphics (camera) parameters ──
	let bloomIntensity = $state(0.675);
	let lensFlareIntensity = $state(0.1);
	let motionBlurIntensity = $state(0.45);
	let motionBlurMaxDistortion = $state(0.35);
	let exposureMode = $state('histogram');
	let exposureCompensation = $state(0);
	let exposureMinBright = $state(10);
	let exposureMaxBright = $state(12);
	let exposureSpeedUp = $state(3);
	let exposureSpeedDown = $state(1);
	let gamma = $state(2.2);
	let temp = $state(6500);
	let tint = $state(0);
	let fov = $state(90);
	let slope = $state(0.88);
	let toe = $state(0.55);
	let shoulder = $state(0.26);
	let blackClip = $state(0);
	let whiteClip = $state(0.04);
	let chromaticAberration = $state(0);
	let lensCircleMultiplier = $state(0);
	let lensCircleFalloff = $state(5);
	let enablePostprocess = $state(true);

	// ── Weather presets ──
	const WEATHER_PRESETS: Record<string, () => void> = {
		'Clear Noon': () => {
			cloudiness = 5; precipitation = 0; precipitationDeposits = 0;
			windIntensity = 10; sunAzimuth = -1; sunAltitude = 45;
			fogDensity = 2; fogDistance = 100; fogFalloff = 0.1;
			wetness = 0; scatteringIntensity = 1; mieScattering = 0.03;
			rayleighScattering = 0.0331; dustStorm = 0;
		},
		'Sunset': () => {
			cloudiness = 5; precipitation = 0; precipitationDeposits = 0;
			windIntensity = 10; sunAzimuth = -1; sunAltitude = 15;
			fogDensity = 2; fogDistance = 100; fogFalloff = 0.1;
			wetness = 0; scatteringIntensity = 1; mieScattering = 0.03;
			rayleighScattering = 0.0331; dustStorm = 0;
		},
		'Cloudy': () => {
			cloudiness = 60; precipitation = 0; precipitationDeposits = 0;
			windIntensity = 10; sunAzimuth = -1; sunAltitude = 45;
			fogDensity = 3; fogDistance = 100; fogFalloff = 0.1;
			wetness = 0; scatteringIntensity = 1; mieScattering = 0.03;
			rayleighScattering = 0.0331; dustStorm = 0;
		},
		'Rainy': () => {
			cloudiness = 80; precipitation = 60; precipitationDeposits = 60;
			windIntensity = 60; sunAzimuth = -1; sunAltitude = 45;
			fogDensity = 3; fogDistance = 100; fogFalloff = 0.1;
			wetness = 80; scatteringIntensity = 1; mieScattering = 0.03;
			rayleighScattering = 0.0331; dustStorm = 0;
		},
		'Storm': () => {
			cloudiness = 85; precipitation = 70; precipitationDeposits = 70;
			windIntensity = 80; sunAzimuth = -1; sunAltitude = 45;
			fogDensity = 7; fogDistance = 100; fogFalloff = 0.1;
			wetness = 80; scatteringIntensity = 1; mieScattering = 0.03;
			rayleighScattering = 0.0331; dustStorm = 0;
		},
	};

	// ── Graphics presets ──
	const GRAPHICS_PRESETS: Record<string, () => void> = {
		'Default': () => {
			bloomIntensity = 0.675; lensFlareIntensity = 0.1;
			motionBlurIntensity = 0.45; motionBlurMaxDistortion = 0.35;
			exposureMode = 'histogram'; exposureCompensation = 0;
			exposureMinBright = 10; exposureMaxBright = 12;
			exposureSpeedUp = 3; exposureSpeedDown = 1;
			gamma = 2.2; temp = 6500; tint = 0; fov = 90;
			slope = 0.88; toe = 0.55; shoulder = 0.26;
			blackClip = 0; whiteClip = 0.04;
			chromaticAberration = 0; lensCircleMultiplier = 0;
			lensCircleFalloff = 5; enablePostprocess = true;
		},
		'Clean': () => {
			bloomIntensity = 0.1; lensFlareIntensity = 0;
			motionBlurIntensity = 0; motionBlurMaxDistortion = 0;
			exposureMode = 'histogram'; exposureCompensation = 0.5;
			exposureMinBright = 8; exposureMaxBright = 14;
			exposureSpeedUp = 3; exposureSpeedDown = 1;
			gamma = 2.2; temp = 6500; tint = 0; fov = 90;
			slope = 0.88; toe = 0.45; shoulder = 0.3;
			blackClip = 0; whiteClip = 0.04;
			chromaticAberration = 0; lensCircleMultiplier = 0;
			lensCircleFalloff = 5; enablePostprocess = true;
		},
		'Bright Shadows': () => {
			bloomIntensity = 0.2; lensFlareIntensity = 0;
			motionBlurIntensity = 0; motionBlurMaxDistortion = 0;
			exposureMode = 'histogram'; exposureCompensation = 1.5;
			exposureMinBright = 5; exposureMaxBright = 16;
			exposureSpeedUp = 3; exposureSpeedDown = 1;
			gamma = 2.4; temp = 6500; tint = 0; fov = 90;
			slope = 0.88; toe = 0.35; shoulder = 0.4;
			blackClip = 0; whiteClip = 0.06;
			chromaticAberration = 0; lensCircleMultiplier = 0;
			lensCircleFalloff = 5; enablePostprocess = true;
		},
		'No Post FX': () => {
			enablePostprocess = false;
			bloomIntensity = 0; lensFlareIntensity = 0;
			motionBlurIntensity = 0; motionBlurMaxDistortion = 0;
			exposureMode = 'manual'; exposureCompensation = 0;
			gamma = 2.2; temp = 6500; tint = 0; fov = 90;
		},
		'Cinematic': () => {
			bloomIntensity = 0.4; lensFlareIntensity = 0.05;
			motionBlurIntensity = 0.3; motionBlurMaxDistortion = 0.25;
			exposureMode = 'histogram'; exposureCompensation = 0;
			exposureMinBright = 10; exposureMaxBright = 12;
			exposureSpeedUp = 1.5; exposureSpeedDown = 0.5;
			gamma = 2.2; temp = 5800; tint = 0; fov = 75;
			slope = 0.88; toe = 0.55; shoulder = 0.26;
			blackClip = 0; whiteClip = 0.04;
			chromaticAberration = 0.2; lensCircleMultiplier = 0.3;
			lensCircleFalloff = 5; enablePostprocess = true;
		},
		'Wide FOV': () => {
			bloomIntensity = 0.2; lensFlareIntensity = 0;
			motionBlurIntensity = 0; motionBlurMaxDistortion = 0;
			exposureMode = 'histogram'; exposureCompensation = 0.5;
			exposureMinBright = 8; exposureMaxBright = 14;
			exposureSpeedUp = 3; exposureSpeedDown = 1;
			gamma = 2.2; temp = 6500; tint = 0; fov = 110;
			slope = 0.88; toe = 0.45; shoulder = 0.3;
			blackClip = 0; whiteClip = 0.04;
			chromaticAberration = 0; lensCircleMultiplier = 0;
			lensCircleFalloff = 5; enablePostprocess = true;
		},
	};

	function applyWeatherPreset(name: string) {
		WEATHER_PRESETS[name]();
		applyWeather();
	}

	function applyGraphicsPreset(name: string) {
		GRAPHICS_PRESETS[name]();
		applyGraphics();
	}

	function applyWeather() {
		setWeather({
			cloudiness, precipitation, precipitation_deposits: precipitationDeposits,
			wind_intensity: windIntensity, sun_azimuth_angle: sunAzimuth,
			sun_altitude_angle: sunAltitude, fog_density: fogDensity,
			fog_distance: fogDistance, fog_falloff: fogFalloff,
			wetness, scattering_intensity: scatteringIntensity,
			mie_scattering_scale: mieScattering,
			rayleigh_scattering_scale: rayleighScattering, dust_storm: dustStorm,
		});
	}

	function applyGraphics() {
		setCameraSettings({
			bloom_intensity: bloomIntensity,
			lens_flare_intensity: lensFlareIntensity,
			motion_blur_intensity: motionBlurIntensity,
			motion_blur_max_distortion: motionBlurMaxDistortion,
			exposure_mode: exposureMode,
			exposure_compensation: exposureCompensation,
			exposure_min_bright: exposureMinBright,
			exposure_max_bright: exposureMaxBright,
			exposure_speed_up: exposureSpeedUp,
			exposure_speed_down: exposureSpeedDown,
			gamma,
			temp,
			tint,
			fov,
			slope,
			toe,
			shoulder,
			black_clip: blackClip,
			white_clip: whiteClip,
			chromatic_aberration_intensity: chromaticAberration,
			lens_circle_multiplier: lensCircleMultiplier,
			lens_circle_falloff: lensCircleFalloff,
			enable_postprocess_effects: enablePostprocess ? 'true' : 'false',
		});
	}

	type SliderDef = { label: string; get: () => number; set: (v: number) => void; min: number; max: number; step: number };

	const weatherSliders: SliderDef[] = [
		{ label: 'Sun Altitude', get: () => sunAltitude, set: (v) => sunAltitude = v, min: 10, max: 90, step: 1 },
		{ label: 'Sun Azimuth', get: () => sunAzimuth, set: (v) => sunAzimuth = v, min: -1, max: 360, step: 1 },
		{ label: 'Cloudiness', get: () => cloudiness, set: (v) => cloudiness = v, min: 0, max: 85, step: 1 },
		{ label: 'Precipitation', get: () => precipitation, set: (v) => precipitation = v, min: 0, max: 70, step: 1 },
		{ label: 'Puddles', get: () => precipitationDeposits, set: (v) => precipitationDeposits = v, min: 0, max: 70, step: 1 },
		{ label: 'Wind', get: () => windIntensity, set: (v) => windIntensity = v, min: 0, max: 80, step: 1 },
		{ label: 'Fog Density', get: () => fogDensity, set: (v) => fogDensity = v, min: 0, max: 25, step: 1 },
		{ label: 'Fog Distance', get: () => fogDistance, set: (v) => fogDistance = v, min: 25, max: 100, step: 0.25 },
		{ label: 'Fog Falloff', get: () => fogFalloff, set: (v) => fogFalloff = v, min: 0.05, max: 5, step: 0.1 },
		{ label: 'Wetness', get: () => wetness, set: (v) => wetness = v, min: 0, max: 80, step: 1 },
		{ label: 'Scattering', get: () => scatteringIntensity, set: (v) => scatteringIntensity = v, min: 0.5, max: 2, step: 0.1 },
		{ label: 'Mie Scatter', get: () => mieScattering, set: (v) => mieScattering = v, min: 0, max: 0.2, step: 0.01 },
		{ label: 'Rayleigh', get: () => rayleighScattering, set: (v) => rayleighScattering = v, min: 0, max: 0.08, step: 0.001 },
		{ label: 'Dust Storm', get: () => dustStorm, set: (v) => dustStorm = v, min: 0, max: 30, step: 1 },
	];

	const graphicsSliders: SliderDef[] = [
		{ label: 'Bloom', get: () => bloomIntensity, set: (v) => bloomIntensity = v, min: 0, max: 2, step: 0.025 },
		{ label: 'Lens Flare', get: () => lensFlareIntensity, set: (v) => lensFlareIntensity = v, min: 0, max: 1, step: 0.01 },
		{ label: 'Motion Blur', get: () => motionBlurIntensity, set: (v) => motionBlurIntensity = v, min: 0, max: 1, step: 0.05 },
		{ label: 'Blur Distortion', get: () => motionBlurMaxDistortion, set: (v) => motionBlurMaxDistortion = v, min: 0, max: 1, step: 0.05 },
		{ label: 'FOV', get: () => fov, set: (v) => fov = v, min: 50, max: 130, step: 1 },
		{ label: 'Exposure Comp', get: () => exposureCompensation, set: (v) => exposureCompensation = v, min: -5, max: 5, step: 0.1 },
		{ label: 'Exp Min Bright', get: () => exposureMinBright, set: (v) => exposureMinBright = v, min: 0, max: 20, step: 0.5 },
		{ label: 'Exp Max Bright', get: () => exposureMaxBright, set: (v) => exposureMaxBright = v, min: 0, max: 20, step: 0.5 },
		{ label: 'Exp Speed Up', get: () => exposureSpeedUp, set: (v) => exposureSpeedUp = v, min: 0, max: 10, step: 0.1 },
		{ label: 'Exp Speed Down', get: () => exposureSpeedDown, set: (v) => exposureSpeedDown = v, min: 0, max: 10, step: 0.1 },
		{ label: 'Gamma', get: () => gamma, set: (v) => gamma = v, min: 1, max: 4, step: 0.05 },
		{ label: 'Color Temp', get: () => temp, set: (v) => temp = v, min: 2000, max: 12000, step: 100 },
		{ label: 'Tint', get: () => tint, set: (v) => tint = v, min: -1, max: 1, step: 0.05 },
		{ label: 'Tone Slope', get: () => slope, set: (v) => slope = v, min: 0, max: 1, step: 0.01 },
		{ label: 'Tone Toe', get: () => toe, set: (v) => toe = v, min: 0, max: 1, step: 0.01 },
		{ label: 'Tone Shoulder', get: () => shoulder, set: (v) => shoulder = v, min: 0, max: 1, step: 0.01 },
		{ label: 'Black Clip', get: () => blackClip, set: (v) => blackClip = v, min: 0, max: 0.2, step: 0.005 },
		{ label: 'White Clip', get: () => whiteClip, set: (v) => whiteClip = v, min: 0, max: 0.2, step: 0.005 },
		{ label: 'Chromatic Aberr', get: () => chromaticAberration, set: (v) => chromaticAberration = v, min: 0, max: 1, step: 0.05 },
		{ label: 'Vignette', get: () => lensCircleMultiplier, set: (v) => lensCircleMultiplier = v, min: 0, max: 2, step: 0.05 },
		{ label: 'Vignette Falloff', get: () => lensCircleFalloff, set: (v) => lensCircleFalloff = v, min: 0, max: 10, step: 0.5 },
	];
</script>

<div class="absolute bottom-16 right-2 z-30 w-80 max-h-[75vh] bg-gray-900/95 border border-gray-700 rounded-xl overflow-hidden pointer-events-auto flex flex-col">
	<!-- Header with tabs -->
	<div class="border-b border-gray-700 flex items-center">
		<button
			onclick={() => { activeTab = 'weather'; }}
			class="flex-1 px-3 py-2 text-xs font-semibold tracking-wider uppercase transition-colors
				{activeTab === 'weather' ? 'text-white border-b-2 border-cyan-500' : 'text-gray-500 hover:text-gray-300'}"
		>
			Weather
		</button>
		{#if ENABLE_GRAPHICS_CONTROLS}
			<button
				onclick={() => { activeTab = 'graphics'; }}
				class="flex-1 px-3 py-2 text-xs font-semibold tracking-wider uppercase transition-colors
					{activeTab === 'graphics' ? 'text-white border-b-2 border-cyan-500' : 'text-gray-500 hover:text-gray-300'}"
			>
				Graphics
			</button>
		{/if}
		<button onclick={onClose}
			class="px-3 py-2 text-gray-500 hover:text-white text-xs transition-colors">
			X
		</button>
	</div>

	{#if activeTab === 'weather'}
		<!-- Weather presets -->
		<div class="p-2 border-b border-gray-800 flex flex-wrap gap-1">
			{#each Object.keys(WEATHER_PRESETS) as name}
				<button
					onclick={() => applyWeatherPreset(name)}
					class="px-2 py-1 bg-gray-800 hover:bg-gray-700 rounded text-[10px] text-gray-300 hover:text-white transition-colors"
				>{name}</button>
			{/each}
		</div>

		<!-- Weather sliders -->
		<div class="flex-1 overflow-y-auto p-2 flex flex-col gap-2">
			{#each weatherSliders as s}
				<div>
					<div class="flex justify-between mb-0.5">
						<label class="text-[10px] text-gray-500">{s.label}</label>
						<span class="text-[10px] text-gray-400 font-mono">{s.get().toFixed(s.step < 1 ? 2 : 0)}</span>
					</div>
					<input type="range" min={s.min} max={s.max} step={s.step} value={s.get()}
						oninput={(e) => { s.set(Number((e.target as HTMLInputElement).value)); }}
						class="w-full h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500" />
				</div>
			{/each}
		</div>

		<div class="p-2 border-t border-gray-700">
			<button onclick={applyWeather}
				class="w-full py-1.5 bg-cyan-600 hover:bg-cyan-500 rounded-lg text-xs font-medium text-white transition-colors">
				Apply Weather
			</button>
		</div>

	{:else}
		<!-- Graphics presets -->
		<div class="p-2 border-b border-gray-800 flex flex-wrap gap-1">
			{#each Object.keys(GRAPHICS_PRESETS) as name}
				<button
					onclick={() => applyGraphicsPreset(name)}
					class="px-2 py-1 bg-gray-800 hover:bg-gray-700 rounded text-[10px] text-gray-300 hover:text-white transition-colors"
				>{name}</button>
			{/each}
		</div>

		<!-- Post-process toggle -->
		<div class="px-2 pt-2 flex items-center justify-between">
			<span class="text-[10px] text-gray-500">Post-Processing</span>
			<button
				onclick={() => { enablePostprocess = !enablePostprocess; }}
				class="px-2 py-0.5 rounded text-[10px] font-medium transition-colors
					{enablePostprocess ? 'bg-green-600/30 text-green-400' : 'bg-red-600/30 text-red-400'}"
			>{enablePostprocess ? 'ON' : 'OFF'}</button>
		</div>

		<!-- Exposure mode toggle -->
		<div class="px-2 pt-1 flex items-center justify-between">
			<span class="text-[10px] text-gray-500">Exposure Mode</span>
			<div class="flex gap-1">
				<button
					onclick={() => { exposureMode = 'histogram'; }}
					class="px-2 py-0.5 rounded text-[10px] transition-colors
						{exposureMode === 'histogram' ? 'bg-cyan-600/30 text-cyan-400' : 'bg-gray-800 text-gray-500'}"
				>Auto</button>
				<button
					onclick={() => { exposureMode = 'manual'; }}
					class="px-2 py-0.5 rounded text-[10px] transition-colors
						{exposureMode === 'manual' ? 'bg-cyan-600/30 text-cyan-400' : 'bg-gray-800 text-gray-500'}"
				>Manual</button>
			</div>
		</div>

		<!-- Graphics sliders -->
		<div class="flex-1 overflow-y-auto p-2 flex flex-col gap-2">
			{#each graphicsSliders as s}
				<div>
					<div class="flex justify-between mb-0.5">
						<label class="text-[10px] text-gray-500">{s.label}</label>
						<span class="text-[10px] text-gray-400 font-mono">{s.get().toFixed(s.step < 1 ? (s.step < 0.01 ? 3 : 2) : 0)}</span>
					</div>
					<input type="range" min={s.min} max={s.max} step={s.step} value={s.get()}
						oninput={(e) => { s.set(Number((e.target as HTMLInputElement).value)); }}
						class="w-full h-1 bg-gray-700 rounded-lg appearance-none cursor-pointer accent-cyan-500" />
				</div>
			{/each}
		</div>

		<div class="p-2 border-t border-gray-700">
			<button onclick={applyGraphics}
				class="w-full py-1.5 bg-cyan-600 hover:bg-cyan-500 rounded-lg text-xs font-medium text-white transition-colors">
				Apply Graphics
			</button>
		</div>
	{/if}
</div>
