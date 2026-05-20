<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { DRIVE_TUNNELS, buildDriveTunnels, type DriveTunnel, type TunnelId } from '$lib/constants';
	import type { CameraView, SpawnableObject } from '$lib/types';

	import {
		gamepadConnected,
		calibrated,
		normalizedInput,
		startPolling,
		stopPolling,
		applyDefaultRests,
	} from '$lib/stores/gamepad';

	import {
		keyboardActive,
		keyboardInput,
		startKeyboardInput,
		stopKeyboardInput,
	} from '$lib/stores/keyboard';

	import {
		driveConnected,
		sessionState,
		telemetry,
		lastError,
		objectsCount,
		vehicleList,
		spawnableObjects,
		placedCount,
		scenarioList,
		v2xSignalCount,
		dynamicActors,
		connect,
		disconnect,
		startSession,
		sendControl,
		switchCamera,
		endSession,
		respawnVehicle,
		clearNonEgoVehicles,
		requestVehicles,
		requestObjects,
		requestScenarios,
		saveScenario,
		loadScenario,
		deleteScenario,
		spawnObject,
		spawnDynamicActor,
		despawnDynamicActor,
		undoPlace,
		undoV2xSignal,
		setOnFrame,
		cameraAspect,
	} from '$lib/stores/driveSocket';

	import CalibrationWizard from '$lib/components/CalibrationWizard.svelte';
	import CameraViewComponent from '$lib/components/CameraView.svelte';
	import HudOverlay from '$lib/components/HudOverlay.svelte';
	import DriverDashboardConnected from '$lib/components/dashboard/DriverDashboardConnected.svelte';
	// V2xToast removed — warnings now stack into the DriverDashboard center stack.
	import V2xSignalPlacer from '$lib/components/V2xSignalPlacer.svelte';
	import V2xZoneEditor from '$lib/components/V2xZoneEditor.svelte';
	import DriveMiniMap from '$lib/components/DriveMiniMap.svelte';
	import WeatherPanel from '$lib/components/WeatherPanel.svelte';
	import TrafficPanel from '$lib/components/TrafficPanel.svelte';
	import TrajectoryPanel from '$lib/components/TrajectoryPanel.svelte';
	import CameraSettingsPanel from '$lib/components/CameraSettingsPanel.svelte';
	import ScenarioPicker from '$lib/components/ScenarioPicker.svelte';
	import { checkZoneProximity, resetZoneProximity, clearZones } from '$lib/stores/v2xZones';
	import { v2xZones } from '$lib/stores/v2xZones';
	import { checkActorGeofenceProximity, resetActorGeofenceProximity } from '$lib/stores/actorGeofences';
	import { syncV2xZones } from '$lib/stores/driveSocket';
	import { shouldSyncZone } from '$lib/zoneRules';
	import { fetchMapDataFull, type MapDataResponse } from '$lib/api';
	import { loadRuntimeConfig } from '$lib/runtime-config';

	type InputMode = 'wheel' | 'keyboard';

	let showCalibration = $state(false);
	let activeCamera = $state<CameraView>('chase');
	let controlLoopId = $state<number | null>(null);
	let inputMode = $state<InputMode>('keyboard');
	let cameraViewRef = $state<CameraViewComponent | null>(null);
	let driveTunnels = $state<DriveTunnel[]>(DRIVE_TUNNELS);
	let selectedTunnel = $state<TunnelId>(DRIVE_TUNNELS[0].id);
	let selectedVehicle = $state('vehicle.tesla.model3');
	let showObjectPlacer = $state(false);
	let showV2xPlacer = $state(false);
	let objectFilter = $state('');
	let actorSpawnMode = $state<'static' | 'autopilot'>('static');
	let geofenceRadiusM = $state(35);
	let dynamicActorMessage = $state('Moving emergency vehicle geofence active');
	let selectedScenario = $state('');
	let showSaveDialog = $state(false);
	let scenarioName = $state('');
	let showZoneEditor = $state(false);
	let showWeatherPanel = $state(false);
	let showTrafficPanel = $state(false);
	let showCameraPanel = $state(false);
	let showTrajectoryPanel = $state(false);
	let showXoscPicker = $state(false);

	// Split-panel width for the right-side map (px). Persisted in localStorage.
	const MAP_WIDTH_MIN = 260;
	const MAP_WIDTH_MAX = 900;
	const MAP_WIDTH_STORAGE_KEY = 'drive-map-panel-width';
	function loadStoredMapWidth(): number | null {
		if (typeof localStorage === 'undefined') return null;
		const raw = localStorage.getItem(MAP_WIDTH_STORAGE_KEY);
		const n = raw ? parseInt(raw, 10) : NaN;
		return Number.isFinite(n) ? n : null;
	}
	let mapPanelWidth = $state<number>(loadStoredMapWidth() ?? 500);
	let dragging = $state(false);

	type MapMode = 'panel' | 'overlay';
	const MAP_MODE_STORAGE_KEY = 'drive-map-mode';
	function loadStoredMapMode(): MapMode {
		if (typeof localStorage === 'undefined') return 'panel';
		return localStorage.getItem(MAP_MODE_STORAGE_KEY) === 'overlay' ? 'overlay' : 'panel';
	}
	let mapMode = $state<MapMode>(loadStoredMapMode());
	function toggleMapMode() {
		mapMode = mapMode === 'panel' ? 'overlay' : 'panel';
		try { localStorage.setItem(MAP_MODE_STORAGE_KEY, mapMode); } catch { /* storage full */ }
	}

	function clampMapWidth(w: number): number {
		const max = typeof window !== 'undefined'
			? Math.min(MAP_WIDTH_MAX, Math.floor(window.innerWidth * 0.75))
			: MAP_WIDTH_MAX;
		return Math.max(MAP_WIDTH_MIN, Math.min(max, w));
	}

	function handleDividerDown(e: PointerEvent) {
		e.preventDefault();
		dragging = true;
		document.body.style.cursor = 'col-resize';
		document.body.style.userSelect = 'none';
		const move = (ev: PointerEvent) => {
			mapPanelWidth = clampMapWidth(window.innerWidth - ev.clientX);
		};
		const up = () => {
			dragging = false;
			document.body.style.cursor = '';
			document.body.style.userSelect = '';
			window.removeEventListener('pointermove', move);
			window.removeEventListener('pointerup', up);
			try { localStorage.setItem(MAP_WIDTH_STORAGE_KEY, String(mapPanelWidth)); } catch { /* storage full */ }
		};
		window.addEventListener('pointermove', move);
		window.addEventListener('pointerup', up);
	}
	let mapData = $state<MapDataResponse | null>(null);
	let numZones = $derived($v2xZones.length);

	let vehicles = $derived($vehicleList);
	let objects = $derived($spawnableObjects);
	let scenarios = $derived($scenarioList);
	let numPlaced = $derived($placedCount);
	let numV2xSignals = $derived($v2xSignalCount);
	let activeDynamicActors = $derived($dynamicActors);
	let filteredObjects = $derived(
		objects.filter(o =>
			objectFilter === '' ||
			o.name.toLowerCase().includes(objectFilter.toLowerCase()) ||
			o.id.toLowerCase().includes(objectFilter.toLowerCase())
		)
	);

	function getSelectedUrl(): string {
		return driveTunnels.find(t => t.id === selectedTunnel)?.url ?? driveTunnels[0].url;
	}

	function switchTunnel(id: TunnelId) {
		if (id === selectedTunnel) return;
		selectedTunnel = id;
		// Reconnect with the new URL — clear vehicle list so it re-fetches
		vehicleList.set([]);
		disconnect();
		connect(getSelectedUrl());
	}

	let connected = $derived($driveConnected);
	let state = $derived($sessionState);
	let currentTelemetry = $derived($telemetry);
	let gamepad = $derived($gamepadConnected);
	let isCalibrated = $derived($calibrated);
	let wheelReady = $derived(inputMode === 'keyboard' || isCalibrated);
	let error = $derived($lastError);

	$effect(() => {
		if ($sessionState === 'driving') {
			startControlLoop();
		}
	});

	function cleanupSession() {
		stopControlLoop();
		stopPolling();
		stopKeyboardInput();
		setOnFrame(null);
		resetActorGeofenceProximity();
		if (state === 'driving' || state === 'ready' || state === 'reconstructing') {
			endSession();
		}
		disconnect();
	}

	// Request vehicle list and scenarios once connected
	$effect(() => {
		if ($driveConnected && $vehicleList.length === 0) {
			requestVehicles();
			requestScenarios();
		}
	});

	onMount(async () => {
		startPolling();
		startKeyboardInput();
		try {
			const config = await loadRuntimeConfig();
			driveTunnels = buildDriveTunnels(config);
			selectedTunnel = driveTunnels[0].id;
		} catch {
			driveTunnels = DRIVE_TUNNELS;
			selectedTunnel = driveTunnels[0].id;
		}
		connect(getSelectedUrl());

		setOnFrame((blob: Blob) => {
			if (cameraViewRef) {
				cameraViewRef.pushFrame(blob);
			}
		});

		// Fetch map data for mini-map and coordinate conversion
		try {
			mapData = await fetchMapDataFull();
		} catch {
			console.warn('Failed to load map data for mini-map');
		}
	});

	onDestroy(() => {
		cleanupSession();
	});

	// Load scenario zones immediately at idle when user selects one.
	// Server responds with zones (and no object spawn because no active session).
	let lastLoadedScenarioFile = $state('');
	$effect(() => {
		if (!$driveConnected || !selectedScenario) return;
		if (selectedScenario === lastLoadedScenarioFile) return;
		lastLoadedScenarioFile = selectedScenario;
		loadScenario(selectedScenario);
	});

	// Re-load scenario once driving so the server spawns the scenario's CARLA objects.
	$effect(() => {
		if ($sessionState === 'driving' && selectedScenario) {
			loadScenario(selectedScenario);
		}
	});

	function handleQuickStart() {
		// Wheel without prior calibration: apply G923 defaults so input works
		// immediately. Wizard remains available via the "Calibrate Wheel" button.
		if (inputMode === 'wheel' && !isCalibrated) {
			applyDefaultRests();
		}
		const now = new Date();
		const oneHourAgo = new Date(now.getTime() - 60 * 60 * 1000);
		startSession(oneHourAgo.toISOString(), now.toISOString(), selectedVehicle);
	}

	function handleSaveScenario() {
		const name = scenarioName.trim();
		if (!name) return;
		saveScenario(name, $v2xZones);
		scenarioName = '';
		showSaveDialog = false;
	}

	function handleDeleteScenario(file: string) {
		if (!file) return;
		if (!confirm('Delete this scenario? This cannot be undone.')) return;
		deleteScenario(file);
		if (selectedScenario === file) {
			selectedScenario = '';
			lastLoadedScenarioFile = '';
		}
	}

	function handleNewScenario() {
		// Clear selection and zones so user starts with a blank slate.
		selectedScenario = '';
		lastLoadedScenarioFile = '';
		clearZones();
	}

	function clampGeofenceRadius(value: number): number {
		if (!Number.isFinite(value)) return 35;
		return Math.max(5, Math.min(250, value));
	}

	function spawnFromActorPanel(obj: SpawnableObject): void {
		if (actorSpawnMode === 'static') {
			spawnObject(obj.id);
			return;
		}

		if (obj.category !== 'vehicle') {
			lastError.set('Autopilot mode only supports vehicles. Switch to Static mode to place props.');
			return;
		}

		const radius = clampGeofenceRadius(geofenceRadiusM);
		geofenceRadiusM = radius;
		spawnDynamicActor(obj.id, radius, dynamicActorMessage.trim());
	}

	// V2X zone proximity check — runs on every telemetry update during driving
	$effect(() => {
		if ($sessionState !== 'driving' || !mapData?.geo_ref) return;
		const t = $telemetry;
		checkZoneProximity(
			t.pos[0], t.pos[1],
			mapData.geo_ref.origin_lat, mapData.geo_ref.origin_lon
		);
	});

	// Moving actor geofence proximity uses CARLA coordinates and does not need map data.
	$effect(() => {
		if ($sessionState !== 'driving') return;
		const t = $telemetry;
		checkActorGeofenceProximity(t.pos, $dynamicActors);
	});

	// Sync V2X zones to bridge for 3D outline rendering (redraw every 5s)
	let zoneSyncInterval: ReturnType<typeof setInterval> | null = null;
	function getSyncedZones() {
		return $v2xZones.filter(shouldSyncZone).map(z => ({
			polygon: z.polygon,
			zone_kind: z.zone_kind,
			signal_type: z.signal_type,
			color: z.color,
		}));
	}

	$effect(() => {
		const zones = getSyncedZones();
		if ($sessionState === 'driving' && zones.length > 0) {
			syncV2xZones(zones);
			if (!zoneSyncInterval) {
				zoneSyncInterval = setInterval(() => {
					const latestZones = getSyncedZones();
					if (latestZones.length > 0) syncV2xZones(latestZones);
				}, 5000);
			}
		} else {
			if (zoneSyncInterval) {
				clearInterval(zoneSyncInterval);
				zoneSyncInterval = null;
			}
		}
	});

	function handleEndSession() {
		stopControlLoop();
		resetZoneProximity();
		resetActorGeofenceProximity();
		endSession();
	}

	function handleCameraSwitch(view: CameraView) {
		activeCamera = view;
		switchCamera(view);
	}

	function handleCalibrationComplete() {
		showCalibration = false;
	}

	function setInputMode(mode: InputMode) {
		inputMode = mode;
	}

	function startControlLoop() {
		if (controlLoopId !== null) return;
		function loop() {
			const input = inputMode === 'wheel' ? $normalizedInput : $keyboardInput;
			sendControl(input.steer, input.throttle, input.brake, input.reverse);
			controlLoopId = requestAnimationFrame(loop);
		}
		controlLoopId = requestAnimationFrame(loop);
	}

	function stopControlLoop() {
		if (controlLoopId !== null) {
			cancelAnimationFrame(controlLoopId);
			controlLoopId = null;
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
		if (e.key === 'Escape') {
			if (showObjectPlacer) {
				showObjectPlacer = false;
			} else if (showV2xPlacer) {
				showV2xPlacer = false;
			} else if (state === 'driving' || state === 'ready') {
				handleEndSession();
			}
		}
		if (state !== 'driving') return;
		if (e.key === 'r') {
			respawnVehicle();
		}
		if (e.key === 'p' || e.key === 'P') {
			showObjectPlacer = !showObjectPlacer;
			showV2xPlacer = false;
			if (showObjectPlacer && objects.length === 0) {
				requestObjects();
			}
		}
		if (e.key === 'v' || e.key === 'V') {
			showV2xPlacer = !showV2xPlacer;
			showObjectPlacer = false;
		}
		if ((e.key === 'u' || e.key === 'U') && !showObjectPlacer && !showV2xPlacer) {
			undoPlace();
		}
		if (e.key === 'x' || e.key === 'X') {
			showXoscPicker = !showXoscPicker;
		}
	}
</script>

<svelte:head>
	<title>V2X Drive</title>
</svelte:head>

<svelte:window onkeydown={handleKeydown} onbeforeunload={cleanupSession} />

{#if showCalibration}
	<CalibrationWizard onComplete={handleCalibrationComplete} />
{/if}

{#if showXoscPicker}
	<ScenarioPicker onclose={() => { showXoscPicker = false; }} />
{/if}

<div class="h-screen w-screen bg-black relative overflow-hidden">
	{#if state === 'idle' || state === 'connecting'}
		<div class="absolute inset-0 flex justify-center bg-gray-950 overflow-y-auto py-6" style="align-items: safe center;">
			<!-- Subtle radial glow behind card -->
			<div class="absolute inset-0 flex items-center justify-center pointer-events-none">
				<div class="w-[600px] h-[600px] rounded-full bg-accent/5 blur-[120px]"></div>
			</div>

			<div class="relative w-[420px] p-8 bg-gray-900/80 backdrop-blur-xl rounded-2xl border border-gray-800/60 text-center shadow-2xl shadow-black/50">
				{#if !connected}
					<div class="py-12">
						<!-- Pulsing ring loader -->
						<div class="relative w-16 h-16 mx-auto mb-5">
							<div class="absolute inset-0 border-2 border-accent/30 rounded-full animate-ping"></div>
							<div class="absolute inset-0 border-2 border-accent border-t-transparent rounded-full animate-spin"></div>
							<div class="absolute inset-2 border border-gray-700 rounded-full"></div>
						</div>
						<p class="font-body text-sm text-gray-400 tracking-wide">CONNECTING TO DRIVE SERVER</p>
						<div class="mt-3 flex justify-center gap-1">
							<div class="w-1 h-1 bg-accent/60 rounded-full animate-pulse"></div>
							<div class="w-1 h-1 bg-accent/60 rounded-full animate-pulse" style="animation-delay: 0.2s"></div>
							<div class="w-1 h-1 bg-accent/60 rounded-full animate-pulse" style="animation-delay: 0.4s"></div>
						</div>
					</div>
				{:else}
					<!-- Title -->
					<div class="mb-6">
						<h2 class="font-display text-2xl font-bold text-white tracking-widest uppercase">V2X Drive</h2>
						<div class="mt-1.5 mx-auto w-12 h-0.5 bg-accent rounded-full"></div>
						<p class="mt-2 font-body text-xs text-gray-500 tracking-wider uppercase">CARLA Simulation</p>
					</div>

					<!-- Input mode toggle -->
					<div class="mb-5">
						<label class="block text-left text-[10px] font-body text-gray-600 tracking-widest uppercase mb-1.5">Input</label>
						<div class="flex gap-2">
							<button onclick={() => setInputMode('keyboard')}
								class="flex-1 group relative px-4 py-3 rounded-xl text-sm font-body tracking-wide transition-all duration-200 cursor-pointer
								{inputMode === 'keyboard'
									? 'bg-gray-800 text-white border border-accent/50 shadow-[0_0_15px_rgba(220,38,38,0.15)]'
									: 'bg-gray-800/50 text-gray-500 border border-gray-800 hover:border-gray-700 hover:text-gray-300'}">
								<!-- Keyboard icon -->
								<svg class="w-5 h-5 mx-auto mb-1.5 {inputMode === 'keyboard' ? 'text-accent' : 'text-gray-600 group-hover:text-gray-400'} transition-colors" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
									<path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25H12" />
								</svg>
								KEYBOARD
							</button>
							<button onclick={() => setInputMode('wheel')}
								class="flex-1 group relative px-4 py-3 rounded-xl text-sm font-body tracking-wide transition-all duration-200 cursor-pointer
								{inputMode === 'wheel'
									? 'bg-gray-800 text-white border border-accent/50 shadow-[0_0_15px_rgba(220,38,38,0.15)]'
									: 'bg-gray-800/50 text-gray-500 border border-gray-800 hover:border-gray-700 hover:text-gray-300'}
								{!gamepad ? 'opacity-40' : ''}">
								<!-- Wheel icon -->
								<svg class="w-5 h-5 mx-auto mb-1.5 {inputMode === 'wheel' ? 'text-accent' : 'text-gray-600 group-hover:text-gray-400'} transition-colors" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
									<circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="3" /><path d="M12 3v6M12 15v6M3 12h6M15 12h6" />
								</svg>
								WHEEL {gamepad ? '' : '(N/A)'}
								{#if inputMode === 'wheel' && gamepad}
									<button onclick={(e) => { e.stopPropagation(); showCalibration = true; }}
										class="absolute -top-1 -right-1 w-5 h-5 bg-gray-700 hover:bg-gray-600 rounded-full flex items-center justify-center cursor-pointer transition-colors"
										title="Calibrate">
										<svg class="w-3 h-3 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
											<path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
											<path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
										</svg>
									</button>
								{/if}
							</button>
						</div>
					</div>

					<!-- Tunnel selector -->
					<div class="mb-4">
						<label class="block text-left text-[10px] font-body text-gray-600 tracking-widest uppercase mb-1.5">Tunnel</label>
						<div class="flex bg-gray-800/50 rounded-xl p-1 border border-gray-800">
							{#each driveTunnels as tunnel}
								<button onclick={() => switchTunnel(tunnel.id)}
									class="flex-1 px-3 py-2 rounded-lg text-xs font-body tracking-wider transition-all duration-200 cursor-pointer
									{selectedTunnel === tunnel.id
										? 'bg-gray-700 text-white shadow-sm'
										: 'text-gray-500 hover:text-gray-300'}">
									{tunnel.label.toUpperCase()}
								</button>
							{/each}
						</div>
					</div>

					<!-- Vehicle picker -->
					<div class="mb-3">
						<label class="block text-left text-[10px] font-body text-gray-600 tracking-widest uppercase mb-1.5">Vehicle</label>
						{#if vehicles.length > 0}
							<div class="relative">
								<select
									bind:value={selectedVehicle}
									class="w-full px-4 py-2.5 bg-gray-800/50 border border-gray-800 rounded-xl text-sm font-body text-white focus:outline-none focus:border-accent/50 focus:shadow-[0_0_10px_rgba(220,38,38,0.1)] appearance-none cursor-pointer transition-all duration-200"
								>
									{#each vehicles as v}
										<option value={v.id}>
											{v.name}{v.wheels === 2 ? ' (bike)' : ''}
										</option>
									{/each}
								</select>
								<svg class="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500 pointer-events-none" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
									<path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
								</svg>
							</div>
						{:else}
							<div class="px-4 py-2.5 bg-gray-800/50 border border-gray-800 rounded-xl text-sm font-body text-gray-600 text-center">
								<span class="inline-block w-3 h-3 border border-gray-600 border-t-transparent rounded-full animate-spin mr-2 align-middle"></span>
								Loading vehicles...
							</div>
						{/if}
					</div>

					<!-- Scenario preset -->
					<div class="mb-5">
						<label class="block text-left text-[10px] font-body text-gray-600 tracking-widest uppercase mb-1.5">Scenario</label>
						<div class="flex gap-2">
							<div class="relative flex-1">
								<select
									bind:value={selectedScenario}
									class="w-full px-4 py-2.5 bg-gray-800/50 border border-gray-800 rounded-xl text-sm font-body text-white focus:outline-none focus:border-accent/50 focus:shadow-[0_0_10px_rgba(220,38,38,0.1)] appearance-none cursor-pointer transition-all duration-200"
								>
									<option value="">No scenario (empty world)</option>
									{#each scenarios as s}
										<option value={s.file}>
											{s.name} ({s.object_count} obj{#if s.zone_count}, {s.zone_count} zone{s.zone_count !== 1 ? 's' : ''}{/if})
										</option>
									{/each}
								</select>
								<svg class="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500 pointer-events-none" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
									<path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7" />
								</svg>
							</div>
							{#if selectedScenario}
								<button onclick={() => handleDeleteScenario(selectedScenario)}
									title="Delete scenario"
									class="px-3 py-2.5 bg-gray-800/50 hover:bg-red-600/30 border border-gray-800 hover:border-red-500/60 rounded-xl text-red-400 transition-all duration-200 cursor-pointer">
									<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
										<path stroke-linecap="round" stroke-linejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6M1 7h22M9 7V5a2 2 0 012-2h2a2 2 0 012 2v2" />
									</svg>
								</button>
							{:else if $v2xZones.length > 0}
								<button onclick={handleNewScenario}
									title="Clear zones for a fresh scenario"
									class="px-3 py-2.5 bg-gray-800/50 hover:bg-gray-800 border border-gray-800 hover:border-gray-700 rounded-xl text-gray-400 hover:text-white transition-all duration-200 cursor-pointer text-xs font-body tracking-wide">
									Clear
								</button>
							{/if}
						</div>
					</div>

					<!-- V2X Zone Editor button -->
					<div class="mb-2">
						<button onclick={() => { showZoneEditor = true; }}
							class="w-full py-2.5 bg-gray-800/50 hover:bg-gray-800 border border-gray-700 hover:border-cyan-500/50 rounded-xl text-xs font-body tracking-wider text-gray-300 hover:text-cyan-300 transition-all duration-200 cursor-pointer flex items-center justify-center gap-2">
							<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
								<path stroke-linecap="round" stroke-linejoin="round" d="M9 6.75V15m6-6v8.25m.503 3.498l4.875-2.437c.381-.19.622-.58.622-1.006V4.82c0-.836-.88-1.38-1.628-1.006l-3.869 1.934c-.317.159-.69.159-1.006 0L9.503 3.252a1.125 1.125 0 00-1.006 0L3.622 5.689C3.24 5.88 3 6.27 3 6.695V19.18c0 .836.88 1.38 1.628 1.006l3.869-1.934c.317-.159.69-.159 1.006 0l4.994 2.497c.317.158.69.158 1.006 0z" />
							</svg>
							PLAN V2X ZONES
							{#if numZones > 0}
								<span class="px-1.5 py-0.5 bg-cyan-600/30 rounded text-[10px] text-cyan-400">{numZones}</span>
							{/if}
						</button>
					</div>

					<!-- OpenSCENARIO Picker button -->
					<div class="mb-4">
						<button onclick={() => { showXoscPicker = true; }}
							class="w-full py-2.5 bg-gray-800/50 hover:bg-gray-800 border border-gray-700 hover:border-purple-500/50 rounded-xl text-xs font-body tracking-wider text-gray-300 hover:text-purple-300 transition-all duration-200 cursor-pointer flex items-center justify-center gap-2">
							<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.5">
								<path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7" />
								<path stroke-linecap="round" stroke-linejoin="round" d="M14 5l7 7-7 7" opacity="0.4" />
							</svg>
							OPENSCENARIO (.xosc)
						</button>
					</div>

					<!-- Action button -->
					<button onclick={handleQuickStart}
						class="w-full py-3.5 bg-accent hover:bg-red-500 rounded-xl text-sm font-display font-bold tracking-widest uppercase text-white transition-all duration-200 shadow-[0_0_20px_rgba(220,38,38,0.3)] hover:shadow-[0_0_30px_rgba(220,38,38,0.5)] cursor-pointer">
						Start Driving
					</button>

					<!-- Keyboard shortcuts or wheel status -->
					<div class="mt-5 pt-4 border-t border-gray-800/60">
						{#if inputMode === 'keyboard'}
							<div class="grid grid-cols-2 gap-x-6 gap-y-1.5 text-left">
								{#each [
									['W', 'Throttle'], ['S', 'Reverse'],
									['A/D', 'Steer'], ['Space', 'Brake'],
									['R', 'Respawn'], ['1-4', 'Camera'],
									['P', 'Place Obj'], ['V', 'V2X Signal'],
									['U', 'Undo'], ['X', 'Scenarios']
								] as [key, action]}
									<div class="flex items-center gap-2">
										<kbd class="px-1.5 py-0.5 bg-gray-800 border border-gray-700 rounded text-[10px] font-mono text-gray-400 min-w-[28px] text-center">{key}</kbd>
										<span class="text-[10px] font-body text-gray-600 tracking-wide">{action}</span>
									</div>
								{/each}
							</div>
						{:else if gamepad}
							<div class="flex items-center justify-center gap-2">
								<div class="w-2 h-2 bg-green-500 rounded-full shadow-[0_0_6px_rgba(34,197,94,0.5)]"></div>
								<span class="text-xs font-body text-green-400/80 tracking-wider">WHEEL CONNECTED — READY</span>
							</div>
						{:else}
							<div class="flex items-center justify-center gap-2">
								<div class="w-2 h-2 bg-yellow-500 rounded-full animate-pulse"></div>
								<span class="text-xs font-body text-yellow-500/80 tracking-wider">CONNECT WHEEL OR USE KEYBOARD</span>
							</div>
						{/if}
					</div>
				{/if}
			</div>
		</div>

	{:else if state === 'reconstructing'}
		<div class="absolute inset-0 flex justify-center bg-gray-950 overflow-y-auto py-6" style="align-items: safe center;">
			<div class="text-center">
				<div class="relative w-16 h-16 mx-auto mb-5">
					<div class="absolute inset-0 border-2 border-accent/20 rounded-full animate-ping"></div>
					<div class="absolute inset-0 border-2 border-accent border-t-transparent rounded-full animate-spin"></div>
				</div>
				<p class="font-display text-lg text-white font-bold tracking-widest uppercase">Loading Scene</p>
				<p class="mt-1 font-body text-xs text-gray-500 tracking-wider">Reconstructing environment...</p>
			</div>
		</div>

	{:else if state === 'driving'}
		<!-- Tesla-style split layout: (camera + dashboard) left, map right -->
		<div class="flex h-full w-full">
			<!-- Left: camera viewport with translucent dashboard overlay -->
			<div class="relative flex-1 min-w-0 overflow-hidden">
				<!-- Aspect-constrained camera viewport: container reshapes with the chosen aspect preset -->
				<div class="absolute inset-0 flex items-center justify-center">
					<div class="relative" style="height: 100%; max-width: 100%; aspect-ratio: {$cameraAspect.w} / {$cameraAspect.h};">
						<CameraViewComponent bind:this={cameraViewRef} activeView={activeCamera} onSwitchView={handleCameraSwitch} />
						<!-- Dashboard overlay anchored to the visible camera frame (scales with aspect) -->
						<div class="absolute bottom-3 left-1/2 -translate-x-1/2 z-10 pointer-events-auto" style="width: 94%; height: 95px;">
							<DriverDashboardConnected />
						</div>
					</div>
				</div>
				<HudOverlay telemetry={currentTelemetry} isRecording={true} />

				{#if mapMode === 'overlay' && mapData}
					<DriveMiniMap
						roadLines={mapData.road_network}
						originLat={mapData.geo_ref.origin_lat}
						originLon={mapData.geo_ref.origin_lon}
						fullPanel={false}
					/>
				{/if}

				<!-- Camera switcher (top-right) -->
				<div class="absolute top-4 right-4 z-20 flex items-center gap-0.5 bg-black/40 backdrop-blur-md rounded-xl border border-white/10 p-1 shadow-lg pointer-events-auto">
					{#each [{ id: 'chase', label: 'Chase' }, { id: 'hood', label: 'Hood' }, { id: 'bird', label: 'Bird' }, { id: 'free', label: 'Free' }] as view}
						<button onclick={() => handleCameraSwitch(view.id as CameraView)}
							aria-pressed={activeCamera === view.id}
							class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide transition-all duration-200 cursor-pointer {activeCamera === view.id
								? 'bg-white/15 text-white shadow-sm'
								: 'text-gray-400 hover:text-white hover:bg-white/5'}">
							{view.label}
						</button>
					{/each}
				</div>

				<!-- Status badges (top-left) — hidden in overlay-map mode to avoid collision with the floating mini-map -->
				<div class="absolute top-4 left-4 z-20 flex items-center gap-2 bg-black/40 backdrop-blur-md rounded-xl border border-white/10 px-3 py-1.5 shadow-lg pointer-events-auto {mapMode === 'overlay' ? 'hidden' : ''}">
					<span class="w-2 h-2 rounded-full {connected ? 'bg-green-400 shadow-[0_0_6px_rgba(74,222,128,0.7)]' : 'bg-red-500'}"></span>
					<span class="text-xs font-medium text-gray-200 tracking-wide">
						{inputMode === 'keyboard' ? 'WASD' : 'Wheel'}
					</span>
					{#if inputMode === 'wheel' && gamepad}
						<span class="h-3.5 w-px bg-white/15"></span>
						<button onclick={() => showCalibration = true}
							class="text-xs text-gray-400 hover:text-white transition-colors cursor-pointer">
							Cal
						</button>
					{/if}
				</div>

				<!-- Bottom action bar -->
				<div class="absolute bottom-4 left-4 z-20 flex flex-col items-stretch gap-0.5 bg-black/40 backdrop-blur-md rounded-xl border border-white/10 p-1 shadow-lg pointer-events-auto">
					<!-- Panel toggles -->
					<button onclick={() => { showWeatherPanel = !showWeatherPanel; showTrafficPanel = false; showCameraPanel = false; showTrajectoryPanel = false; }}
						aria-pressed={showWeatherPanel}
						class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide transition-all duration-200 cursor-pointer {showWeatherPanel
							? 'bg-cyan-600 text-white shadow-[0_0_8px_rgba(8,145,178,0.45)]'
							: 'text-gray-300 hover:text-white hover:bg-white/5'}">
						Weather
					</button>
					<button onclick={() => { showTrafficPanel = !showTrafficPanel; showWeatherPanel = false; showCameraPanel = false; showTrajectoryPanel = false; }}
						aria-pressed={showTrafficPanel}
						class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide transition-all duration-200 cursor-pointer {showTrafficPanel
							? 'bg-amber-600 text-white shadow-[0_0_8px_rgba(217,119,6,0.45)]'
							: 'text-gray-300 hover:text-white hover:bg-white/5'}">
						Traffic
					</button>
					<button onclick={() => { showCameraPanel = !showCameraPanel; showWeatherPanel = false; showTrafficPanel = false; showTrajectoryPanel = false; }}
						aria-pressed={showCameraPanel}
						class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide transition-all duration-200 cursor-pointer {showCameraPanel
							? 'bg-cyan-600 text-white shadow-[0_0_8px_rgba(8,145,178,0.45)]'
							: 'text-gray-300 hover:text-white hover:bg-white/5'}">
						Camera
					</button>
					<button onclick={() => { showTrajectoryPanel = !showTrajectoryPanel; showWeatherPanel = false; showTrafficPanel = false; showCameraPanel = false; }}
						aria-pressed={showTrajectoryPanel}
						class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide transition-all duration-200 cursor-pointer {showTrajectoryPanel
							? 'bg-blue-600 text-white shadow-[0_0_8px_rgba(37,99,235,0.45)]'
							: 'text-gray-300 hover:text-white hover:bg-white/5'}">
						Trajectory
					</button>
					<button onclick={() => { showXoscPicker = !showXoscPicker; }}
						aria-pressed={showXoscPicker}
						class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide transition-all duration-200 cursor-pointer {showXoscPicker
							? 'bg-purple-600 text-white shadow-[0_0_8px_rgba(147,51,234,0.45)]'
							: 'text-gray-300 hover:text-white hover:bg-white/5'}"
						title="OpenSCENARIO (X)">
						Scenarios
					</button>
					<button onclick={toggleMapMode}
						aria-pressed={mapMode === 'overlay'}
						aria-label={mapMode === 'overlay' ? 'Switch map to full panel' : 'Switch map to floating overlay'}
						class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide transition-all duration-200 cursor-pointer text-gray-300 hover:text-white hover:bg-white/5"
						title="Toggle map: full panel / floating overlay">
						Map
					</button>

					<span class="h-px w-full bg-white/15 my-1.5"></span>

					<button onclick={() => clearNonEgoVehicles()}
						class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide bg-orange-600/80 hover:bg-orange-600 text-white transition-all duration-200 cursor-pointer"
						title="Delete all non-ego vehicles (traffic, scenario actors, trajectory playback, placed cars)">
						Clear NPCs
					</button>
					<button onclick={() => respawnVehicle()}
						class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide bg-blue-600/80 hover:bg-blue-600 text-white transition-all duration-200 cursor-pointer">
						Respawn
					</button>
					<button onclick={handleEndSession}
						class="px-3 py-1.5 rounded-lg text-xs font-medium tracking-wide bg-red-600/80 hover:bg-red-600 text-white transition-all duration-200 cursor-pointer">
						End
					</button>
				</div>

				<!-- V2X Signal Placer Panel -->
				{#if showV2xPlacer}
					<V2xSignalPlacer onClose={() => { showV2xPlacer = false; }} />
				{/if}

				<!-- Weather Settings Panel -->
				{#if showWeatherPanel}
					<WeatherPanel onClose={() => { showWeatherPanel = false; }} />
				{/if}

				<!-- Traffic Panel -->
				{#if showTrafficPanel}
					<TrafficPanel onClose={() => { showTrafficPanel = false; }} />
				{/if}

				<!-- Camera Settings Panel -->
				{#if showCameraPanel}
					<CameraSettingsPanel onClose={() => { showCameraPanel = false; }} />
				{/if}

				<!-- Trajectory Panel -->
				{#if showTrajectoryPanel}
					<TrajectoryPanel onClose={() => { showTrajectoryPanel = false; }} />
				{/if}
			</div>

			<!-- Draggable divider -->
			{#if mapData && mapMode === 'panel'}
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div
					class="relative w-1 flex-shrink-0 bg-gray-800 hover:bg-cyan-500/60 {dragging ? 'bg-cyan-500' : ''} cursor-col-resize transition-colors group"
					onpointerdown={handleDividerDown}
					title="Drag to resize"
				>
					<!-- Wider invisible hit area for easier grabbing -->
					<div class="absolute inset-y-0 -left-1.5 -right-1.5"></div>
					<!-- Visible grip dots -->
					<div class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 flex flex-col gap-1 opacity-40 group-hover:opacity-100 transition-opacity pointer-events-none">
						<span class="block w-0.5 h-0.5 rounded-full bg-white"></span>
						<span class="block w-0.5 h-0.5 rounded-full bg-white"></span>
						<span class="block w-0.5 h-0.5 rounded-full bg-white"></span>
						<span class="block w-0.5 h-0.5 rounded-full bg-white"></span>
					</div>
				</div>

				<!-- Right: Full map panel -->
				<div class="flex-shrink-0 bg-gray-950" style="width: {mapPanelWidth}px;">
					<DriveMiniMap
						roadLines={mapData.road_network}
						originLat={mapData.geo_ref.origin_lat}
						originLon={mapData.geo_ref.origin_lon}
						fullPanel={true}
					/>
				</div>
			{/if}
		</div>

		<!-- Object Placer Panel — slide-in from bottom-left -->
		{#if showObjectPlacer}
			<div class="absolute bottom-16 left-2 z-30 w-72 max-h-[30rem] bg-gray-900/95 border border-gray-700 rounded-xl overflow-hidden pointer-events-auto flex flex-col">
				<div class="p-2 border-b border-gray-700 space-y-2">
					<div class="flex items-center gap-2">
						<input
							type="text"
							bind:value={objectFilter}
							placeholder="Search objects..."
							class="flex-1 px-2 py-1 bg-gray-800 border border-gray-600 rounded text-xs text-white placeholder-gray-500 focus:outline-none focus:border-blue-500"
						/>
						<button onclick={() => undoPlace()}
							class="px-2 py-1 bg-yellow-600/70 hover:bg-yellow-600 rounded text-xs text-white"
							title="Undo last (U)">
							Undo
						</button>
						<button onclick={() => { showObjectPlacer = false; }}
							class="px-2 py-1 bg-gray-700 hover:bg-gray-600 rounded text-xs text-gray-300">
							X
						</button>
					</div>

					<div class="grid grid-cols-2 gap-1 rounded bg-gray-800/70 p-1">
						<button
							onclick={() => { actorSpawnMode = 'static'; }}
							class="px-2 py-1 rounded text-[10px] font-medium transition-colors {actorSpawnMode === 'static'
								? 'bg-blue-600 text-white'
								: 'text-gray-400 hover:text-white'}"
						>
							Static
						</button>
						<button
							onclick={() => { actorSpawnMode = 'autopilot'; }}
							class="px-2 py-1 rounded text-[10px] font-medium transition-colors {actorSpawnMode === 'autopilot'
								? 'bg-red-600 text-white'
								: 'text-gray-400 hover:text-white'}"
						>
							Autopilot
						</button>
					</div>

					{#if actorSpawnMode === 'autopilot'}
						<div class="grid grid-cols-[4.5rem_1fr] gap-1.5">
							<input
								type="number"
								min="5"
								max="250"
								step="5"
								bind:value={geofenceRadiusM}
								onblur={() => { geofenceRadiusM = clampGeofenceRadius(geofenceRadiusM); }}
								class="px-2 py-1 bg-gray-800 border border-gray-600 rounded text-xs text-white focus:outline-none focus:border-red-500"
								title="Geofence radius in meters"
							/>
							<input
								type="text"
								bind:value={dynamicActorMessage}
								placeholder="Geofence message..."
								class="px-2 py-1 bg-gray-800 border border-gray-600 rounded text-xs text-white placeholder-gray-500 focus:outline-none focus:border-red-500"
							/>
						</div>
					{/if}
				</div>
				<div class="overflow-y-auto flex-1">
					{#each filteredObjects as obj}
						<button
							onclick={() => spawnFromActorPanel(obj)}
							disabled={actorSpawnMode === 'autopilot' && obj.category !== 'vehicle'}
							class="w-full px-3 py-1.5 text-left text-xs transition-colors flex items-center gap-2 {actorSpawnMode === 'autopilot' && obj.category !== 'vehicle'
								? 'opacity-40 cursor-not-allowed'
								: 'hover:bg-gray-800 cursor-pointer'}"
						>
							<span class="w-1.5 h-1.5 rounded-full {obj.category === 'vehicle' ? 'bg-blue-400' : 'bg-orange-400'}"></span>
							<span class="text-white truncate">{obj.name}</span>
							<span class="text-gray-500 text-[10px] ml-auto">{obj.category}</span>
						</button>
					{/each}
					{#if filteredObjects.length === 0}
						<p class="p-3 text-xs text-gray-500 text-center">
							{objects.length === 0 ? 'Loading...' : 'No matches'}
						</p>
					{/if}
					{#if activeDynamicActors.length > 0}
						<div class="border-t border-gray-700 p-2">
							<p class="mb-1 text-[10px] uppercase tracking-wide text-gray-500">Moving actors</p>
							<div class="space-y-1">
								{#each activeDynamicActors as actor (actor.actor_id)}
									<div class="flex items-center gap-2 rounded bg-gray-800/60 px-2 py-1">
										<div class="min-w-0 flex-1">
											<p class="truncate text-xs text-white">{actor.name || actor.blueprint}</p>
											<p class="text-[10px] text-gray-500">{actor.geofence_radius}m radius</p>
										</div>
										<button
											onclick={() => despawnDynamicActor(actor.actor_id)}
											class="px-2 py-0.5 rounded bg-red-600/60 hover:bg-red-600 text-[10px] text-white transition-colors"
										>
											Remove
										</button>
									</div>
								{/each}
							</div>
						</div>
					{/if}
				</div>
				<div class="p-1.5 border-t border-gray-700">
					{#if showSaveDialog}
						<div class="flex gap-1">
							<input
								type="text"
								bind:value={scenarioName}
								placeholder="Scenario name..."
								class="flex-1 px-2 py-1 bg-gray-800 border border-gray-600 rounded text-xs text-white placeholder-gray-500 focus:outline-none"
								onkeydown={(e) => { if (e.key === 'Enter') handleSaveScenario(); if (e.key === 'Escape') showSaveDialog = false; }}
							/>
							<button onclick={handleSaveScenario}
								class="px-2 py-1 bg-green-600/70 hover:bg-green-600 rounded text-xs text-white">
								Save
							</button>
						</div>
					{:else}
						<div class="flex items-center justify-between">
							<span class="text-[10px] text-gray-500">
								{numPlaced} obj{#if numZones > 0}, {numZones} zone{numZones !== 1 ? 's' : ''}{/if} · P toggle
							</span>
							{#if numPlaced > 0 || numZones > 0}
								<button onclick={() => { showSaveDialog = true; }}
									class="px-2 py-0.5 bg-green-600/50 hover:bg-green-600 rounded text-[10px] text-white">
									Save Scene
								</button>
							{/if}
						</div>
					{/if}
				</div>
			</div>
		{/if}

	{:else if state === 'error'}
		<div class="absolute inset-0 flex justify-center bg-gray-950 overflow-y-auto py-6" style="align-items: safe center;">
			<div class="text-center max-w-md px-8">
				<div class="w-12 h-12 mx-auto mb-4 rounded-full border-2 border-accent/50 flex items-center justify-center">
					<svg class="w-6 h-6 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
						<path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4.5c-.77-.833-2.694-.833-3.464 0L3.34 16.5c-.77.833.192 2.5 1.732 2.5z" />
					</svg>
				</div>
				<p class="font-display text-lg text-white font-bold tracking-widest uppercase mb-2">Error</p>
				<p class="text-sm font-body text-gray-400 mb-6">{error}</p>
				<button onclick={() => sessionState.set('idle')}
					class="px-8 py-2.5 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded-xl text-sm font-body tracking-wider text-white transition-all duration-200 cursor-pointer">
					TRY AGAIN
				</button>
			</div>
		</div>
	{/if}

	{#if showZoneEditor}
		<V2xZoneEditor onclose={() => { showZoneEditor = false; }} />
	{/if}
</div>
