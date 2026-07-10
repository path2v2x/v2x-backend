import { buildAssetUrl, loadRuntimeConfig } from './runtime-config';
import type {
	DemoVideo,
	DetectionPage,
	DetectionQueryMode,
	DetectionTimeline,
	LivePerceptionDetections,
	VideoCoverage,
	VideoSession
} from './types';

export interface StateJson {
	objects: import('./types').TrackedObject[];
	bridge_status?: {
		status?: string | null;
		carla_fps?: number | null;
		objects_tracked?: number | null;
		cameras_active?: number | null;
		last_heartbeat?: string | number | null;
	};
	updated_at?: string | null;
}

interface DemoVideosResponse {
	items: DemoVideo[];
}

/**
 * Fetch the current digital twin state from the read API.
 * The API reads the private S3 state bucket and rewrites snapshot URLs.
 */
export async function fetchState(): Promise<StateJson> {
	const config = await loadRuntimeConfig();
	const url = `${buildAssetUrl(config.stateBaseUrl, config.statePath)}?_t=${Date.now()}`;
	const response = await fetch(url);

	if (!response.ok) {
		throw new Error(`Failed to fetch state: ${response.status}`);
	}

	return response.json() as Promise<StateJson>;
}

/**
 * Fetch road polyline data for the map overlay from the read API.
 * Returns an array of polylines, each polyline is an array of [lon, lat] pairs.
 */
export interface MapDataResponse {
	geo_ref: {
		map_name: string;
		origin_lat: number;
		origin_lon: number;
		origin_alt: number;
		proj_string?: string;
	};
	road_network: number[][][];
}

export async function fetchMapData(): Promise<number[][][]> {
	const data = await fetchMapDataFull();
	return data.road_network;
}

export async function fetchMapDataFull(): Promise<MapDataResponse> {
	const config = await loadRuntimeConfig();
	const url = buildAssetUrl(config.stateBaseUrl, config.mapDataPath);
	const response = await fetch(url);

	if (!response.ok) {
		throw new Error(`Failed to fetch map data: ${response.status}`);
	}

	return (await response.json()) as MapDataResponse;
}

async function readErrorDetail(response: Response): Promise<string> {
	let detail = `${response.status}`;
	try {
		const body = (await response.json()) as { detail?: string; error?: string };
		detail = body.detail || body.error || detail;
	} catch {
		// Keep the HTTP status fallback.
	}
	return detail;
}

export async function fetchVideoSession(
	cameraId: string,
	archiveWindow?: { start: string; end: string }
): Promise<VideoSession> {
	const config = await loadRuntimeConfig();
	const url = new URL(
		`${config.apiBaseUrl.replace(/\/+$/, '')}/video/session/${encodeURIComponent(cameraId)}`
	);
	if (archiveWindow) {
		url.searchParams.set('start', archiveWindow.start);
		url.searchParams.set('end', archiveWindow.end);
	}
	const response = await fetch(url, { cache: 'no-store' });

	if (!response.ok) {
		throw new Error(`Failed to fetch video session: ${await readErrorDetail(response)}`);
	}

	return (await response.json()) as VideoSession;
}

export async function fetchVideoCoverage(
	cameraId: string,
	window: { start: string; end: string }
): Promise<VideoCoverage> {
	const config = await loadRuntimeConfig();
	const url = new URL(
		`${config.apiBaseUrl.replace(/\/+$/, '')}/video/coverage/${encodeURIComponent(cameraId)}`
	);
	url.searchParams.set('start', window.start);
	url.searchParams.set('end', window.end);
	const response = await fetch(url, { cache: 'no-store' });

	if (!response.ok) {
		throw new Error(`Failed to fetch video coverage: ${await readErrorDetail(response)}`);
	}

	return (await response.json()) as VideoCoverage;
}

