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
	status: 'connected' | 'disconnected' | 'stale' | 'error';
	carla_fps: number;
	objects_tracked: number;
	cameras_active: number;
	/** Producer timestamp from bridge_status.last_heartbeat, normalized to ISO-8601. */
	last_heartbeat: string | null;
	/** Producer timestamp for the state snapshot itself. */
	updated_at: string | null;
}

export interface SnapshotHistoryEntry {
	url: string;
	timestamp: string;
	object_id: string;
}

export interface VideoSession {
	cameraId: string;
	streamName: string;
	playbackMode: 'LIVE' | 'ON_DEMAND' | string;
	hlsUrl: string;
	delivery?: 'SAME_ORIGIN_PROXY' | 'DIRECT_KINESIS' | string;
	expiresIn: number;
	region: string;
	start?: string;
	end?: string;
}

export interface CoverageInterval {
	start: string;
	end: string;
}

export interface VideoCoverage {
	cameraId: string;
	start: string;
	end: string;
	intervals: CoverageInterval[];
	fragmentCount: number;
	truncated: boolean;
}

export interface TimelineEvent {
	object_id: string;
	object_type: string;
	device_id: string;
	first_seen: string;
	last_seen: string;
	count: number;
	max_confidence: number;
	media_time_trusted?: boolean;
	timestamp_schema_version?: number | null;
	first_event_id?: string;
	last_event_id?: string;
	first_media_timestamp_utc?: string;
	last_media_timestamp_utc?: string;
}

export interface TwinObjectEvidence {
	object_id: string;
	object_type: string;
	event_id?: string | null;
	detection_timestamp_utc?: string | null;
	media_timestamp_utc?: string | null;
	timestamp_schema_version?: number | null;
	media_time_trusted?: boolean;
	media_clock?: {
		schema_version?: number | null;
		source?: string | null;
		anchor_program_date_time_utc?: string | null;
		position_milliseconds?: number | null;
	} | null;
	device_id?: string | null;
	track_id?: number | string | null;
	bbox?: {
		x1?: number;
		y1?: number;
		x2?: number;
		y2?: number;
	} | null;
	gps_location?: {
		latitude?: number;
		longitude?: number;
	} | null;
	tracked_actor_id?: number | null;
	actor_id?: number | null;
	actor_present?: boolean;
	actor_type?: string | null;
	carla_transform?: {
		location: { x: number; y: number; z: number };
		rotation: { pitch: number; yaw: number; roll: number };
	} | null;
}

export interface TimelineHistogramBucket {
	bucket_start: string;
	counts: Record<string, number>;
}

export interface DetectionTimeline {
	start: string;
	end: string;
	bucketSeconds: number;
	totalDetections: number;
	truncated: boolean;
	events: TimelineEvent[];
	histogram: TimelineHistogramBucket[];
}

export type DetectionQueryMode = 'recent' | 'object' | 'geohash';

export interface PersistedMediaClock {
	source?: string | null;
	schema_version?: number | null;
	anchor_program_date_time_utc?: string | null;
	position_milliseconds?: number | null;
}

export interface DetectionItem {
	event_id?: string;
	object_id?: string;
	object_type?: string | null;
	geohash?: string | null;
	confidence_score?: number | string | null;
	device_id?: string | null;
	timestamp_utc?: string | null;
	media_timestamp_utc?: string | null;
	decode_received_at_utc?: string | null;
	decode_latency_ms?: number | string | null;
	timestamp_schema_version?: number | null;
	media_time_trusted?: boolean;
	media_clock?: PersistedMediaClock | null;
	media_clock_status?: string | null;
	perception_run_id?: string | null;
}

export interface DetectionPage {
	items?: DetectionItem[];
	next?: string | null;
}

export interface LivePerceptionDetection {
	object_id?: string | null;
	object_type?: string | null;
	confidence_score?: number | string | null;
	timestamp_utc?: string | null;
	device_id?: string | null;
	track_id?: number | string | null;
	bbox?: {
		x1?: number;
		y1?: number;
		x2?: number;
		y2?: number;
	} | null;
}

export interface LivePerceptionCamera {
	updated_at?: string | null;
	frame_count?: number;
	detections?: LivePerceptionDetection[];
}

export interface LivePerceptionDetections {
	generated_at?: string | null;
	cameras?: Record<string, LivePerceptionCamera>;
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

export type PerceptionClass =
	| 'vehicle'
	| 'pedestrian'
	| 'cone'
	| 'traffic_sign'
	| 'traffic_light';

export type PerceptionAlertLevel = 'none' | 'info' | 'warn' | 'critical';

/** Ego-relative perception record sent as part of drive telemetry. */
export interface Detection {
	id: string;
	class: PerceptionClass;
	pos: [number, number];
	distance: number;
	bbox_dim: [number, number];
	in_path: boolean;
	alert: PerceptionAlertLevel;
	velocity?: [number, number];
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
	detections?: Detection[];
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

/** Client-to-bridge teleport request. Optional values are omitted, never sent as null. */
export interface TeleportCommand extends DriveMessage {
	type: 'teleport';
	request_id: string;
	x: number;
	y: number;
	z?: number;
	yaw?: number;
}

/** Bridge acknowledgement after CARLA reports the vehicle's final position. */
export interface TeleportedMessage extends DriveMessage {
	type: 'teleported';
	request_id: string;
	success: true;
	pos: [number, number, number];
	yaw?: number;
	snapped_to_road?: boolean;
}

/** Validation/runtime failure returned specifically for a teleport request. */
export interface TeleportErrorMessage extends DriveMessage {
	type: 'teleport_error';
	request_id: string;
	success: false;
	message: string;
}

export type TeleportRequestState = 'idle' | 'pending' | 'succeeded' | 'error';

export interface TeleportStatus {
	state: TeleportRequestState;
	message: string | null;
	pos: [number, number, number] | null;
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
