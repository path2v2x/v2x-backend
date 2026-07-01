interface DetectionRoutes {
	recent: string;
	byObject: string;
	byGeohash: string;
}

export interface RuntimeConfig {
	apiBaseUrl: string;
	detectionsApiBaseUrl: string;
	detectionRoutes: DetectionRoutes;
	stateBaseUrl: string;
	statePath: string;
	mapDataPath: string;
	driveConfigPath: string;
	demoVideosPath: string;
	videoCameraIds: string[];
	perceptionStreamUrls: Record<string, string>;
	perceptionStreamBaseUrl: string;
	perceptionStreamPathTemplate: string;
	cloudflareDriveWsUrl: string;
	tailscaleDriveWsUrl: string;
	driveConfigUpdatedAt?: string;
	driveConfigExpiresAt?: string;
	driveConfigSource?: string;
}

const DEFAULT_CONFIG: RuntimeConfig = {
	apiBaseUrl: 'https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com',
	detectionsApiBaseUrl: 'https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com',
	detectionRoutes: {
		recent: '/detections/recent',
		byObject: '/detections/object/{object_id}',
		byGeohash: '/detections/geohash/{geohash}'
	},
	stateBaseUrl: 'https://w0j9m7dgpg.execute-api.us-west-1.amazonaws.com',
	statePath: '/state',
	mapDataPath: '/map-data',
	driveConfigPath: '/drive-config',
	demoVideosPath: '/demo-videos',
	videoCameraIds: ['ch1', 'ch2', 'ch3', 'ch4'],
	perceptionStreamUrls: {},
	perceptionStreamBaseUrl: '',
	perceptionStreamPathTemplate: '/streams/{camera_id}.mjpg',
	cloudflareDriveWsUrl: '',
	tailscaleDriveWsUrl: 'wss://path-b860i-aorus-pro-ice.tail1cad6a.ts.net'
};

let configPromise: Promise<RuntimeConfig> | null = null;

function withDefaultPath(path: string | undefined, fallback: string): string {
	if (!path) return fallback;
	return path.startsWith('/') ? path : `/${path}`;
}

function normalizeConfig(config: Partial<RuntimeConfig>): RuntimeConfig {
	const apiBaseUrl = (config.apiBaseUrl || DEFAULT_CONFIG.apiBaseUrl).replace(/\/+$/, '');
	const detectionsApiBaseUrl = (
		config.detectionsApiBaseUrl ||
		config.apiBaseUrl ||
		DEFAULT_CONFIG.detectionsApiBaseUrl
	).replace(/\/+$/, '');

	return {
		apiBaseUrl,
		detectionsApiBaseUrl,
		detectionRoutes: {
			recent: withDefaultPath(
				config.detectionRoutes?.recent,
				DEFAULT_CONFIG.detectionRoutes.recent
			),
			byObject: withDefaultPath(
				config.detectionRoutes?.byObject,
				DEFAULT_CONFIG.detectionRoutes.byObject
			),
			byGeohash: withDefaultPath(
				config.detectionRoutes?.byGeohash,
				DEFAULT_CONFIG.detectionRoutes.byGeohash
			)
		},
		stateBaseUrl: (
			config.stateBaseUrl ||
			config.apiBaseUrl ||
			DEFAULT_CONFIG.stateBaseUrl
		).replace(/\/+$/, ''),
		statePath: withDefaultPath(config.statePath, DEFAULT_CONFIG.statePath),
		mapDataPath: withDefaultPath(config.mapDataPath, DEFAULT_CONFIG.mapDataPath),
		driveConfigPath: withDefaultPath(config.driveConfigPath, DEFAULT_CONFIG.driveConfigPath),
		demoVideosPath: withDefaultPath(config.demoVideosPath, DEFAULT_CONFIG.demoVideosPath),
		videoCameraIds: config.videoCameraIds || DEFAULT_CONFIG.videoCameraIds,
		perceptionStreamUrls: config.perceptionStreamUrls || DEFAULT_CONFIG.perceptionStreamUrls,
		perceptionStreamBaseUrl: (config.perceptionStreamBaseUrl || DEFAULT_CONFIG.perceptionStreamBaseUrl).replace(/\/+$/, ''),
		perceptionStreamPathTemplate:
			config.perceptionStreamPathTemplate || DEFAULT_CONFIG.perceptionStreamPathTemplate,
		cloudflareDriveWsUrl: config.cloudflareDriveWsUrl || DEFAULT_CONFIG.cloudflareDriveWsUrl,
		tailscaleDriveWsUrl: config.tailscaleDriveWsUrl || DEFAULT_CONFIG.tailscaleDriveWsUrl,
		driveConfigUpdatedAt: config.driveConfigUpdatedAt,
		driveConfigExpiresAt: config.driveConfigExpiresAt,
		driveConfigSource: config.driveConfigSource
	};
}

async function loadDriveConfigOverlay(config: RuntimeConfig): Promise<Partial<RuntimeConfig>> {
	const url = buildAssetUrl(config.apiBaseUrl, config.driveConfigPath);
	const response = await fetch(`${url}?_t=${Date.now()}`, { cache: 'no-store' });
	if (!response.ok) {
		return {};
	}

	const overlay = (await response.json()) as {
		cloudflareDriveWsUrl?: string;
		tailscaleDriveWsUrl?: string;
		updatedAt?: string;
		expiresAt?: string;
		source?: string;
	};

	return {
		cloudflareDriveWsUrl: overlay.cloudflareDriveWsUrl,
		tailscaleDriveWsUrl: overlay.tailscaleDriveWsUrl,
		driveConfigUpdatedAt: overlay.updatedAt,
		driveConfigExpiresAt: overlay.expiresAt,
		driveConfigSource: overlay.source
	};
}

function withBrowserOverrides(config: RuntimeConfig): RuntimeConfig {
	if (typeof window === 'undefined') return config;

	const params = new URLSearchParams(window.location.search);
	const perceptionStreamBaseUrl =
		params.get('perceptionStreamBaseUrl') || params.get('perceptionBaseUrl');
	const perceptionStreamPathTemplate = params.get('perceptionStreamPathTemplate');
	if (!perceptionStreamBaseUrl && !perceptionStreamPathTemplate) return config;

	return normalizeConfig({
		...config,
		perceptionStreamBaseUrl: perceptionStreamBaseUrl || config.perceptionStreamBaseUrl,
		perceptionStreamPathTemplate:
			perceptionStreamPathTemplate || config.perceptionStreamPathTemplate
	});
}

function shouldSkipDriveConfigOverlay(): boolean {
	if (typeof window === 'undefined') return false;
	const params = new URLSearchParams(window.location.search);
	return params.get('skipDriveConfig') === '1';
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
	if (!configPromise) {
		const configUrl = `/config.json?v=${Date.now()}`;
		configPromise = fetch(configUrl, { cache: 'no-store' })
			.then(async (response) => {
				if (!response.ok) {
					return DEFAULT_CONFIG;
				}
				const staticConfig = normalizeConfig((await response.json()) as Partial<RuntimeConfig>);
				if (shouldSkipDriveConfigOverlay()) {
					return withBrowserOverrides(staticConfig);
				}
				try {
					return withBrowserOverrides(normalizeConfig({
						...staticConfig,
						...(await loadDriveConfigOverlay(staticConfig))
					}));
				} catch {
					return withBrowserOverrides(staticConfig);
				}
			})
			.catch(() => DEFAULT_CONFIG);
	}

	return configPromise;
}

export function buildAssetUrl(baseUrl: string, path: string): string {
	return `${baseUrl.replace(/\/+$/, '')}${withDefaultPath(path, '/')}`;
}