export async function fetchDetectionTimeline(options: {
	start: string;
	end: string;
	bucketSeconds?: number;
	deviceId?: string;
	objectType?: string;
}): Promise<DetectionTimeline> {
	const config = await loadRuntimeConfig();
	const url = new URL(`${config.detectionsApiBaseUrl.replace(/\/+$/, '')}/detections/timeline`);
	url.searchParams.set('start', options.start);
	url.searchParams.set('end', options.end);
	if (options.bucketSeconds) {
		url.searchParams.set('bucket', String(options.bucketSeconds));
	}
	if (options.deviceId) {
		url.searchParams.set('device_id', options.deviceId);
	}
	if (options.objectType) {
		url.searchParams.set('object_type', options.objectType);
	}
	const response = await fetch(url, {
		headers: { accept: 'application/json' },
		cache: 'no-store'
	});

	if (!response.ok) {
		throw new Error(`Failed to fetch detection timeline: ${await readErrorDetail(response)}`);
	}

	return (await response.json()) as DetectionTimeline;
}

export async function fetchDetectionsRange(options: {
	start: string;
	end: string;
	limit?: number;
	next?: string | null;
}): Promise<DetectionPage> {
	const config = await loadRuntimeConfig();
	const url = new URL(`${config.detectionsApiBaseUrl.replace(/\/+$/, '')}/detections/range`);
	url.searchParams.set('start', options.start);
	url.searchParams.set('end', options.end);
	url.searchParams.set('limit', String(options.limit || 50));
	if (options.next) {
		url.searchParams.set('next', options.next);
	}
	const response = await fetch(url, {
		headers: { accept: 'application/json' },
		cache: 'no-store'
	});

	if (!response.ok) {
		throw new Error(`Failed to fetch detections range: ${await readErrorDetail(response)}`);
	}

	return (await response.json()) as DetectionPage;
}

export async function fetchDemoVideos(): Promise<DemoVideo[]> {
	const config = await loadRuntimeConfig();
	const response = await fetch(
		buildAssetUrl(config.apiBaseUrl, config.demoVideosPath),
		{ cache: 'no-store' }
	);

	if (!response.ok) {
		throw new Error(`Failed to fetch demo videos: ${response.status}`);
	}

	return ((await response.json()) as DemoVideosResponse).items || [];
}

function buildDetectionsUrl(
	mode: DetectionQueryMode,
	query: string,
	limit: number,
	next: string | null,
	config: Awaited<ReturnType<typeof loadRuntimeConfig>>
): string {
	const base = config.detectionsApiBaseUrl.replace(/\/+$/, '');
	let path = config.detectionRoutes.recent;

	if (mode === 'object') {
		path = config.detectionRoutes.byObject.replace('{object_id}', encodeURIComponent(query));
	} else if (mode === 'geohash') {
		path = config.detectionRoutes.byGeohash.replace('{geohash}', encodeURIComponent(query));
	}

	const url = new URL(`${base}${path}`);
	url.searchParams.set('limit', String(limit));
	if (next) {
		url.searchParams.set('next', next);
	}
	return url.toString();
}

export async function fetchDetectionsPage(options: {
	mode: DetectionQueryMode;
	query?: string;
	limit?: number;
	next?: string | null;
}): Promise<DetectionPage> {
	const config = await loadRuntimeConfig();
	const response = await fetch(
		buildDetectionsUrl(
			options.mode,
			options.query?.trim() || '',
			options.limit || 50,
			options.next || null,
			config
		),
		{
			headers: { accept: 'application/json' },
			cache: 'no-store'
		}
	);

	if (!response.ok) {
		throw new Error(`Failed to fetch detections: ${response.status}`);
	}

	return (await response.json()) as DetectionPage;
}

function buildPerceptionMetadataBaseUrl(config: Awaited<ReturnType<typeof loadRuntimeConfig>>): string {
	if (config.perceptionStreamBaseUrl) {
		return config.perceptionStreamBaseUrl.replace(/\/+$/, '');
	}

	const firstStreamUrl = Object.values(config.perceptionStreamUrls)[0];
	if (!firstStreamUrl) {
		throw new Error('Perception stream metadata is not configured.');
	}

	return new URL(firstStreamUrl).origin;
}

export async function fetchLivePerceptionDetections(): Promise<LivePerceptionDetections> {
	const config = await loadRuntimeConfig();
	const baseUrl = buildPerceptionMetadataBaseUrl(config);
	const response = await fetch(`${baseUrl}/detections/latest?_t=${Date.now()}`, {
		headers: { accept: 'application/json' },
		cache: 'no-store'
	});

	if (!response.ok) {
		throw new Error(`Failed to fetch live perception detections: ${response.status}`);
	}

	return (await response.json()) as LivePerceptionDetections;
}
