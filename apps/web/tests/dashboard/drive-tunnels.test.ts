import { describe, expect, it } from 'vitest';

import { buildDriveTunnels } from '../../src/lib/constants';


describe('Drive tunnel selection', () => {
	it('does not invent an unproven named Cloudflare endpoint', () => {
		const tunnels = buildDriveTunnels(
			{
				cloudflareDriveWsUrl: '',
				tailscaleDriveWsUrl: 'wss://path.example.test'
			},
			'laptop.example.test'
		);
		expect(tunnels).toEqual([
			{ id: 'tailscale', label: 'Tailscale', url: 'wss://path.example.test' }
		]);
	});

	it('rejects a Path-PC loopback endpoint for a remote browser', () => {
		const tunnels = buildDriveTunnels(
			{
				cloudflareDriveWsUrl: 'ws://localhost:8765',
				tailscaleDriveWsUrl: 'wss://path.example.test'
			},
			'simforgelaptop.example.test'
		);
		expect(tunnels.map((tunnel) => tunnel.id)).toEqual(['tailscale']);
	});

	it('allows loopback only when the page itself is local', () => {
		const tunnels = buildDriveTunnels(
			{
				cloudflareDriveWsUrl: 'ws://127.0.0.1:8765',
				tailscaleDriveWsUrl: 'wss://path.example.test'
			},
			'localhost'
		);
		expect(tunnels[0]).toEqual({
			id: 'cloudflare',
			label: 'Cloudflare',
			url: 'ws://127.0.0.1:8765'
		});
	});

	it('rejects every 127/8 endpoint from a remote browser', () => {
		for (const value of [
			'ws://127.0.0.1:8765',
			'ws://127.42.19.200:8765',
			'ws://127.255.255.254:8765',
			'ws://127.1:8765'
		]) {
			const tunnels = buildDriveTunnels(
				{
					cloudflareDriveWsUrl: value,
					tailscaleDriveWsUrl: 'wss://path.example.test'
				},
				'simforgelaptop.example.test'
			);
			expect(tunnels.map((tunnel) => tunnel.id)).toEqual(['tailscale']);
		}
	});

	it('rejects bracketed IPv6 loopback for either tunnel from a remote browser', () => {
		const tunnels = buildDriveTunnels(
			{
				cloudflareDriveWsUrl: 'ws://[::1]:8765',
				tailscaleDriveWsUrl: 'wss://[::1]:8765'
			},
			'simforgelaptop.example.test'
		);
		expect(tunnels).toEqual([]);
	});

	it('rejects credentialed and non-WebSocket URLs', () => {
		for (const value of ['ftp://example.test', 'wss://user:pass@example.test']) {
			const tunnels = buildDriveTunnels(
				{
					cloudflareDriveWsUrl: value,
					tailscaleDriveWsUrl: 'wss://path.example.test'
				},
				'laptop.example.test'
			);
			expect(tunnels.map((tunnel) => tunnel.id)).toEqual(['tailscale']);
		}
	});
});
