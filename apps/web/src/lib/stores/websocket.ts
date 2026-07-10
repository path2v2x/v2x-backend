import { writable } from 'svelte/store';
import { POLL_INTERVAL } from '$lib/constants';
import { objects, bridgeStatus } from './objects';
import { fetchState } from '$lib/api';
import type { StateJson } from '$lib/api';
import { normalizeProducerTimestamp } from '$lib/producer-time';

/** Whether the polling loop is actively fetching data. */
export const wsConnected = writable<boolean>(false);

let pollTimer: ReturnType<typeof setInterval> | null = null;
let pollGeneration = 0;
let pollRequestSequence = 0;
let activePollRequestId: number | null = null;

export const STATE_STALE_AFTER_MS = 30_000;
const STATE_FUTURE_TOLERANCE_MS = 5_000;
let latestAppliedProducerMs: number | null = null;

function finiteNumber(value: number | null | undefined): number {
	return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

/**
 * Apply only a producer-fresh state snapshot. A cached 200 response is not
 * proof that the bridge is alive, so stale snapshots never replace objects.
 */
export function applyStateSnapshot(state: StateJson, nowMs = Date.now()): boolean {
	const updatedAt = normalizeProducerTimestamp(state.updated_at);
	const lastHeartbeat = normalizeProducerTimestamp(state.bridge_status?.last_heartbeat);
	const freshnessTimestamp = updatedAt ?? lastHeartbeat;
	const freshnessMs = freshnessTimestamp ? Date.parse(freshnessTimestamp) : Number.NaN;
	const ageMs = nowMs - freshnessMs;
	const fresh = Number.isFinite(ageMs)
		&& ageMs >= -STATE_FUTURE_TOLERANCE_MS
		&& ageMs <= STATE_STALE_AFTER_MS;
	const outOfOrder = fresh
		&& latestAppliedProducerMs !== null
		&& freshnessMs < latestAppliedProducerMs;
	if (outOfOrder) {
		console.warn('[Poll] Ignoring out-of-order state snapshot');
		return false;
	}

	const producerStatus = state.bridge_status?.status;
	const status = !fresh
		? 'stale'
		: producerStatus === 'connected'
			? 'connected'
			: producerStatus === 'error'
				? 'error'
				: 'disconnected';

	bridgeStatus.set({
		status,
		carla_fps: finiteNumber(state.bridge_status?.carla_fps),
		objects_tracked: finiteNumber(state.bridge_status?.objects_tracked),
		cameras_active: finiteNumber(state.bridge_status?.cameras_active),
		last_heartbeat: lastHeartbeat,
		updated_at: updatedAt
	});

	if (!fresh) {
		wsConnected.set(false);
		return false;
	}

	latestAppliedProducerMs = freshnessMs;
	objects.setAll(Array.isArray(state.objects) ? state.objects : []);
	wsConnected.set(status === 'connected');
	return true;
}

/** Reset ordering state when beginning a new browser-side polling session. */
export function resetStateSnapshotFreshness(): void {
	latestAppliedProducerMs = null;
}

async function poll(generation: number): Promise<void> {
	if (generation !== pollGeneration || activePollRequestId !== null) return;
	const requestId = ++pollRequestSequence;
	activePollRequestId = requestId;

	try {
		const state = await fetchState();
		if (generation !== pollGeneration || activePollRequestId !== requestId) return;
		applyStateSnapshot(state);
	} catch (err) {
		if (generation !== pollGeneration || activePollRequestId !== requestId) return;
		console.warn('[Poll] Failed to fetch state:', err);
		wsConnected.set(false);
		bridgeStatus.update((s) => ({ ...s, status: 'disconnected' }));
	} finally {
		if (activePollRequestId === requestId) activePollRequestId = null;
	}
}

/**
 * Start polling state from the read API.
 */
export function connectWebSocket(): void {
	if (pollTimer) return;

	resetStateSnapshotFreshness();
	pollGeneration += 1;
	activePollRequestId = null;
	const generation = pollGeneration;
	console.log(`[Poll] Starting state polling every ${POLL_INTERVAL}ms`);
	void poll(generation); // initial fetch
	pollTimer = setInterval(() => {
		void poll(generation);
	}, POLL_INTERVAL);
}

/**
 * Stop polling.
 */
export function disconnectWebSocket(): void {
	if (pollTimer) {
		clearInterval(pollTimer);
		pollTimer = null;
	}
	pollGeneration += 1;
	activePollRequestId = null;
	wsConnected.set(false);
	resetStateSnapshotFreshness();
	bridgeStatus.update((status) => ({ ...status, status: 'disconnected' }));
}
