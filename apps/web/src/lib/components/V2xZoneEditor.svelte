<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import maplibregl from 'maplibre-gl';
	import { TerraDraw, TerraDrawRectangleMode, TerraDrawPolygonMode, TerraDrawSelectMode, TerraDrawRenderMode } from 'terra-draw';
	import { TerraDrawMapLibreGLAdapter } from 'terra-draw-maplibre-gl-adapter';
	import { MAP_CENTER, DEFAULT_ZOOM, MAP_STYLE_URL } from '$lib/constants';
	import { fetchMapDataFull, type MapDataResponse } from '$lib/api';
	import { v2xZones, addZone, removeZone, updateZone } from '$lib/stores/v2xZones';
	import { saveScenario, driveConnected } from '$lib/stores/driveSocket';
	import { DEFAULT_ZONE_COLORS } from '$lib/zoneRules';
	import type { V2xZone, V2xZoneKind } from '$lib/types';

	interface Props {
		onclose: () => void;
	}

	let { onclose }: Props = $props();

	let mapContainer: HTMLDivElement;
	let map: maplibregl.Map | null = null;
	let draw: TerraDraw | null = null;
	let mapData: MapDataResponse | null = $state(null);
	let activeTool = $state<'rectangle' | 'polygon' | 'select'>('rectangle');
	let editingZoneId = $state<string | null>(null);
	let zoneName = $state('');
	let zoneMessage = $state('');
	let zoneKind = $state<V2xZoneKind>('geofence');
	let showSaveDialog = $state(false);
	let scenarioNameInput = $state('');
	let saveStatus = $state<string>('');

	let zones = $derived($v2xZones);

	onMount(async () => {
		try {
			mapData = await fetchMapDataFull();
		} catch (e) {
			console.warn('Failed to fetch map data:', e);
		}

		const center: [number, number] = mapData?.geo_ref
			? [mapData.geo_ref.origin_lon, mapData.geo_ref.origin_lat]
			: [MAP_CENTER.lon, MAP_CENTER.lat];

		map = new maplibregl.Map({
			container: mapContainer,
			style: MAP_STYLE_URL,
			center,
			zoom: DEFAULT_ZOOM,
			attributionControl: false,
			antialias: true,
		});

		map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'top-right');

		map.on('load', () => {
			if (!map) return;

			// Road network overlay
			if (mapData?.road_network) {
				map.addSource('roads', {
					type: 'geojson',
					data: buildRoadGeoJSON(mapData.road_network),
				});
				map.addLayer({
					id: 'roads-layer',
					type: 'line',
					source: 'roads',
					paint: {
						'line-color': '#6b7280',
						'line-width': 2,
						'line-opacity': 0.6,
					},
				});
			}

			// Existing zones overlay (non-terra-draw, for display)
			map.addSource('existing-zones', {
				type: 'geojson',
				data: buildZonesGeoJSON(zones),
			});
			map.addLayer({
				id: 'existing-zones-fill',
				type: 'fill',
				source: 'existing-zones',
				paint: {
					'fill-color': ['get', 'color'],
					'fill-opacity': 0.25,
				},
			});
			map.addLayer({
				id: 'existing-zones-outline',
				type: 'line',
				source: 'existing-zones',
				paint: {
					'line-color': ['get', 'color'],
					'line-width': 2,
					'line-opacity': 0.8,
				},
			});
			map.addLayer({
				id: 'existing-zones-label',
				type: 'symbol',
				source: 'existing-zones',
				layout: {
					'text-field': ['get', 'name'],
					'text-size': 12,
					'text-anchor': 'center',
				},
				paint: {
					'text-color': '#ffffff',
					'text-halo-color': '#000000',
					'text-halo-width': 1,
				},
			});

			// Initialize terra-draw
			initTerraDraw();
		});
	});

	function initTerraDraw() {
		if (!map) return;

		draw = new TerraDraw({
			adapter: new TerraDrawMapLibreGLAdapter({ map }),
			modes: [
				new TerraDrawRectangleMode(),
				new TerraDrawPolygonMode(),
				new TerraDrawSelectMode({
					flags: {
						polygon: { feature: { draggable: true, coordinates: { midpoints: true, draggable: true, deletable: true } } },
					},
				}),
				new TerraDrawRenderMode({ modeName: 'render', styles: {} }),
			],
		});

		// Register events BEFORE start
		draw.on('finish', (id: any) => {
			console.log('[ZoneEditor] finish event fired, id:', id);
			if (!draw) return;
			const snapshot = draw.getSnapshot();
			console.log('[ZoneEditor] snapshot:', snapshot.length, 'features');
			const feature = snapshot.find((f: any) => f.id === id);
			if (!feature) {
				console.warn('[ZoneEditor] feature not found in snapshot for id:', id);
				return;
			}
			console.log('[ZoneEditor] feature type:', feature.geometry.type);
			if (feature.geometry.type !== 'Polygon') return;

			const coords = feature.geometry.coordinates[0] as [number, number][];
			const newZone: V2xZone = {
				id: Math.random().toString(36).slice(2) + Date.now().toString(36),
				name: `Zone ${zones.length + 1}`,
				message: '',
				zone_kind: 'geofence',
				signal_type: 'warning',
				polygon: coords,
				color: DEFAULT_ZONE_COLORS.geofence,
			};

			addZone(newZone);

			// Remove from terra-draw (we manage zones ourselves)
			try {
				draw.removeFeatures([id]);
			} catch (e) {
				console.warn('[ZoneEditor] removeFeatures error:', e);
			}

			// Update the existing zones layer
			refreshZonesLayer();

			// Open editor for the new zone
			editingZoneId = newZone.id;
			zoneName = newZone.name;
			zoneMessage = newZone.message;
			zoneKind = newZone.zone_kind;
		});

		draw.on('change', (ids: any, type: any) => {
			console.log('[ZoneEditor] change event:', type, 'ids:', ids);
		});

		draw.start();
		draw.setMode('rectangle');
		console.log('[ZoneEditor] terra-draw started in rectangle mode');
	}

	function setTool(tool: 'rectangle' | 'polygon' | 'select') {
		activeTool = tool;
		if (draw) {
			draw.setMode(tool === 'select' ? 'select' : tool);
		}
	}

	function refreshZonesLayer() {
		if (!map) return;
		const source = map.getSource('existing-zones') as maplibregl.GeoJSONSource | undefined;
		if (source) {
			source.setData(buildZonesGeoJSON($v2xZones));
		}
	}

	function editZone(zone: V2xZone) {
		editingZoneId = zone.id;
		zoneName = zone.name;
		zoneMessage = zone.message;
		zoneKind = zone.zone_kind;
	}

	function saveZoneEdit() {
		if (!editingZoneId) return;
		updateZone(editingZoneId, {
			name: zoneName,
			message: zoneMessage,
			zone_kind: zoneKind,
			signal_type: 'warning',
			color: DEFAULT_ZONE_COLORS[zoneKind],
		});
		editingZoneId = null;
		refreshZonesLayer();
	}

	function deleteZone(id: string) {
		removeZone(id);
		if (editingZoneId === id) editingZoneId = null;
		refreshZonesLayer();
	}

	function handleClose() {
		// Save any pending edit
		if (editingZoneId) saveZoneEdit();
		onclose();
	}

	function handleSaveAsScenario() {
		const name = scenarioNameInput.trim();
		if (!name) return;
		if ($v2xZones.length === 0) {
			saveStatus = 'No zones to save';
			return;
		}
		if (!$driveConnected) {
			saveStatus = 'Connect to drive server to save';
			return;
		}
		saveScenario(name, $v2xZones);
		saveStatus = `Saved "${name}"`;
		scenarioNameInput = '';
		showSaveDialog = false;
		setTimeout(() => { saveStatus = ''; }, 2500);
	}

	function buildRoadGeoJSON(lines: number[][][]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: lines.map((coords) => ({
				type: 'Feature' as const,
				geometry: { type: 'LineString' as const, coordinates: coords },
				properties: {},
			})),
		};
	}

	function buildZonesGeoJSON(zones: V2xZone[]): GeoJSON.FeatureCollection {
		return {
			type: 'FeatureCollection',
			features: zones
				.filter((z) => z.polygon.length >= 3)
				.map((z) => ({
					type: 'Feature' as const,
					geometry: {
						type: 'Polygon' as const,
						coordinates: [z.polygon],
					},
					properties: {
						id: z.id,
						name: z.name,
						zone_kind: z.zone_kind,
						color: z.color,
					},
				})),
		};
	}

	function zoneKindLabel(zone: V2xZone): string {
		return zone.zone_kind === 'geofence' ? 'Geo-fence' : 'Warning';
	}

	onDestroy(() => {
		if (draw) {
			try { draw.stop(); } catch { /* already stopped */ }
			draw = null;
		}
		if (map) {
			map.remove();
			map = null;
		}
	});
