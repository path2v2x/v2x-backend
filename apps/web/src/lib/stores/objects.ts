import { writable, derived } from 'svelte/store';
import type { TrackedObject, BridgeStatus } from '$lib/types';
import { producerEpochMs } from '$lib/producer-time';

function metadataProducerEpochMs(obj: TrackedObject): number {
	if (Number.isFinite(obj.last_updated) && obj.last_updated >= 0) return obj.last_updated;
	return producerEpochMs(obj.timestamp_utc) ?? 0;
}

function normalizedObject(obj: TrackedObject): TrackedObject {
	return { ...obj, last_updated: metadataProducerEpochMs(obj) };
}

function snapshotProducerEpochMs(obj: TrackedObject): number | null {
	return producerEpochMs(obj.snapshot_timestamp);
}

/**
 * Object metadata and camera snapshots are produced by independent pipelines.
 * Compare their clocks separately so a fresh update from one pipeline cannot
 * roll back (or be rejected by) the other pipeline.
 */
function mergeTrackedObject(
	existing: TrackedObject,
	candidate: TrackedObject
): TrackedObject {
	const incoming = normalizedObject(candidate);
	const useIncomingMetadata =
		metadataProducerEpochMs(incoming) >= metadataProducerEpochMs(existing);
	const incomingSnapshotMs = snapshotProducerEpochMs(incoming);
	const existingSnapshotMs = snapshotProducerEpochMs(existing);
	const useIncomingSnapshot =
		incomingSnapshotMs !== null
			? existingSnapshotMs === null || incomingSnapshotMs >= existingSnapshotMs
			: existingSnapshotMs === null;

	const metadata = useIncomingMetadata ? incoming : existing;
	const snapshot = useIncomingSnapshot ? incoming : existing;
	return {
		...metadata,
		snapshot_url: snapshot.snapshot_url,
		snapshot_timestamp: snapshot.snapshot_timestamp
	};
}

function trackedObjectChanged(existing: TrackedObject, incoming: TrackedObject): boolean {
	return (
		existing.object_type !== incoming.object_type ||
		existing.lat !== incoming.lat ||
		existing.lon !== incoming.lon ||
		existing.confidence !== incoming.confidence ||
		existing.street_name !== incoming.street_name ||
		existing.timestamp_utc !== incoming.timestamp_utc ||
		existing.snapshot_url !== incoming.snapshot_url ||
		existing.snapshot_timestamp !== incoming.snapshot_timestamp ||
		existing.last_updated !== incoming.last_updated
	);
}

/**
 * Store holding all tracked objects keyed by object_id.
 */
function createObjectsStore() {
	const { subscribe, set, update } = writable<Map<string, TrackedObject>>(new Map());

	return {
		subscribe,
		set,
		update,

		/** Merge a fresh list of objects, only updating entries that changed. */
		setAll(objectList: TrackedObject[]) {
			update((prev) => {
				const incoming = new Set<string>();
				let changed = false;

				for (const candidate of objectList) {
					incoming.add(candidate.object_id);
					const obj = normalizedObject(candidate);
					const existing = prev.get(obj.object_id);
					const merged = existing ? mergeTrackedObject(existing, obj) : obj;
					if (!existing || trackedObjectChanged(existing, merged)) {
						prev.set(obj.object_id, merged);
						changed = true;
					}
				}

				// Remove objects no longer present
				for (const id of prev.keys()) {
					if (!incoming.has(id)) {
						prev.delete(id);
						changed = true;
					}
				}

				// Return new Map only if something changed (triggers reactivity)
				return changed ? new Map(prev) : prev;
			});
		},

		/** Add or update a single object. */
		upsert(obj: TrackedObject) {
			update((map) => {
				const normalized = normalizedObject(obj);
				const existing = map.get(obj.object_id);
				const merged = existing
					? mergeTrackedObject(existing, normalized)
					: normalized;
				if (existing && !trackedObjectChanged(existing, merged)) return map;
				const updated = new Map(map);
				updated.set(obj.object_id, merged);
				return updated;
			});
		},

		/** Remove an object by ID. */
		remove(id: string) {
			update((map) => {
				const updated = new Map(map);
				updated.delete(id);
				return updated;
			});
		},

		/** Update only the snapshot fields on an existing object. */
		updateSnapshot(objectId: string, snapshotUrl: string, snapshotTimestamp: string) {
			update((map) => {
				const existing = map.get(objectId);
				if (!existing) return map;
				const incomingSnapshotMs = producerEpochMs(snapshotTimestamp);
				const existingSnapshotMs = producerEpochMs(existing.snapshot_timestamp);
				if (
					existingSnapshotMs !== null &&
					(incomingSnapshotMs === null || incomingSnapshotMs < existingSnapshotMs)
				) {
					return map;
				}

				const updated = new Map(map);
				updated.set(objectId, {
					...existing,
					snapshot_url: snapshotUrl,
					snapshot_timestamp: snapshotTimestamp
				});
				return updated;
			});
		}
	};
}

export const objects = createObjectsStore();

/**
 * Derived store: array of all tracked objects sorted by object_id.
 */
export const objectList = derived(objects, ($objects) =>
	Array.from($objects.values()).sort((a, b) => a.object_id.localeCompare(b.object_id))
);

/**
 * Bridge / system status store.
 */
export const bridgeStatus = writable<BridgeStatus>({
	status: 'disconnected',
	carla_fps: 0,
	objects_tracked: 0,
	cameras_active: 0,
	last_heartbeat: null,
	updated_at: null
});

/**
 * Currently selected object ID (for detail panel).
 */
export const selectedObjectId = writable<string | null>(null);

/**
 * Derived: the full TrackedObject for the currently selected ID, or null.
 */
export const selectedObject = derived(
	[objects, selectedObjectId],
	([$objects, $selectedId]) => {
		if ($selectedId === null) return null;
		return $objects.get($selectedId) ?? null;
	}
);

/**
 * Helper: update an object from an incoming WebSocket payload.
 */
export function updateObject(obj: TrackedObject): void {
	objects.upsert(obj);
}

/**
 * Helper: remove an object by ID.
 */
export function removeObject(id: string): void {
	objects.remove(id);
}

/**
 * Helper: update snapshot on an object.
 */
export function updateSnapshot(
	objectId: string,
	snapshotUrl: string,
	snapshotTimestamp: string
): void {
	objects.updateSnapshot(objectId, snapshotUrl, snapshotTimestamp);
}
