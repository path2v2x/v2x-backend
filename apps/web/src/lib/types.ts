export interface TrackedObject {
	object_id: string;
	object_type: 'traffic_cone' | 'vehicle' | 'walker' | string;
	lat: number;
	lon: number;
	confidence: number;
	street_name: string;
	timestamp_utc: string;
	snapshot_url: string | null;
	snapshot_timestamp: string | null;
	last_updated: number; // unix ms
}

export interface BridgeStatus {
	status: 'connected' | 'disconnected' | 'error';
	carla_fps: number;
	objects_tracked: number;
	cameras_active: number;
	last_heartbeat: number;
}

export interface SnapshotHistoryEntry {
	url: string;
	timestamp: string;
	object_id: string;
}

export interface VideoSession {
	cameraId: string;
	streamName: string;
	playbackMode: 'LIVE' | string;
	hlsUrl: string;
	expiresIn: number;
	region: string;
}

export type DetectionQueryMode = 'recent' | 'object' | 'geohash';

export interface DetectionItem {
	object_id?: string;
	object_type?: string | null;
	geohash?: string | null;
	confidence_score?: number | string | null;
	device_id?: string | null;
	timestamp_utc?: string | null;
}

export interface DetectionPage {
	items?: DetectionItem[];
	next?: string | null;
}

export interface DemoVideo {
	key: string;
	fileName: string;
	title: string;
	url: string;
	sizeBytes: number;
	lastModified: string | null;
	contentType: string;
}

export type FreshnessLevel = 'fresh' | 'stale' | 'old';

// ── Drive Mode Types ──

export type CameraView = 'chase' | 'hood' | 'bird' | 'free';

export type DriveSessionState =
	| 'idle'
	| 'connecting'
	| 'reconstructing'
	| 'ready'
	| 'driving'
	| 'ending'
	| 'error';

export interface NearbyActor {
	id: number;
	pos: [number, number];
	yaw: number;
	type: 'traffic' | 'dynamic' | 'other';
}

export interface DynamicActor {
	actor_id: number;
	blueprint: string;
	name: string;
	pos: [number, number, number];
	yaw: number;
	geofence_radius: number;
	message: string;
	autopilot: boolean;
}

export interface ActorGeofenceAlert {
	actor: DynamicActor;
	distance: number;
}

export interface VehicleTelemetry {
	speed: number;
	gear: number;
	pos: [number, number, number];
	rot: [number, number, number];
	steer: number;
	throttle: number;
	brake: number;
	nearby_actors?: NearbyActor[];
	dynamic_actors?: DynamicActor[];
}

export type TrafficPreset = 'none' | 'light' | 'medium' | 'heavy' | 'chaos';

export interface GamepadCalibration {
	steerAxis: number;
	gasAxis: number;
	brakeAxis: number;
	steerInverted: boolean;
	gasInverted: boolean;
	brakeInverted: boolean;
}

export interface VehicleOption {
	id: string;
	name: string;
	wheels: number;
}

export type DriveMapId = 'richmond' | 'san_ramon';

export interface DriveMapOption {
	id: DriveMapId;
	label: string;
	map_name: string;
}

export interface SpawnableObject {
	id: string;
	name: string;
	category: 'vehicle' | 'prop';
}

export interface PlacedObject {
	actor_id: number;
	blueprint: string;
	pos: [number, number, number];
}

export interface ScenarioInfo {
	name: string;
	file: string;
	object_count: number;
	zone_count?: number;
}

export interface V2xSignal {
	id: number;
	pos: [number, number, number];
	message: string;
	signal_type: 'warning' | 'info' | 'alert';
	radius: number;
}

export interface V2xAlert {
	id: number;
	message: string;
	signal_type: 'warning' | 'info' | 'alert';
	distance: number;
}

export type V2xZoneKind = 'warning' | 'geofence';

export interface V2xZone {
	id: string;
	name: string;
	message: string;
	zone_kind: V2xZoneKind;
	signal_type: 'warning' | 'info' | 'alert';
	polygon: [number, number][];
	color: string;
}

export interface DriveMessage {
	type: string;
	[key: string]: unknown;
}

export interface TrajectoryInfo {
	file: string;
	samples: number;
}

export interface TrajectoryStatus {
	active: boolean;
	name?: string;
	elapsed?: number;
	duration?: number;
	vehicle_id?: number;
	finished?: boolean;
}

export interface XoscScenarioInfo {
	file: string;
	name: string;
	size_bytes: number;
}

export interface XoscRunnerStatus {
	running: boolean;
	file?: string | null;
	started_at?: number | null;
	exit_code?: number | null;
	scenario_runner_configured: boolean;
}

export interface XoscEvent {
	line: string;
	ts: number;
}

export interface XoscFinishedEvent {
	file: string | null;
	exit_code: number | null;
	verdict: 'SUCCESS' | 'FAILURE';
	duration_sec: number;
}