</script>

<!-- Full-screen modal overlay -->
<div class="fixed inset-0 z-50 flex bg-gray-950">
	<!-- Map area -->
	<div class="relative flex-1">
		<div bind:this={mapContainer} class="h-full w-full"></div>

		<!-- Drawing toolbar -->
		<div class="absolute left-4 top-4 z-10 flex gap-1 rounded-lg border border-gray-700 bg-gray-900/95 p-1 backdrop-blur-sm">
			<button
				class="rounded px-3 py-1.5 text-xs font-medium transition-colors {activeTool === 'rectangle' ? 'bg-blue-600 text-white' : 'text-gray-400 hover:bg-gray-800 hover:text-white'}"
				onclick={() => setTool('rectangle')}
			>
				Rectangle
			</button>
			<button
				class="rounded px-3 py-1.5 text-xs font-medium transition-colors {activeTool === 'polygon' ? 'bg-blue-600 text-white' : 'text-gray-400 hover:bg-gray-800 hover:text-white'}"
				onclick={() => setTool('polygon')}
			>
				Polygon
			</button>
			<button
				class="rounded px-3 py-1.5 text-xs font-medium transition-colors {activeTool === 'select' ? 'bg-blue-600 text-white' : 'text-gray-400 hover:bg-gray-800 hover:text-white'}"
				onclick={() => setTool('select')}
			>
				Select
			</button>
		</div>

		<!-- Close button -->
		<button
			class="absolute right-4 top-4 z-10 rounded-lg border border-gray-700 bg-gray-900/95 px-4 py-2 text-sm font-medium text-white backdrop-blur-sm transition-colors hover:bg-gray-800"
			onclick={handleClose}
		>
			Save & Close
		</button>

		<!-- Instructions -->
		<div class="absolute bottom-4 left-4 z-10 rounded-lg border border-gray-700/50 bg-gray-900/90 px-3 py-2 text-xs text-gray-400 backdrop-blur-sm">
			{#if activeTool === 'rectangle'}
				Click and drag to draw a V2X zone rectangle. Set it as a warning or geo-fence in the side panel.
			{:else if activeTool === 'polygon'}
				Click to place vertices, double-click to finish polygon. Set it as a warning or geo-fence in the side panel.
			{:else}
				Click a zone to select, drag to move
			{/if}
		</div>
	</div>

	<!-- Zone list sidebar -->
	<div class="flex w-80 flex-col border-l border-gray-800 bg-gray-900">
		<div class="border-b border-gray-800 px-4 py-3">
			<h2 class="text-sm font-semibold text-white">V2X Zones</h2>
			<p class="mt-0.5 text-xs text-gray-500">{zones.length} zone{zones.length !== 1 ? 's' : ''} defined</p>
		</div>

		<div class="flex-1 overflow-y-auto">
			{#if zones.length === 0}
				<div class="px-4 py-8 text-center text-xs text-gray-600">
					Draw a shape on the map to create a V2X zone
				</div>
			{/if}

			{#each zones as zone (zone.id)}
				<div
					class="border-b border-gray-800/50 px-4 py-3 transition-colors {editingZoneId === zone.id ? 'bg-gray-800/50' : 'hover:bg-gray-800/30'}"
				>
					{#if editingZoneId === zone.id}
						<!-- Editing form -->
						<div class="flex flex-col gap-2">
							<input
								type="text"
								bind:value={zoneName}
								placeholder="Zone name"
								class="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-white placeholder:text-gray-600 focus:border-blue-500 focus:outline-none"
							/>
							<textarea
								bind:value={zoneMessage}
								placeholder="Alert message shown when car enters this zone"
								rows="2"
								class="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-white placeholder:text-gray-600 focus:border-blue-500 focus:outline-none"
							></textarea>
							<select
								bind:value={zoneKind}
								class="rounded border border-gray-700 bg-gray-800 px-2 py-1 text-xs text-white focus:border-blue-500 focus:outline-none"
							>
								<option value="geofence">Geo-fence (draws boundary)</option>
								<option value="warning">Warning (notification only)</option>
							</select>
							<div class="flex gap-1">
								<button
									class="flex-1 rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500"
									onclick={saveZoneEdit}
								>
									Save
								</button>
								<button
									class="rounded bg-red-600/20 px-2 py-1 text-xs font-medium text-red-400 hover:bg-red-600/30"
									onclick={() => deleteZone(zone.id)}
								>
									Delete
								</button>
							</div>
						</div>
					{:else}
						<!-- Zone summary -->
						<button
							class="w-full text-left"
							onclick={() => editZone(zone)}
						>
							<div class="flex items-center gap-2">
								<span
									class="h-3 w-3 rounded-sm"
									style="background-color: {zone.color};"
								></span>
								<span class="text-xs font-medium text-white">{zone.name}</span>
								<span class="rounded bg-gray-800 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-gray-400">{zoneKindLabel(zone)}</span>
							</div>
							{#if zone.message}
								<p class="mt-0.5 truncate pl-5 text-xs text-gray-500">{zone.message}</p>
							{:else}
								<p class="mt-0.5 pl-5 text-xs italic text-gray-600">No message set</p>
							{/if}
						</button>
					{/if}
				</div>
			{/each}
		</div>

		<!-- Footer actions -->
		<div class="border-t border-gray-800 px-4 py-3 flex flex-col gap-2">
			{#if showSaveDialog}
				<div class="flex gap-1">
					<input
						type="text"
						bind:value={scenarioNameInput}
						placeholder="Scenario name..."
						class="flex-1 rounded border border-gray-700 bg-gray-800 px-2 py-1.5 text-xs text-white placeholder:text-gray-600 focus:border-blue-500 focus:outline-none"
						onkeydown={(e) => {
							if (e.key === 'Enter') handleSaveAsScenario();
							if (e.key === 'Escape') { showSaveDialog = false; scenarioNameInput = ''; }
						}}
					/>
					<button
						class="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
						onclick={handleSaveAsScenario}
						disabled={!scenarioNameInput.trim()}
					>
						Save
					</button>
					<button
						class="rounded bg-gray-700 px-2 py-1.5 text-xs text-gray-300 hover:bg-gray-600"
						onclick={() => { showSaveDialog = false; scenarioNameInput = ''; }}
					>
						X
					</button>
				</div>
			{:else}
				<button
					class="w-full rounded border border-blue-600/40 bg-blue-600/10 px-3 py-2 text-xs font-medium text-blue-300 transition-colors hover:bg-blue-600/20 disabled:opacity-40 disabled:cursor-not-allowed"
					onclick={() => { showSaveDialog = true; }}
					disabled={zones.length === 0}
					title={zones.length === 0 ? 'Draw at least one zone to save' : 'Save these zones as a reusable scenario'}
				>
					Save as Scenario{zones.length > 0 ? ` (${zones.length})` : ''}
				</button>
			{/if}
			{#if saveStatus}
				<p class="text-center text-[10px] text-gray-400">{saveStatus}</p>
			{/if}
			<button
				class="w-full rounded bg-green-600 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-green-500"
				onclick={handleClose}
			>
				Done — Back to Drive
			</button>
		</div>
	</div>
</div>
