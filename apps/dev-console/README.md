# Drive API Dev Console

Developer-facing console for the CARLA drive WebSocket API — spawn a car,
drive it, script inputs, inject raw packets, and watch every frame on the
wire. Built for partners integrating against the drive server.

## Stack
React + Parcel. The whole UI lives in `src/App.tsx` (design tokens,
day/night dash modes, B612 / B612 Mono / Archivo type via @fontsource).

## Build
```
npm install   # or pnpm install
node ./node_modules/parcel/lib/bin.js build index.html --dist-dir dist --no-source-maps
```

## Deploy (Path PC)
The static file server on :8088 serves /tmp:
```
cp dist/*.js dist/*.css dist/*.woff dist/*.woff2 /tmp/
cp dist/index.html /tmp/dev.html
cp diagram.html /tmp/diagram.html
```
Note: /tmp is cleared on reboot — moving to a durable serve directory is a TODO.

## Pages
- `dev.html` — the console: ASSISTED / RAW operating modes, packet
  injector, script deck with gear picker, flight recorder, datasheet
- `diagram.html` — interactive explainer of the connection → DriveSession
  → CARLA-actor routing model
