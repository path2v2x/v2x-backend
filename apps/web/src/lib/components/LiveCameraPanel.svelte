<script lang="ts">
	import type { RuntimeConfig } from '$lib/runtime-config';
	import LiveVideoCard from './LiveVideoCard.svelte';

	interface Props {
		config: RuntimeConfig | null;
	}

	let { config }: Props = $props();

	function buildPerceptionStreamUrl(cameraId: string): string {
		if (!config) return '';
		const explicitUrl = config.perceptionStreamUrls[cameraId];
		if (explicitUrl) return explicitUrl;
		if (!config.perceptionStreamBaseUrl) return '';
		const path = config.perceptionStreamPathTemplate.replace(
			'{camera_id}',
			encodeURIComponent(cameraId)
		);
		return `${config.perceptionStreamBaseUrl}${path.startsWith('/') ? path : `/${path}`}`;
	}
</script>

<section class="bg-black">
	{#if config}
		<div
			class="grid gap-px bg-gray-900"
			style="grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));"
		>
			{#each config.videoCameraIds as cameraId}
				{@const streamUrl = buildPerceptionStreamUrl(cameraId)}
				<LiveVideoCard
					{cameraId}
					{streamUrl}
					sourceLabel={streamUrl ? 'Perception' : 'Raw'}
				/>
			{/each}
		</div>
	{:else}
		<div class="flex min-h-40 items-center justify-center bg-black">
			<div class="h-8 w-8 animate-spin rounded-full border-2 border-gray-700 border-t-cyan-300"></div>
		</div>
	{/if}
</section>
