<script lang="ts" module>
	export type WarningSeverity = 'critical' | 'warning' | 'info';
	export type WarningSource = 'v2x' | 'eva' | 'scenario' | 'verdict';

	export interface DashboardWarning {
		/** Stable dedup key — same id replaces an existing entry in place. */
		id: string;
		message: string;
		severity: WarningSeverity;
		source: WarningSource;
		/** Last time this warning was updated (ms). Used for auto-fade. */
		lastUpdate: number;
		/** Optional secondary text (e.g. "12.3m"). */
		detail?: string;
	}
</script>

<script lang="ts">
	import { fly } from 'svelte/transition';
	import { AlertTriangle, AlertCircle, Info } from 'lucide-svelte';

	interface Props {
		warnings: DashboardWarning[];
		/** Current time in ms (override for tests). Defaults to Date.now(). */
		now?: number;
		/** Fade items older than this many ms since lastUpdate. */
		fadeMs?: number;
		/** Max items shown before "+N more" badge. */
		maxVisible?: number;
	}

	let {
		warnings = [],
		now = Date.now(),
		fadeMs = 1500,
		maxVisible = 4,
	}: Props = $props();

	const fresh = $derived(
		warnings.filter((w) => now - w.lastUpdate <= fadeMs)
	);

	// Newest first — sort by lastUpdate desc so a flurry of new alerts pushes
	// older (but still fresh) ones down.
	const sorted = $derived(
		[...fresh].sort((a, b) => b.lastUpdate - a.lastUpdate)
	);

	const visible = $derived(sorted.slice(0, maxVisible));
	const overflow = $derived(Math.max(0, sorted.length - maxVisible));

	function severityBg(s: WarningSeverity): string {
		switch (s) {
			case 'critical':
				return 'var(--color-tesla-critical)';
			case 'warning':
				return 'var(--color-tesla-warning)';
			default:
				return 'var(--color-tesla-accent)';
		}
	}
</script>

<div
	class="flex flex-col gap-1.5 w-full h-full overflow-hidden p-2 pointer-events-none"
	data-testid="warning-stack"
	data-count={sorted.length}
	role="log"
	aria-live="polite"
>
	{#if visible.length === 0}
		<!-- Idle state: empty. The vehicle visualization fills the center stack. -->
		<div class="sr-only" data-testid="warning-empty">No active warnings</div>
	{:else}
		{#each visible as w (w.id)}
			<div
				class="flex items-center gap-2.5 rounded-md px-3 py-2 overflow-hidden font-tesla"
				style="
					background: var(--color-tesla-bg-elevated);
					border-left: 3px solid {severityBg(w.severity)};
					box-shadow: 0 0 12px {severityBg(w.severity)}33;
				"
				in:fly={{ y: -8, duration: 180 }}
				out:fly={{ y: -8, duration: 140 }}
				data-testid="warning-{w.id}"
				data-severity={w.severity}
				data-source={w.source}
			>
				<span
					class="shrink-0"
					style="color: {severityBg(w.severity)};"
					aria-hidden="true"
				>
					{#if w.severity === 'critical'}
						<AlertTriangle size={16} strokeWidth={2.4} />
					{:else if w.severity === 'warning'}
						<AlertCircle size={16} strokeWidth={2.4} />
					{:else}
						<Info size={16} strokeWidth={2.4} />
					{/if}
				</span>
				<span
					class="flex-1 text-sm leading-tight truncate"
					style="color: var(--color-tesla-text);"
					data-testid="warning-msg-{w.id}"
				>
					{w.message}
				</span>
				{#if w.detail}
					<span
						class="shrink-0 text-xs tabular-nums"
						style="
							color: var(--color-tesla-text-secondary);
							font-feature-settings: 'tnum';
						"
						data-testid="warning-detail-{w.id}"
					>
						{w.detail}
					</span>
				{/if}
			</div>
		{/each}

		{#if overflow > 0}
			<div
				class="self-center px-2 py-0.5 rounded text-[10px] uppercase tracking-widest font-tesla"
				style="
					color: var(--color-tesla-text-secondary);
					background: var(--color-tesla-bg-elevated);
				"
				data-testid="warning-overflow"
			>
				+{overflow} more
			</div>
		{/if}
	{/if}
</div>
